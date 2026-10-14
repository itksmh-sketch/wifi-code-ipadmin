"""The account status controls on operator_detail.html and the operators list.
Pure unit tests: no server, no DB, no Redis.

Same rules as tests/test_operator_access_ui.py: the new markup and script add
nothing CSP would need 'unsafe-inline' for, and everything the script puts in
innerHTML from the API is escaped. Also pins the moves the page offers to the
server's transition table, so the two can't drift apart.
"""
from __future__ import annotations

import re
from html.parser import HTMLParser
from pathlib import Path

import pytest

from src.modules.platform import operator_status

PORTAL = Path(__file__).resolve().parents[1] / "src" / "platform_portal"
PAGE = PORTAL / "operator_detail.html"
LIST = PORTAL / "operators.html"
SCRIPT = PORTAL / "statics" / "operator_status.js"
NEW_ROOTS = {"status-card", "status-overlay"}


class _Collector(HTMLParser):
    """Attributes of every element inside NEW_ROOTS, and every <script>."""

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
    assert page.inside, "status-card / status-overlay not found"
    for tag, attrs in page.inside:
        assert "style" not in attrs, f"<{tag} id={attrs.get('id')}> has a style attribute"
        assert not [a for a in attrs if a.startswith("on")], f"<{tag}> has an inline handler"


def test_every_id_the_script_uses_exists(page):
    used = set(re.findall(r"\$\('([a-z0-9-]+)'\)", SCRIPT.read_text()))
    assert used and used <= page.ids, sorted(used - page.ids)


def test_script_is_external_and_after_the_page_script(page):
    sources = [s.get("src", "") for s in page.scripts]
    ours = [i for i, src in enumerate(sources) if "operator_status.js" in src]
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
    for field in ("status", "billing_status", "suspension_reason", "invoice_number", "amount_ghs",
                  "reseller_wallet_total_ghs", "cancelled_at", "hotspot_service_ends_at", "name"):
        for match in re.finditer(r"\+ (?:i|operator)\." + field + r"\b", js):
            line = js[js.rfind("\n", 0, match.start()) + 1:js.find("\n", match.start())]
            # textContent is safe; anything else must go through esc().
            assert "textContent" in line or "return 'Approves" in line, f"unescaped {match.group(0)!r}: {line.strip()}"


def test_moves_offered_match_the_server_transition_table():
    js = SCRIPT.read_text()
    moves = dict(re.findall(r"^\s+(\w+): \[([^\]]*)\],$", js[js.index("const MOVES"):js.index("const MOVE =")], re.M))
    targets = dict(re.findall(r"^\s+(\w+): \{ status: '(\w+)'", js[js.index("const MOVE ="):], re.M))
    offered = {status: {targets[m.strip().strip("'")] for m in listed.split(",") if m.strip()}
               for status, listed in moves.items()}
    assert offered == {k: set(v) for k, v in operator_status.TRANSITIONS.items()}


def test_default_wind_down_matches_the_server():
    js = SCRIPT.read_text()
    assert f"const DEFAULT_DAYS = {operator_status.DEFAULT_HOTSPOT_SERVICE_DAYS};" in js
    assert f'value="{operator_status.DEFAULT_HOTSPOT_SERVICE_DAYS}"' in PAGE.read_text()


def test_operators_list_has_no_one_click_status_buttons():
    html = LIST.read_text()
    assert 'data-action="set-status"' not in html and "setStatus(" not in html
    assert "#status-card" in html
