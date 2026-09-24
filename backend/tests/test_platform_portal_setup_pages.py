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
