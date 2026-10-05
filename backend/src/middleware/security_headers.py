"""Security headers for every response, chosen by path prefix.

One table (AREAS) says what each part of the site gets. The token-holding
areas (/admin, /platform, /reseller, /apply) and the captive portal share one
origin, so a script injected into any of them could read the others' tokens;
these headers are the backstop if escaping ever slips again.

Rollout (see the CSP plan): every non-CSP header below is enforced now. CSP
is enforced on JSON API responses, and is Report-Only on /admin. /platform,
/reseller and /apply get a hash-based Report-Only CSP from their page routes
(csp_pages.py), since the hashes are per page; the portal's three pages get a
nonce-based Report-Only CSP from their routes (phase C). Violations are
POSTed to /api/v1/csp-report.

Headers a route sets itself win (the payment-callback page has its own
stricter CSP): this only fills in what is missing.

A pure ASGI middleware rather than BaseHTTPMiddleware, so it only edits the
response start message and never buffers a body (FileResponse, streaming).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

CSP_REPORT_PATH = "/api/v1/csp-report"
CSP_REPORT_GROUP = "csp-endpoint"

# Sent on every response.
BASE_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "strict-origin-when-cross-origin",
    # Nothing on the site uses these; denying them means injected script can't either.
    "Permissions-Policy": "camera=(), microphone=(), geolocation=(), payment=(), usb=()",
}


def _policy(*directives: str) -> str:
    return "; ".join(directives)


ADMIN_CSP = _policy(
    "default-src 'self'",
    "script-src 'self'",
    "style-src 'self'",
    # blob: for the logo preview before upload.
    "img-src 'self' blob:",
    "connect-src 'self'",
    "font-src 'self'",
    # The branding page's phone preview frames /portal/login (same origin).
    "frame-src 'self'",
    "object-src 'none'",
    "base-uri 'none'",
    "form-action 'self'",
    "frame-ancestors 'none'",
)

# JSON has no business being rendered as a document, framed, or running anything.
API_CSP = _policy("default-src 'none'", "frame-ancestors 'none'")


@dataclass(frozen=True)
class Area:
    frame_options: str = "DENY"
    # same-origin on the token-holding areas: a window this page opens, or that
    # opens it, gets no handle on it.
    coop: Optional[str] = None
    csp: Optional[str] = None             # enforced
    csp_report_only: Optional[str] = None
    csp_on_html_only: bool = True


TOKEN_AREA = Area(coop="same-origin")

# First match wins; a prefix matches the path itself or anything under it as a
# whole segment, so "/platform" does not match "/platform-ui".
AREAS: list[tuple[str, Area]] = [
    # Every API response, JSON or not. The one HTML route under /api (the
    # payment callback) sets its own stricter CSP, which wins.
    ("/api", Area(csp=API_CSP, csp_on_html_only=False)),
    ("/admin", Area(coop="same-origin", csp_report_only=ADMIN_CSP)),
    ("/platform", TOKEN_AREA),
    ("/reseller", TOKEN_AREA),
    ("/apply", TOKEN_AREA),
    # Framed by the admin's branding preview, so same-origin framing stays
    # allowed. No COOP: captive-portal browsers and the hotspot hand-off must
    # be left alone until the real-device check (phase C).
    ("/portal", Area(frame_options="SAMEORIGIN")),
]
DEFAULT_AREA = Area()


def area_for(path: str) -> Area:
    for prefix, area in AREAS:
        if path == prefix or path.startswith(prefix + "/"):
            return area
    return DEFAULT_AREA


def _request_headers(scope) -> dict[str, str]:
    return {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers", [])}


def is_https(scope) -> bool:
    # nginx's HTTPS site (ip-admin.duckdns.org) sends X-Forwarded-Proto; the
    # hotspot site on the bare IP sends none, so that origin is plain http.
    proto = _request_headers(scope).get("x-forwarded-proto") or scope.get("scheme", "http")
    return proto.split(",")[0].strip().lower() == "https"


def report_suffix(scope) -> str:
    """report-uri for every browser; report-to (Reporting API) only on HTTPS.
    The Reporting API ignores an http:// endpoint, and a browser that honours
    report-to may then skip report-uri, so on the plain-http IP origin (the
    captive portal) the page keeps report-uri alone."""
    suffix = f"; report-uri {CSP_REPORT_PATH}"
    if is_https(scope):
        suffix += f"; report-to {CSP_REPORT_GROUP}"
    return suffix


def reporting_endpoints(scope) -> str:
    # Reporting-Endpoints wants an absolute, secure URL. Built from the
    # request, so it names whichever HTTPS host the page was served from;
    # empty on plain http (see report_suffix).
    host = _request_headers(scope).get("host", "")
    if not host or not is_https(scope):
        return ""
    return f'{CSP_REPORT_GROUP}="https://{host}{CSP_REPORT_PATH}"'


def headers_for(path: str, content_type: str, scope) -> dict[str, str]:
    area = area_for(path)
    out = dict(BASE_HEADERS)
    out["X-Frame-Options"] = area.frame_options
    if area.coop:
        out["Cross-Origin-Opener-Policy"] = area.coop
    is_html = content_type.split(";", 1)[0].strip().lower() == "text/html"
    if is_html or not area.csp_on_html_only:
        if area.csp:
            out["Content-Security-Policy"] = area.csp
        if area.csp_report_only:
            out["Content-Security-Policy-Report-Only"] = area.csp_report_only + report_suffix(scope)
            endpoints = reporting_endpoints(scope)
            if endpoints:
                out["Reporting-Endpoints"] = endpoints
    return out


class SecurityHeadersMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        path = scope.get("path", "")

        async def send_with_headers(message):
            if message["type"] == "http.response.start":
                existing = list(message.get("headers", []))
                present = {k.decode("latin-1").lower() for k, _ in existing}
                content_type = next(
                    (v.decode("latin-1") for k, v in existing if k.decode("latin-1").lower() == "content-type"), ""
                )
                for name, value in headers_for(path, content_type, scope).items():
                    if name.lower() not in present:
                        existing.append((name.encode("latin-1"), value.encode("latin-1")))
                message = {**message, "headers": existing}
            await send(message)

        await self.app(scope, receive, send_with_headers)
