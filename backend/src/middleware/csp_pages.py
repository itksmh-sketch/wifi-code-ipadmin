"""Report-Only CSP for the server-served pages: hash-based for /platform,
/reseller and /apply, nonce-based for the captive portal's three pages.

Each page's inline <script> and <style> blocks are allowed by their sha256
hash, so no 'unsafe-inline' for scripts. The hashes are computed from the
page's file on disk, never from the response body: hashing what is sent would
also allow a script injected server-side, while hashing the source only
allows what is in the repo. That equals the served text only while a page has
no template syntax and no carriage returns (the HTML parser turns CRLF into
LF before hashing); tests/test_csp_inline_guard.py pins both.

uvicorn reloads on .py changes, not .html, so an HTML-only edit would leave a
startup-time hash stale. The cache is keyed on the file's (mtime_ns, size)
instead: one os.stat() per request, and a changed file is re-hashed on the
next request with no restart.

Report-Only for now (phase B of the CSP rollout); the security headers
middleware still adds everything else and never overwrites these headers.
"""
from __future__ import annotations

import base64
import hashlib
import os
import secrets
from html.parser import HTMLParser
from pathlib import Path

from fastapi import Request
from fastapi.responses import FileResponse

from src.middleware.security_headers import report_suffix, reporting_endpoints


class _InlineBlocks(HTMLParser):
    """Collects the text of inline <script> (no src) and <style> blocks."""

    def __init__(self):
        super().__init__(convert_charrefs=False)
        self.scripts: list[str] = []
        self.styles: list[str] = []
        self._current: list[str] | None = None

    def handle_starttag(self, tag, attrs):
        if tag == "script" and not any(name == "src" for name, _ in attrs):
            self._current = self.scripts
        elif tag == "style":
            self._current = self.styles
        else:
            return
        self._current.append("")

    def handle_data(self, data):
        if self._current is not None:
            self._current[-1] += data

    def handle_endtag(self, tag):
        if tag in ("script", "style"):
            self._current = None


def inline_blocks(html: str) -> tuple[list[str], list[str]]:
    parser = _InlineBlocks()
    parser.feed(html)
    parser.close()
    return parser.scripts, parser.styles


def csp_hash(text: str) -> str:
    return "'sha256-" + base64.b64encode(hashlib.sha256(text.encode("utf-8")).digest()).decode() + "'"


def page_policy(script_hashes: list[str], style_hashes: list[str]) -> str:
    return "; ".join([
        "default-src 'self'",
        " ".join(["script-src 'self'", *script_hashes]),
        # Fallback for browsers without style-src-elem/-attr (CSP3 ignores
        # 'unsafe-inline' wherever a hash is present, hence the split).
        "style-src 'self' 'unsafe-inline'",
        " ".join(["style-src-elem 'self'", *style_hashes]),
        # ~140 style="" attributes across the platform pages, and the
        # notification-templates email preview (a sandboxed srcdoc iframe,
        # which inherits this policy) is styled by attributes too.
        "style-src-attr 'unsafe-inline'",
        "img-src 'self'",
        "connect-src 'self'",
        "font-src 'self'",
        "frame-src 'none'",
        "object-src 'none'",
        "base-uri 'none'",
        "form-action 'self'",
        "frame-ancestors 'none'",
    ])


# path -> ((mtime_ns, size), policy)
_cache: dict[str, tuple[tuple[int, int], str]] = {}


def policy_for(path: Path | str) -> str:
    key = str(path)
    st = os.stat(key)
    stamp = (st.st_mtime_ns, st.st_size)
    hit = _cache.get(key)
    if hit and hit[0] == stamp:
        return hit[1]
    with open(key, encoding="utf-8") as f:
        scripts, styles = inline_blocks(f.read())
    policy = page_policy(sorted({csp_hash(s) for s in scripts}), sorted({csp_hash(s) for s in styles}))
    _cache[key] = (stamp, policy)
    return policy


def page_csp_headers(path: Path | str, request: Request) -> dict[str, str]:
    return _report_only_headers(policy_for(path), request)


def _report_only_headers(policy: str, request: Request) -> dict[str, str]:
    headers = {"Content-Security-Policy-Report-Only": policy + report_suffix(request.scope)}
    endpoints = reporting_endpoints(request.scope)
    if endpoints:
        headers["Reporting-Endpoints"] = endpoints
    return headers


def serve_page(path: Path | str, request: Request) -> FileResponse:
    return FileResponse(path, headers=page_csp_headers(path, request))


# ── The captive portal (phase C) ─────────────────────────────────────────
#
# /portal/login, /pay and /success are Jinja-rendered per request with values
# inside their inline scripts, so a file hash can't match: each response gets
# a fresh nonce instead, which the template puts on its <script> tags.


def portal_policy(nonce: str) -> str:
    return "; ".join([
        "default-src 'self'",
        f"script-src 'self' 'nonce-{nonce}'",
        "style-src 'self' 'unsafe-inline'",
        # No <style> blocks; the server-rendered <html style="--primary-color…">
        # and the display toggles are attributes.
        "style-src-elem 'self'",
        "style-src-attr 'unsafe-inline'",
        "img-src 'self'",
        "connect-src 'self'",
        "font-src 'self'",
        "frame-src 'none'",
        "object-src 'none'",
        "base-uri 'none'",
        # The router sign-in posts to http://<gateway>/login, and the gateway
        # address differs per router.
        "form-action 'self' http:",
        # The admin branding page previews /portal/login in a same-origin frame.
        "frame-ancestors 'self'",
    ])


def portal_csp_headers(request: Request) -> tuple[str, dict[str, str]]:
    """(nonce, headers) for one portal page response."""
    nonce = secrets.token_urlsafe(16)
    return nonce, _report_only_headers(portal_policy(nonce), request)

