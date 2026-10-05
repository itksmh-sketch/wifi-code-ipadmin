"""Security headers (src/middleware/security_headers.py) and the CSP report
endpoint (POST /api/v1/csp-report).

Phase A of the CSP rollout: the non-CSP headers are enforced everywhere, CSP is
enforced on API responses, and /admin's CSP is Report-Only. /platform,
/reseller and /apply get a hash-based Report-Only CSP from their routes (phase
B, tests/test_csp_inline_guard.py). The portal gets no CSP yet; a test pins
that, so turning it on is a deliberate change here.

Pure unit tests: only routes that need no database are requested. No server,
no DB, no Redis.
"""
from __future__ import annotations

import json
import logging

import httpx
import pytest

from src.app import app
from src.middleware import security_headers as sh
from src.modules.security import csp_report

ALL_RESPONSES = {
    "x-content-type-options": "nosniff",
    "referrer-policy": "strict-origin-when-cross-origin",
    "permissions-policy": "camera=(), microphone=(), geolocation=(), payment=(), usb=()",
}


async def request(method, path, **kwargs):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="https://ip-admin.example.test") as client:
        return await client.request(method, path, **kwargs)


def csp_directives(value: str) -> dict[str, str]:
    out = {}
    for part in value.split(";"):
        part = part.strip()
        if part:
            name, _, rest = part.partition(" ")
            out[name] = rest
    return out


# ── Prefix matching ───────────────────────────────────────────────────────


@pytest.mark.parametrize("path,prefix", [
    ("/admin", "/admin"), ("/admin/billing", "/admin"), ("/admin/assets/x.js", "/admin"),
    ("/platform/operators", "/platform"), ("/reseller/print", "/reseller"), ("/apply", "/apply"),
    ("/portal/login", "/portal"), ("/api/v1/health", "/api"),
])
def test_paths_map_to_their_area(path, prefix):
    assert sh.area_for(path) is dict(sh.AREAS)[prefix]


@pytest.mark.parametrize("path", ["/platform-ui/tokens.css", "/administrator", "/apply-now", "/portals", "/", "/health"])
def test_lookalike_prefixes_get_the_default_area(path):
    assert sh.area_for(path) is sh.DEFAULT_AREA


# ── Headers per area ──────────────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize("path,frame,coop", [
    ("/admin/login", "DENY", "same-origin"),
    ("/platform/login", "DENY", "same-origin"),
    ("/reseller/login", "DENY", "same-origin"),
    ("/apply", "DENY", "same-origin"),
    ("/portal/statics/style.css", "SAMEORIGIN", None),
    ("/platform-ui/tokens.css", "DENY", None),
    ("/api/v1/health", "DENY", None),
])
async def test_enforced_headers(path, frame, coop):
    res = await request("GET", path)
    assert res.status_code == 200, (path, res.status_code)
    for name, value in ALL_RESPONSES.items():
        assert res.headers.get(name) == value, (path, name)
    assert res.headers.get("x-frame-options") == frame
    assert res.headers.get("cross-origin-opener-policy") == coop


@pytest.mark.asyncio
async def test_admin_csp_is_report_only_and_strict():
    res = await request("GET", "/admin/login")
    assert "content-security-policy" not in res.headers, "not enforced yet (phase D)"
    d = csp_directives(res.headers["content-security-policy-report-only"])
    assert d["default-src"] == "'self'"
    assert d["script-src"] == "'self'", "no 'unsafe-inline', no 'unsafe-eval'"
    assert d["style-src"] == "'self'"
    assert d["img-src"] == "'self' blob:"
    assert d["frame-src"] == "'self'"
    assert d["object-src"] == "'none'" and d["base-uri"] == "'none'" and d["frame-ancestors"] == "'none'"
    assert d["form-action"] == "'self'"
    assert d["report-uri"] == "/api/v1/csp-report" and d["report-to"] == "csp-endpoint"
    assert res.headers["reporting-endpoints"] == 'csp-endpoint="https://ip-admin.example.test/api/v1/csp-report"'
    assert "unsafe" not in res.headers["content-security-policy-report-only"]


@pytest.mark.asyncio
async def test_reporting_endpoint_follows_the_forwarded_scheme():
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://internal:8000") as client:
        res = await client.get("/admin/login", headers={"Host": "ip-admin.example.test", "X-Forwarded-Proto": "https"})
    assert res.headers["reporting-endpoints"] == 'csp-endpoint="https://ip-admin.example.test/api/v1/csp-report"'


@pytest.mark.asyncio
async def test_admin_assets_carry_no_csp():
    """CSP only means something on documents."""
    res = await request("GET", "/admin/login")
    assert res.headers["content-type"].startswith("text/html")
    asset = next(p for p in res.text.split('"') if p.startswith("/admin/assets/") and p.endswith(".js"))
    res = await request("GET", asset)
    assert res.status_code == 200
    assert "content-security-policy-report-only" not in res.headers
    assert res.headers["x-content-type-options"] == "nosniff"


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["/portal/statics/style.css"])
async def test_the_portal_has_no_csp_yet(path):
    res = await request("GET", path)
    assert "content-security-policy" not in res.headers
    assert "content-security-policy-report-only" not in res.headers


@pytest.mark.asyncio
@pytest.mark.parametrize("method,path", [("GET", "/api/v1/health"), ("GET", "/api/v1/no-such-route"),
                                         ("GET", "/api/v1/csp-report")])
async def test_api_responses_get_the_strict_csp_whatever_the_status(method, path):
    res = await request(method, path)
    assert res.headers["content-security-policy"] == "default-src 'none'; frame-ancestors 'none'"


@pytest.mark.asyncio
async def test_a_route_that_sets_its_own_csp_keeps_it():
    res = await request("GET", "/api/v1/billing/payment-callback", params={"reference": "INV-1"})
    assert res.headers["content-security-policy"].startswith("default-src 'none'; style-src 'sha256-")
    assert res.headers["x-content-type-options"] == "nosniff"
    assert res.headers.get_list("content-security-policy") == [res.headers["content-security-policy"]]


@pytest.mark.asyncio
async def test_cors_preflight_gets_the_headers_too():
    res = await request("OPTIONS", "/api/v1/health",
                        headers={"Origin": "https://example.test", "Access-Control-Request-Method": "GET"})
    assert res.headers["x-content-type-options"] == "nosniff"


@pytest.mark.asyncio
async def test_headers_are_never_duplicated():
    res = await request("GET", "/admin/login")
    for name in ("x-frame-options", "x-content-type-options", "cross-origin-opener-policy",
                 "content-security-policy-report-only"):
        assert len(res.headers.get_list(name)) == 1, name


# ── POST /api/v1/csp-report ───────────────────────────────────────────────


@pytest.fixture
def no_limit(monkeypatch):
    async def allow(*args, **kwargs):
        return None
    monkeypatch.setattr(csp_report, "enforce_rate_limit", allow)


def logged(caplog):
    return [r.getMessage() for r in caplog.records if r.name == "csp_report"]


@pytest.mark.asyncio
async def test_report_uri_format_is_logged_without_query_strings(no_limit, caplog):
    caplog.set_level(logging.WARNING, logger="csp_report")
    body = {"csp-report": {
        "document-uri": "https://ip-admin.example.test/portal/login?rt=SECRET-TOKEN#x",
        "effective-directive": "script-src-elem", "blocked-uri": "https://evil.example/x.js?k=v",
        "source-file": "https://ip-admin.example.test/admin/assets/a.js?v=1", "line-number": 12,
        "script-sample": "alert(1)", "disposition": "report",
    }}
    res = await request("POST", "/api/v1/csp-report", content=json.dumps(body),
                        headers={"Content-Type": "application/csp-report"})
    assert res.status_code == 204 and res.content == b""
    [line] = logged(caplog)
    assert "SECRET-TOKEN" not in line and "k=v" not in line and "?v=1" not in line
    assert "document=https://ip-admin.example.test/portal/login" in line
    assert "blocked=https://evil.example " in line, "blocked URLs keep only scheme://host"
    assert "directive=script-src-elem" in line and "disposition=report" in line and "noise=False" in line


@pytest.mark.asyncio
async def test_reporting_api_format_is_logged(no_limit, caplog):
    caplog.set_level(logging.WARNING, logger="csp_report")
    body = [
        {"type": "csp-violation", "body": {"documentURL": "https://h/admin/billing", "effectiveDirective": "img-src",
                                           "blockedURL": "inline", "disposition": "report"}},
        {"type": "deprecation", "body": {"message": "ignored"}},
    ]
    res = await request("POST", "/api/v1/csp-report", content=json.dumps(body),
                        headers={"Content-Type": "application/reports+json"})
    assert res.status_code == 204
    [line] = logged(caplog)
    assert "directive=img-src" in line and "blocked=inline" in line and "document=https://h/admin/billing" in line


@pytest.mark.asyncio
async def test_extension_violations_are_marked_as_noise(no_limit, caplog):
    caplog.set_level(logging.WARNING, logger="csp_report")
    body = {"csp-report": {"document-uri": "https://h/admin", "blocked-uri": "chrome-extension://abc/inject.js",
                           "effective-directive": "script-src-elem"}}
    await request("POST", "/api/v1/csp-report", content=json.dumps(body), headers={"Content-Type": "application/csp-report"})
    [line] = logged(caplog)
    assert "noise=True" in line and "blocked=chrome-extension:" in line


@pytest.mark.asyncio
async def test_control_characters_cannot_forge_log_lines(no_limit, caplog):
    caplog.set_level(logging.WARNING, logger="csp_report")
    body = {"csp-report": {"document-uri": "https://h/admin", "effective-directive": "script-src\ncsp_violation FAKE",
                           "script-sample": "x\r\ny"}}
    await request("POST", "/api/v1/csp-report", content=json.dumps(body), headers={"Content-Type": "application/csp-report"})
    [line] = logged(caplog)
    assert "\n" not in line and "\r" not in line


@pytest.mark.asyncio
@pytest.mark.parametrize("content", [b"not json", b"{}", b"[]", b"null", json.dumps({"csp-report": "x"}).encode(),
                                     b'{"csp-report": {"document-uri": "' + b"a" * (17 * 1024) + b'"}}'])
async def test_junk_and_oversized_bodies_are_ignored_quietly(no_limit, caplog, content):
    caplog.set_level(logging.WARNING, logger="csp_report")
    res = await request("POST", "/api/v1/csp-report", content=content, headers={"Content-Type": "application/csp-report"})
    assert res.status_code == 204
    assert logged(caplog) == []


@pytest.mark.asyncio
async def test_one_request_logs_at_most_twenty_reports(no_limit, caplog):
    caplog.set_level(logging.WARNING, logger="csp_report")
    one = {"type": "csp-violation", "body": {"documentURL": "https://h/admin", "effectiveDirective": "img-src"}}
    await request("POST", "/api/v1/csp-report", content=json.dumps([one] * 50),
                  headers={"Content-Type": "application/reports+json"})
    assert len(logged(caplog)) == csp_report.MAX_REPORTS_PER_REQUEST


@pytest.mark.asyncio
async def test_the_report_endpoint_is_rate_limited_per_ip(monkeypatch):
    seen = []

    async def record(key, bucket, limit=10, window_seconds=60):
        seen.append((bucket, limit, window_seconds))

    monkeypatch.setattr(csp_report, "enforce_rate_limit", record)
    await request("POST", "/api/v1/csp-report", content=b"{}", headers={"Content-Type": "application/csp-report"})
    assert seen == [("csp:report", 60, 60)]
