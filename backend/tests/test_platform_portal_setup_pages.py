"""Static checks on the vanilla platform portal pages for the security-setup
gate and the login challenge. No server or database needed.

The browser behaviour itself is exercised end to end separately; these catch
the cheap regressions: a new page that forgets the guard, a guard loaded after
the page's own script (too late to wrap its first fetch), or a login page that
would treat a challenge response as a token pair.
"""
from __future__ import annotations

import re
from pathlib import Path

PORTAL = Path(__file__).resolve().parents[1] / "src" / "platform_portal"
# The guard's tag, allowing a cache-busting query string (?v=1, ...).
GUARD = re.compile(r'<script src="/platform/statics/setup-guard\.js(?:\?[^"]*)?"></script>')
# Pages that must NOT carry the guard: sign-in, and the redirect target itself.
UNGUARDED = {"login.html", "setup.html"}


def pages():
    return sorted(p for p in PORTAL.glob("*.html") if p.name not in UNGUARDED)


def test_there_are_signed_in_pages_to_check():
    assert len(pages()) >= 10


def test_every_signed_in_page_loads_the_guard_before_its_own_script():
    missing, late = [], []
    for page in pages():
        html = page.read_text()
        guard = GUARD.search(html)
        if guard is None:
            missing.append(page.name)
            continue
        first_inline = re.search(r"<script>", html)
        if first_inline and guard.start() > first_inline.start():
            late.append(page.name)
    assert missing == [], f"pages without the setup guard: {missing}"
    assert late == [], f"guard loaded after the page's own script: {late}"


def test_setup_and_login_pages_do_not_load_the_guard():
    for name in UNGUARDED:
        assert GUARD.search((PORTAL / name).read_text()) is None, name


def test_the_guard_pattern_accepts_a_cache_buster_and_nothing_looser():
    ok = ['<script src="/platform/statics/setup-guard.js"></script>',
          '<script src="/platform/statics/setup-guard.js?v=1"></script>',
          '<script src="/platform/statics/setup-guard.js?v=2024-09-24"></script>']
    bad = ['<script src="/platform/statics/setup-guard.json"></script>',
           '<script src="/platform/statics/setup-guardXjs"></script>',
           '<script src="/elsewhere/setup-guard.js"></script>',
           '<!-- setup-guard.js -->']
    assert all(GUARD.search(t) for t in ok)
    assert not any(GUARD.search(t) for t in bad)


def test_the_guard_redirects_on_the_header_and_never_settles():
    js = (PORTAL / "statics" / "setup-guard.js").read_text()
    assert "X-Security-Setup-Required" in js
    assert "'/platform/setup'" in js
    assert "new Promise(function () {})" in js


def test_login_page_handles_the_challenge_and_never_stores_its_token():
    html = (PORTAL / "login.html").read_text()
    assert "challenge_required" in html
    assert "/api/v1/platform/auth/challenge" in html
    # The single-use challenge token must stay in page memory only.
    for m in re.finditer(r"localStorage\.setItem\(([^)]*)\)", html):
        assert "challenge" not in m.group(1), m.group(0)
    # A 200 without tokens must not be stored as a session.
    assert "if (!data.access_token)" in html


def test_setup_page_is_served():
    routes = (PORTAL / "routes.py").read_text()
    assert '"/platform/setup"' in routes and '"setup.html"' in routes


# ── Sign-out and the refresh token ────────────────────────────────────────

LOGOUT_PATH = "/api/v1/platform/auth/logout"


def _sign_out_code(text: str, start: str) -> str:
    """The sign-out handler's source: from `start` to the next blank line."""
    i = text.index(start)
    return text[i:text.index("\n\n", i)]


def test_no_portal_file_stores_the_refresh_token():
    """The portal never refreshes, so it must not keep the 7-day refresh token."""
    files = list(PORTAL.glob("*.html")) + list((PORTAL / "statics").glob("*.js"))
    stored = [f"{f.name}: {m.group(0)}" for f in files
              for m in re.finditer(r"localStorage\.setItem\(([^)]*)\)", f.read_text()) if "refresh" in m.group(1)]
    assert stored == []


def test_the_guard_clears_a_leftover_refresh_token_on_load():
    js = (PORTAL / "statics" / "setup-guard.js").read_text()
    assert "localStorage.removeItem('platform_refresh_token')" in js


def _assert_signs_out_without_waiting(code: str):
    assert LOGOUT_PATH in code
    assert "keepalive: true" in code
    assert "'Bearer ' + token" in code
    # Local sign-out comes first, so a failed or slow call can't leave it signed in.
    clear = code.index("localStorage.removeItem('platform_access_token')")
    assert clear < code.index("fetch(")
    # And the redirect never waits on the call: on success, failure, a throw,
    # or the timeout, whichever comes first.
    assert re.search(r"setTimeout\(leave, \w+\)", code)
    assert ".then(leave, leave)" in code
    assert re.search(r"catch \(e\) \{\s*leave\(\);", code)
    assert "if (!token) return leave();" in code


def test_nav_sign_out_ends_the_server_session_without_waiting_on_it():
    js = (PORTAL / "statics" / "nav.js").read_text()
    _assert_signs_out_without_waiting(_sign_out_code(js, "var SIGN_OUT_TIMEOUT_MS"))


def test_setup_sign_out_button_ends_the_server_session_without_waiting_on_it():
    html = (PORTAL / "setup.html").read_text()
    _assert_signs_out_without_waiting(_sign_out_code(html, "function signOutEverywhere()"))
    assert "getElementById('logout-btn').addEventListener('click', signOutEverywhere)" in html


def test_signed_in_pages_leave_sign_out_to_nav_js():
    """A page-local handler would sign out locally only, skipping the server."""
    for page in pages():
        assert not re.search(r"getElementById\('logout-btn'\)\.addEventListener", page.read_text()), page.name
