"""Nonce-based Report-Only CSP on the captive portal (phase C of the CSP
rollout; src/middleware/csp_pages.py portal_csp_headers).

The portal's three pages are rendered per request, so each response gets a
fresh nonce that the template puts on its inline <script>. Guards here:
  * no inline handlers / javascript: / eval in any portal template or partial;
  * every inline script in a template carries exactly nonce="{{ csp_nonce }}";
  * the rendered pages: every inline script has the header's nonce, nonces
    differ per request, and the policy is the approved one;
  * report-to / Reporting-Endpoints only on HTTPS (the hotspot reaches the
    portal over plain http on the server IP);
  * what must NOT get the page CSP: statics, the JSON endpoints, and the
    router login template (served later by the router from its own origin).

Pure unit tests: the database is stubbed. No server, no DB, no Redis.
"""
from __future__ import annotations

import re
import uuid
from pathlib import Path

import httpx
import pytest

from src.app import app
from src.db.base import get_db
from src.middleware import csp_pages
from test_csp_inline_guard import FORBIDDEN, directive

PORTAL = Path(__file__).resolve().parents[2] / "portal"
TEMPLATES = sorted(PORTAL.glob("*.html"))
SITE, PLAN = str(uuid.uuid4()), str(uuid.uuid4())
PAGES = [
    f"/portal/login?site_id={SITE}&gateway=10.5.50.1",
    f"/portal/pay?plan_id={PLAN}&site_id={SITE}",
    "/portal/success?ref=abc123",
]
NONCE_ATTR = 'nonce="{{ csp_nonce }}"'


def test_the_template_list_is_complete():
    assert [p.name for p in TEMPLATES] == [
        "_brand_footer.html", "_brand_logo.html", "_head.html", "login.html", "pay.html", "success.html",
    ]


@pytest.mark.parametrize("path", TEMPLATES, ids=lambda p: p.name)
def test_no_inline_handlers_or_eval(path):
    text = path.read_text(encoding="utf-8")
    hits = [
        f"{path.name}:{text.count(chr(10), 0, m.start()) + 1}: {name}: {m.group(0)[:80]!r}"
        for name, pattern in FORBIDDEN for m in pattern.finditer(text)
    ]
    assert not hits, "\n".join(hits)


@pytest.mark.parametrize("path", TEMPLATES, ids=lambda p: p.name)
def test_every_inline_script_carries_the_nonce(path):
    text = path.read_text(encoding="utf-8")
    assert "<style" not in text, f"{path.name}: <style> blocks aren't allowed (style-src-elem 'self')"
    for tag in re.findall(r"<script\b[^>]*>", text):
        if "src=" in tag:
            continue
        assert tag == f"<script {NONCE_ATTR}>", f"{path.name}: inline script without the nonce: {tag}"


# ── The rendered pages ───────────────────────────────────────────────────


class _NoDb:
    async def execute(self, *_a, **_k):
        raise AssertionError("no DB access expected without a router token")


@pytest.fixture
def no_db():
    async def _db():
        yield _NoDb()

    app.dependency_overrides[get_db] = _db
    yield
    app.dependency_overrides.pop(get_db, None)


async def get(path: str, base: str = "https://ip-admin.example.test", **headers):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url=base) as client:
        return await client.get(path, headers=headers)


def nonce_of(policy: str) -> str:
    nonces = [t for t in directive(policy, "script-src") if t.startswith("'nonce-")]
    assert len(nonces) == 1, policy
    return nonces[0][len("'nonce-"):-1]


@pytest.mark.asyncio
@pytest.mark.parametrize("path", PAGES)
async def test_page_scripts_carry_the_headers_nonce(no_db, path):
    res = await get(path)
    assert res.status_code == 200 and res.headers["content-type"].startswith("text/html")
    assert "content-security-policy" not in res.headers, "Report-Only expected, not enforced"
    policy = res.headers["content-security-policy-report-only"]
    nonce = nonce_of(policy)
    assert len(nonce) >= 22
    inline = re.findall(r"<script\b(?![^>]*\bsrc=)[^>]*>", res.text)
    assert inline, "every portal page has an inline script"
    for tag in inline:
        assert tag == f'<script nonce="{nonce}">', tag
    scripts, styles = csp_pages.inline_blocks(res.text)
    assert len(scripts) == len(inline) and styles == []


@pytest.mark.asyncio
async def test_nonces_differ_per_request(no_db):
    a = nonce_of((await get(PAGES[0])).headers["content-security-policy-report-only"])
    b = nonce_of((await get(PAGES[0])).headers["content-security-policy-report-only"])
    assert a != b


@pytest.mark.asyncio
async def test_the_policy_is_the_approved_one(no_db):
    policy = (await get(PAGES[2])).headers["content-security-policy-report-only"]
    d = {p.split()[0]: " ".join(p.split()[1:]) for p in policy.split(";") if p.strip()}
    assert d["default-src"] == "'self'"
    assert d["script-src"].startswith("'self' 'nonce-") and "unsafe" not in d["script-src"]
    assert d["style-src"] == "'self' 'unsafe-inline'"
    assert d["style-src-elem"] == "'self'" and d["style-src-attr"] == "'unsafe-inline'"
    assert d["img-src"] == d["connect-src"] == d["font-src"] == "'self'"
    assert d["frame-src"] == d["object-src"] == d["base-uri"] == "'none'"
    assert d["form-action"] == "'self' http:"
    assert d["frame-ancestors"] == "'self'"
    assert d["report-uri"] == "/api/v1/csp-report"


@pytest.mark.asyncio
async def test_https_gets_report_to_and_reporting_endpoints(no_db):
    res = await get(PAGES[0], base="http://internal:8000", Host="ip-admin.example.test", **{"X-Forwarded-Proto": "https"})
    assert directive(res.headers["content-security-policy-report-only"], "report-to") == ["csp-endpoint"]
    assert res.headers["reporting-endpoints"] == 'csp-endpoint="https://ip-admin.example.test/api/v1/csp-report"'


@pytest.mark.asyncio
async def test_plain_http_gets_report_uri_only(no_db):
    # The hotspot origin: nginx's IP site passes Host but no X-Forwarded-Proto.
    res = await get(PAGES[0], base="http://34.122.11.114")
    policy = res.headers["content-security-policy-report-only"]
    assert directive(policy, "report-uri") == ["/api/v1/csp-report"]
    assert "report-to" not in policy
    assert "reporting-endpoints" not in res.headers


@pytest.mark.asyncio
async def test_the_other_portal_headers_are_unchanged(no_db):
    res = await get(PAGES[0])
    assert res.headers["x-frame-options"] == "SAMEORIGIN"
    assert "cross-origin-opener-policy" not in res.headers


# ── What must not get the page CSP ───────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["/portal/statics/style.css", "/portal/statics/branding.js"])
async def test_statics_have_no_csp(path):
    res = await get(path)
    assert res.status_code == 200
    assert "content-security-policy-report-only" not in res.headers
    assert "content-security-policy" not in res.headers


@pytest.mark.asyncio
async def test_json_endpoints_have_no_page_csp(no_db):
    res = await get("/portal/branding/not-a-token")
    assert res.headers["content-type"].startswith("application/json")
    assert "content-security-policy-report-only" not in res.headers


@pytest.mark.asyncio
async def test_router_login_template_is_untouched(monkeypatch):
    from src.config import get_settings

    monkeypatch.setattr(type(get_settings()), "effective_portal_public_base_url", property(lambda s: "http://34.122.11.114"))
    res = await get("/portal/mikrotik/login-template?rt=abc")
    assert res.status_code == 200
    assert "content-security-policy-report-only" not in res.headers
    assert "content-security-policy" not in res.headers
    assert "<script>" in res.text and "nonce" not in res.text
