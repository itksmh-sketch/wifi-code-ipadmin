"""Guard for the hash-based CSP on /platform, /reseller and /apply
(src/middleware/csp_pages.py, phase B of the CSP rollout).

Fails on anything that would need 'unsafe-inline' or 'unsafe-eval' to run:
inline event handlers (in markup or built inside JS strings), javascript:
URLs, eval and friends. Fails on anything that would make a page's file hash
differ from what is served: template syntax inside an inline block, or a
carriage return. And it checks the live routes: every HTML page under the
three areas carries the Report-Only CSP, and every inline block it serves is
covered by a hash in that header.

Pure unit tests: no server, no DB, no Redis.
"""
from __future__ import annotations

import os
import re
from pathlib import Path

import httpx
import pytest

from src.app import app
from src.middleware import csp_pages

BACKEND = Path(__file__).resolve().parents[1]
PAGE_FILES = sorted([
    *BACKEND.glob("src/platform_portal/*.html"),
    *BACKEND.glob("src/reseller/*.html"),
    BACKEND / "static/apply.html",
])
AREAS = ("/platform", "/reseller", "/apply")
# 13 platform pages, 7 reseller pages, /apply.
EXPECTED_PAGE_ROUTES = 21

FORBIDDEN = [
    # A tag with an on…= attribute, in markup or inside a JS string that builds markup.
    ("inline event handler", re.compile(r"<[a-zA-Z][^>]*?\son[a-z]+\s*=", re.S)),
    ("setAttribute('on…')", re.compile(r"setAttribute\(\s*['\"`]on", re.I)),
    ("javascript: URL", re.compile(r"javascript:", re.I)),
    ("eval(", re.compile(r"\beval\s*\(")),
    ("new Function", re.compile(r"\bnew\s+Function\b")),
    ("string passed to setTimeout/setInterval", re.compile(r"\bset(?:Timeout|Interval)\s*\(\s*['\"`]")),
]


def rel(path: Path) -> str:
    return str(path.relative_to(BACKEND))


def test_the_page_list_is_complete():
    assert len(PAGE_FILES) == 13 + 7 + 1


@pytest.mark.parametrize("path", PAGE_FILES, ids=rel)
def test_no_inline_handlers_or_eval(path):
    text = path.read_text(encoding="utf-8")
    hits = []
    for name, pattern in FORBIDDEN:
        for m in pattern.finditer(text):
            line = text.count("\n", 0, m.start()) + 1
            hits.append(f"{rel(path)}:{line}: {name}: {m.group(0)[:80]!r}")
    assert not hits, "\n".join(hits)


@pytest.mark.parametrize("path", PAGE_FILES, ids=rel)
def test_file_hash_equals_served_hash(path):
    raw = path.read_bytes()
    assert b"\r" not in raw, f"{rel(path)} has a carriage return; the browser would hash LF instead"
    scripts, styles = csp_pages.inline_blocks(raw.decode("utf-8"))
    for block in scripts + styles:
        assert not re.search(r"\{\{|\{%|\{#", block), f"{rel(path)}: template syntax inside an inline block"


# ── The routes ────────────────────────────────────────────────────────────


async def get(path: str):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="https://ip-admin.example.test") as client:
        return await client.get(path)


def page_route_paths() -> list[str]:
    out = []
    for route in app.routes:
        path = getattr(route, "path", "")
        if "GET" not in (getattr(route, "methods", None) or ()) or ":path}" in path:
            continue
        if any(path == a or path.startswith(a + "/") for a in AREAS):
            out.append(re.sub(r"\{[^}]+\}", "00000000-0000-0000-0000-000000000000", path))
    return out


def directive(policy: str, name: str) -> list[str]:
    for part in policy.split(";"):
        tokens = part.split()
        if tokens and tokens[0] == name:
            return tokens[1:]
    return []


@pytest.mark.asyncio
async def test_every_page_route_carries_a_covering_report_only_csp():
    pages = 0
    for path in page_route_paths():
        res = await get(path)
        if res.status_code != 200 or not res.headers.get("content-type", "").startswith("text/html"):
            continue
        pages += 1
        policy = res.headers.get("content-security-policy-report-only", "")
        assert policy, f"{path}: no Report-Only CSP"
        assert "content-security-policy" not in res.headers, f"{path}: CSP is enforced, expected Report-Only"
        assert res.headers.get("reporting-endpoints") == 'csp-endpoint="https://ip-admin.example.test/api/v1/csp-report"'
        script_src, style_elem = directive(policy, "script-src"), directive(policy, "style-src-elem")
        assert "'unsafe-inline'" not in script_src and "'unsafe-eval'" not in script_src
        assert "'unsafe-inline'" not in style_elem
        scripts, styles = csp_pages.inline_blocks(res.text)
        for block in scripts:
            assert csp_pages.csp_hash(block) in script_src, f"{path}: inline script not covered: {block[:60]!r}"
        for block in styles:
            assert csp_pages.csp_hash(block) in style_elem, f"{path}: inline style not covered: {block[:60]!r}"
        # No stray hashes either: the header lists exactly what the page has.
        assert len([t for t in script_src if t.startswith("'sha256-")]) == len({*map(csp_pages.csp_hash, scripts)})
    assert pages == EXPECTED_PAGE_ROUTES


@pytest.mark.asyncio
async def test_the_middleware_still_adds_the_other_headers():
    res = await get("/platform/operators")
    assert res.headers["x-frame-options"] == "DENY"
    assert res.headers["cross-origin-opener-policy"] == "same-origin"
    assert res.headers["x-content-type-options"] == "nosniff"


# ── Re-hashing an HTML-only edit without a restart ───────────────────────


def test_an_edited_page_is_rehashed_on_the_next_request(tmp_path):
    page = tmp_path / "page.html"
    page.write_text("<script>let a = 1;</script><style>p{}</style>")
    before = csp_pages.policy_for(page)
    assert csp_pages.csp_hash("let a = 1;") in before

    page.write_text("<script>let a = 22;</script><style>p{}</style>")
    after = csp_pages.policy_for(page)
    assert csp_pages.csp_hash("let a = 22;") in after
    assert csp_pages.csp_hash("let a = 1;") not in after

    # Same size, different content: the mtime alone must invalidate it.
    st = page.stat()
    page.write_text("<script>let a = 33;</script><style>p{}</style>")
    os.utime(page, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000))
    assert csp_pages.csp_hash("let a = 33;") in csp_pages.policy_for(page)


def test_an_unchanged_page_is_served_from_the_cache(tmp_path, monkeypatch):
    page = tmp_path / "page.html"
    page.write_text("<script>1</script>")
    csp_pages.policy_for(page)
    monkeypatch.setattr(csp_pages, "inline_blocks", lambda html: pytest.fail("re-parsed an unchanged file"))
    csp_pages.policy_for(page)


def test_external_scripts_are_not_hashed():
    scripts, styles = csp_pages.inline_blocks(
        '<script src="/x.js"></script><script>a()</script><style>b{}</style><link rel="stylesheet" href="/c.css">')
    assert scripts == ["a()"] and styles == ["b{}"]
