"""The public landing page (/) and the operator docs (/guides).

The fee, trial length and support email are rendered by the server from the
same sources operator approval reads (src/admin_portal/public_values.py, kept
in memory for a minute), so the pages carry no hard-coded copy of them. Both pages carry nothing inline (no <script>/<style> block, no style
attribute, no handler), so their Report-Only CSP has no hashes and keeps
working when it is enforced with no 'unsafe-inline' at all. landing.js only
sets classes and the open state of menus, never HTML or inline styles.

Pure unit tests: no server, no DB, no Redis.
"""
from __future__ import annotations

import asyncio
import re
from contextlib import asynccontextmanager
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from src.admin_portal import public_values
from src.admin_portal.public_values import PublicValues
from src.app import app
from src.middleware import csp_pages

BACKEND = Path(__file__).resolve().parents[1]
STATIC = BACKEND / "static"
PAGES = {"/": STATIC / "index.html", "/guides": STATIC / "docs.html"}
LANDING_JS = STATIC / "platform-ui/landing.js"
ICONS = STATIC / "platform-ui/icons.svg"

FORBIDDEN = [
    ("inline event handler", re.compile(r"<[a-zA-Z][^>]*?\son[a-z]+\s*=", re.S)),
    ("javascript: URL", re.compile(r"javascript:", re.I)),
    ("eval(", re.compile(r"\beval\s*\(")),
    ("new Function", re.compile(r"\bnew\s+Function\b")),
    ("HTML string sink", re.compile(r"\.(?:innerHTML|outerHTML)\b|insertAdjacentHTML|document\.write")),
    ("inline style from script", re.compile(r"\.style\b|setAttribute\(\s*['\"]style")),
]


async def get(path: str):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="https://ip-admin.example.test") as client:
        return await client.get(path)


@pytest.mark.parametrize("page", sorted(PAGES.values()), ids=lambda p: p.name)
def test_page_has_nothing_inline(page):
    html = page.read_text(encoding="utf-8")
    scripts, styles = csp_pages.inline_blocks(html)
    assert scripts == [] and styles == [], f"{page.name}: inline <script>/<style> block"
    assert not re.search(r"<[a-zA-Z][^>]*?\sstyle\s*=", html, re.S), f"{page.name}: style attribute"
    hits = [name for name, pattern in FORBIDDEN[:2] if pattern.search(html)]
    assert not hits, f"{page.name}: {hits}"
    assert b"\r" not in page.read_bytes()
    assert 'src="/platform-ui/landing.js' in html


def test_landing_js_builds_no_html_and_sets_no_styles():
    js = LANDING_JS.read_text(encoding="utf-8")
    hits = [name for name, pattern in FORBIDDEN if pattern.search(js)]
    assert not hits, hits


@pytest.mark.parametrize("page", sorted(PAGES.values()), ids=lambda p: p.name)
def test_every_icon_the_page_uses_exists(page):
    html = page.read_text(encoding="utf-8")
    defined = set(re.findall(r'<symbol id="([^"]+)"', ICONS.read_text(encoding="utf-8")))
    used = set(re.findall(r'icons\.svg\?v=2#([a-z0-9-]+)', html))
    assert used and used <= defined, used - defined
    # Versioned, so a browser holding the sprite from before the new symbols
    # fetches it again instead of drawing blank icons.
    assert "icons.svg#" not in html


@pytest.mark.parametrize("page", sorted(PAGES.values()), ids=lambda p: p.name)
def test_in_page_links_point_at_real_ids(page):
    html = page.read_text(encoding="utf-8")
    ids = set(re.findall(r'\sid="([^"]+)"', html))
    targets = set(re.findall(r'href="#([^"]+)"', html))
    assert targets <= ids, targets - ids


# The values the pages are rendered with. Deliberately not the production
# ones, so a page still carrying a hard-coded copy would fail.
FEE, TRIAL, EMAIL, APP_URL = Decimal("95.50"), 30, "help@example.test", "https://ip-admin.example.test"


@pytest.fixture
def source(monkeypatch):
    """Stands in for the database behind public_values, with a clock the test moves."""
    state = SimpleNamespace(fee=FEE, trial=TRIAL, email=EMAIL, app_url=APP_URL, fail=False, calls=0, now=1000.0)

    async def load():
        state.calls += 1
        if state.fail:
            raise RuntimeError("database down")
        return PublicValues(public_values.format_fee(state.fee), state.trial, state.email, state.app_url)

    monkeypatch.setattr(public_values, "_load", load)
    monkeypatch.setattr(public_values, "_clock", lambda: state.now)
    public_values.reset()
    yield state
    public_values.reset()


@pytest.mark.parametrize("page", sorted(PAGES.values()), ids=lambda p: p.name)
def test_page_has_no_hard_coded_fee_trial_or_email(page):
    html = page.read_text(encoding="utf-8")
    assert "{{ trial_days }}-day" in html and "GHS {{ fee }}" in html and "{{ support_email }}" in html
    # The hero's dashboard mock-up has made-up GHS figures, so look for the old copies by value.
    assert not re.search(r"\b\d+-day\b|GHS 80\b|@gmail\.com|duckdns|data-live", html)
    # Only these values, always autoescaped.
    assert set(re.findall(r"\{\{\s*([^}]*?)\s*\}\}", html)) == {"fee", "trial_days", "support_email", "app_url"}
    assert "{%" not in html


@pytest.mark.parametrize("page", sorted(PAGES.values()), ids=lambda p: p.name)
def test_whatsapp_links_open_the_number_shown(page):
    html = page.read_text(encoding="utf-8")
    links = re.findall(r'href="https://wa\.me/(\d+)"', html)
    shown = {"233" + n.replace(" ", "")[1:] for n in re.findall(r"\b0\d{2} \d{3} \d{4}\b", html)}
    assert links and set(links) == shown


@pytest.mark.asyncio
@pytest.mark.parametrize("path", sorted(PAGES))
async def test_page_is_rendered_with_the_current_settings(path, source):
    res = await get(path)
    assert res.status_code == 200
    assert res.headers["content-type"].startswith("text/html")
    body = res.text
    assert "{{" not in body
    assert "GHS 95.50" in body and "30-day free trial" in body
    assert 'href="mailto:help@example.test"' in body


@pytest.mark.asyncio
@pytest.mark.parametrize("path", sorted(PAGES))
async def test_page_headers(path, source):
    res = await get(path)
    policy = res.headers["content-security-policy-report-only"]
    assert "frame-ancestors 'none'" in policy
    assert "'sha256-" not in policy
    assert res.headers["x-frame-options"] == "DENY"
    assert res.headers["cache-control"] == "no-cache"


@pytest.mark.asyncio
@pytest.mark.parametrize("path", sorted(PAGES))
async def test_link_preview_tags(path, source):
    body = (await get(path)).text
    url = APP_URL + path

    def meta(prop):
        m = re.search(r'<meta (?:property|name)="%s" content="([^"]*)">' % re.escape(prop), body)
        assert m, prop
        return m.group(1)

    assert f'<link rel="canonical" href="{url}">' in body
    assert meta("og:url") == url
    assert meta("og:image") == APP_URL + "/platform-ui/apple-touch-icon.png"
    assert meta("og:title") and meta("og:site_name") == "IpAdmin" and meta("twitter:card") == "summary"
    # No price in the preview text: a shared link can outlive a fee change.
    assert not re.search(r"GHS|\d", meta("og:description"))


@pytest.mark.asyncio
async def test_settings_values_are_escaped(source):
    source.email = '"><script>x</script>'
    body = (await get("/")).text
    assert "<script>x</script>" not in body
    assert "&lt;script&gt;" in body


@pytest.mark.parametrize("fee,shown", [
    (Decimal("80.00"), "80"), (Decimal("80.50"), "80.50"), (Decimal("1200.00"), "1,200"), (Decimal("0.00"), "0"),
])
def test_format_fee(fee, shown):
    assert public_values.format_fee(fee) == shown


# ── The one-minute cache ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_fresh_values_come_from_memory(source):
    for _ in range(3):
        assert (await get("/")).status_code == 200
    assert source.calls == 1


@pytest.mark.asyncio
async def test_a_changed_setting_shows_once_the_minute_is_up(source):
    await get("/")
    source.fee, source.trial = Decimal("120.00"), 7
    source.now += 59
    assert "GHS 95.50" in (await get("/")).text
    source.now += 2
    body = (await get("/")).text
    assert "GHS 120" in body and "7-day free trial" in body
    assert source.calls == 2


@pytest.mark.asyncio
async def test_a_failed_refresh_serves_the_last_good_values_and_retries_later(source):
    await get("/")
    source.now += 61
    source.fail = True
    res = await get("/")
    assert res.status_code == 200 and "GHS 95.50" in res.text
    assert source.calls == 2
    source.now += 5
    assert (await get("/")).status_code == 200
    assert source.calls == 2  # no retry inside RETRY_SECONDS
    source.fail, source.fee = False, Decimal("100.00")
    source.now += 6
    assert "GHS 100" in (await get("/")).text
    assert source.calls == 3


@pytest.mark.asyncio
async def test_nothing_ever_loaded_is_a_503(source):
    source.fail = True
    res = await get("/")
    assert res.status_code == 503
    assert res.headers["retry-after"] == "30" and res.headers["cache-control"] == "no-store"
    assert "{{" not in res.text and "GHS" not in res.text
    assert not re.search(r"<[a-zA-Z][^>]*?\sstyle\s*=|<script|<style", res.text)
    await get("/guides")
    assert source.calls == 1  # a down database is not hit on every request
    source.fail = False
    source.now += 11
    assert (await get("/guides")).status_code == 200


@pytest.mark.asyncio
async def test_concurrent_requests_share_one_refresh(source, monkeypatch):
    slow_calls = 0

    async def slow_load():
        nonlocal slow_calls
        slow_calls += 1
        await asyncio.sleep(0.01)
        return PublicValues("80", 21, EMAIL, APP_URL)

    monkeypatch.setattr(public_values, "_load", slow_load)
    results = await asyncio.gather(*(public_values.get_public_values() for _ in range(20)))
    assert slow_calls == 1 and all(r == results[0] for r in results)


# ── _load: where the values come from ────────────────────────────────────


@pytest.fixture
def db_source(monkeypatch):
    state = SimpleNamespace(settings={
        "platform_trial_days": "21", "platform_support_email": " help@example.test ",
        "platform_app_url": "https://ip-admin.example.test/",
    }, fail_key=None)

    @asynccontextmanager
    async def session():
        yield object()

    async def fee(db):
        return Decimal("80.00")

    async def setting(db, key):
        if key == state.fail_key:
            raise ConnectionError("database down")
        return state.settings[key]

    monkeypatch.setattr(public_values, "async_session_factory", session)
    monkeypatch.setattr(public_values, "get_default_monthly_fee", fee)
    monkeypatch.setattr(public_values, "get_setting", setting)
    return state


@pytest.mark.asyncio
async def test_load_reads_and_tidies_the_settings(db_source):
    assert await public_values._load() == PublicValues("80", 21, "help@example.test", "https://ip-admin.example.test")


@pytest.mark.asyncio
async def test_load_falls_back_like_approval_for_an_unusable_trial(db_source, monkeypatch):
    db_source.settings["platform_trial_days"] = "0"
    monkeypatch.setattr(public_values, "get_settings", lambda: SimpleNamespace(trial_days=9))
    assert (await public_values._load()).trial_days == 9


@pytest.mark.asyncio
async def test_load_raises_on_a_database_error_instead_of_guessing(db_source):
    # get_trial_days() would swallow this and return the .env value; the cache
    # must see the failure so it keeps serving the last good values.
    db_source.fail_key = "platform_trial_days"
    with pytest.raises(ConnectionError):
        await public_values._load()


# ── "Coming soon" must go when the reseller screens ship ─────────────────

ADMIN_BUNDLES = STATIC / "admin" / "assets"
APP_SOURCE = BACKEND.parent / "frontend" / "src" / "App.jsx"  # only in a full checkout
SOON_TAG = 'class="lp-soon"'


def dashboard_route_paths(texts: list[str]) -> set[str]:
    """Route paths from the built admin bundle (path:"/x") and App.jsx (path="/x")."""
    paths = set()
    for text in texts:
        paths |= set(re.findall(r'path\s*[:=]\s*"(/[^"]*)"', text))
    return paths


def _dashboard_texts() -> list[str]:
    texts = [js.read_text(encoding="utf-8") for js in ADMIN_BUNDLES.glob("*.js")]
    if APP_SOURCE.exists():
        texts.append(APP_SOURCE.read_text(encoding="utf-8"))
    return texts


def test_route_detection_finds_routes_in_both_forms():
    assert dashboard_route_paths(['x({path:"/resellers",element:y})']) == {"/resellers"}
    assert dashboard_route_paths(['<Route path="/resellers/:id" element={<R />} />']) == {"/resellers/:id"}


def test_coming_soon_tag_is_removed_once_the_dashboard_has_reseller_screens():
    paths = dashboard_route_paths(_dashboard_texts())
    # The detection still works on this build: a page that certainly exists is found.
    assert "/vouchers" in paths, "no dashboard routes found; has the bundle format changed?"
    reseller_routes = sorted(p for p in paths if "reseller" in p)
    tagged = [p.name for p in PAGES.values() if SOON_TAG in p.read_text(encoding="utf-8")]
    assert not (reseller_routes and tagged), (
        f"The operator dashboard now has reseller routes {reseller_routes}: remove the 'Coming soon' "
        f"tags and rewrite the reseller text in {tagged} for the shipped screens."
    )
