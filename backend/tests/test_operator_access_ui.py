"""The sign-in access controls on operator_detail.html, and the audit read
rules. Pure unit tests: no server, no DB, no Redis.

The controls must add nothing CSP would need 'unsafe-inline' for: no inline
script, no inline handler, no style attribute — in the markup or in anything
the script builds. tests/test_csp_inline_guard.py covers the page as a whole;
this pins the new parts, which are stricter than the rest of the page (it
still has style attributes of its own).
"""
from __future__ import annotations

import re
from html.parser import HTMLParser
from pathlib import Path

import pytest

from src.modules.audit import read as audit_read

PORTAL = Path(__file__).resolve().parents[1] / "src" / "platform_portal"
PAGE = PORTAL / "operator_detail.html"
SCRIPT = PORTAL / "statics" / "operator_access.js"
NEW_ROOTS = {"access-card", "access-overlay"}


class _Collector(HTMLParser):
    """Attributes of every element inside the new roots, and every <script>."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.depth = 0
        self.inside: list[tuple[str, dict]] = []
        self.scripts: list[dict] = []
        self.ids: set[str] = set()
        self._stack: list[bool] = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "script":
            self.scripts.append(attrs)
        if "id" in attrs:
            self.ids.add(attrs["id"])
        entering = attrs.get("id") in NEW_ROOTS
        if entering or self.depth:
            self.inside.append((tag, attrs))
        if tag in ("input", "br", "img", "meta", "link"):
            return
        self._stack.append(entering or self.depth > 0)
        if entering or self.depth:
            self.depth += 1

    def handle_endtag(self, tag):
        if tag in ("input", "br", "img", "meta", "link") or not self._stack:
            return
        if self._stack.pop():
            self.depth -= 1


@pytest.fixture(scope="module")
def page():
    collector = _Collector()
    collector.feed(PAGE.read_text())
    return collector


def test_new_markup_has_no_style_or_handler_attributes(page):
    assert page.inside, "access-card / access-overlay not found"
    for tag, attrs in page.inside:
        assert "style" not in attrs, f"<{tag} id={attrs.get('id')}> has a style attribute"
        assert not [a for a in attrs if a.startswith("on")], f"<{tag}> has an inline handler"


def test_every_id_the_script_uses_exists(page):
    used = set(re.findall(r"\$\('([a-z0-9-]+)'\)", SCRIPT.read_text()))
    assert used and used <= page.ids, sorted(used - page.ids)


def test_script_is_external_and_after_the_page_script(page):
    sources = [s.get("src", "") for s in page.scripts]
    ours = [i for i, src in enumerate(sources) if "operator_access.js" in src]
    assert len(ours) == 1
    inline = [i for i, s in enumerate(page.scripts) if "src" not in s]
    assert inline and ours[0] > max(inline)


def test_script_builds_nothing_csp_would_refuse():
    js = SCRIPT.read_text()
    assert ".style" not in js
    assert "style=" not in js
    assert not re.search(r"""['"<\s]on[a-z]+=""", js)
    assert "javascript:" not in js
    assert "eval(" not in js and "new Function" not in js and "setTimeout('" not in js
    # Everything that lands in innerHTML from the API goes through esc().
    for field in ("email", "role", "disabled_reason", "admin_logins_disabled_reason", "id"):
        for match in re.finditer(r"\+ (?:a|operator)\." + field + r"\b", js):
            raise AssertionError(f"unescaped {match.group(0)!r} in operator_access.js")


# ── audit read rules ──────────────────────────────────────────────────────


def test_operator_sees_platform_actions_as_platform():
    shown = audit_read.present_actor("platform_owner", "00000000-0000-0000-0000-000000000001",
                                     "owner@example.com", viewer=audit_read.VIEWER_OPERATOR)
    assert shown == {"type": "platform_owner", "id": None, "label": "Platform"}


def test_platform_sees_who_acted():
    shown = audit_read.present_actor("platform_owner", "00000000-0000-0000-0000-000000000001",
                                     "owner@example.com", viewer=audit_read.VIEWER_PLATFORM)
    assert shown["label"] == "owner@example.com" and shown["id"]


def test_operator_users_and_system():
    user = audit_read.present_actor("operator_user", "00000000-0000-0000-0000-000000000002", "a@b.c",
                                    viewer=audit_read.VIEWER_OPERATOR)
    assert user["label"] == "a@b.c"
    system = audit_read.present_actor("system", None, None, viewer=audit_read.VIEWER_OPERATOR)
    assert system == {"type": "system", "id": None, "label": "System"}
    with pytest.raises(ValueError):
        audit_read.present_actor("nobody", None, None, viewer=audit_read.VIEWER_OPERATOR)


def test_null_safe_compiles_to_nullif():
    from sqlalchemy.dialects import postgresql

    from src.db.models import AuditLog

    sql = str(audit_read.null_safe(AuditLog.changes).compile(dialect=postgresql.dialect()))
    assert sql == "nullif(audit_log.changes, 'null'::jsonb)"
