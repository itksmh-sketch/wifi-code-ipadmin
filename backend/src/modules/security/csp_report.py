"""POST /api/v1/csp-report: where browsers send Content-Security-Policy
violations (Report-Only during rollout, and enforced ones after).

Accepts both report formats browsers use:
  * application/csp-report   — report-uri: {"csp-report": {"document-uri": ...}}
  * application/reports+json — report-to (Reporting API): a list of
    {"type": "csp-violation", "body": {"documentURL": ...}}

Each violation becomes one structured log line (logger "csp_report"), which is
how Report-Only is judged clean: grep the backend logs for csp_violation. No
database, nothing rendered anywhere.

Unauthenticated by necessity, so everything in a report is untrusted: bodies
are capped, the rate is limited per IP, every field is truncated with control
characters removed (no log injection), and URLs lose their query string and
fragment (a portal page URL can carry an rt token, a reset link a grant).
Always 204, so a browser never retries.
"""
from __future__ import annotations

import json
import logging
import re
from urllib.parse import urlsplit

from fastapi import APIRouter, Request, Response

from src.middleware.rate_limit import enforce_rate_limit

router = APIRouter(tags=["security"])
logger = logging.getLogger("csp_report")

MAX_BODY_BYTES = 16 * 1024
MAX_REPORTS_PER_REQUEST = 20
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
# Keyword values browsers put in blocked-uri instead of a URL.
_KEYWORDS = {"inline", "eval", "wasm-eval", "data", "blob", "self", "trusted-types-policy", "trusted-types-sink"}


def _clean(value, limit: int = 200) -> str:
    return _CONTROL.sub("", str(value if value is not None else ""))[:limit]


def _url(value, *, origin_only: bool = False) -> str:
    """Path without query or fragment; for blocked URLs only scheme://host."""
    raw = _clean(value, 2000)
    if not raw or raw in _KEYWORDS or ":" not in raw:
        return raw[:200]
    parts = urlsplit(raw)
    if parts.scheme in ("http", "https", "ws", "wss"):
        if origin_only:
            return f"{parts.scheme}://{parts.netloc}"[:200]
        return f"{parts.scheme}://{parts.netloc}{parts.path}"[:200]
    return f"{parts.scheme}:"[:200]  # data:, blob:, chrome-extension: ...


def _normalise(report: dict) -> dict | None:
    """One violation, from either format, as the fields we log."""
    if not isinstance(report, dict):
        return None
    if isinstance(report.get("csp-report"), dict):  # report-uri
        r = report["csp-report"]
        get = lambda *names: next((r.get(n) for n in names if r.get(n) is not None), None)  # noqa: E731
        return {
            "document": _url(get("document-uri")),
            "directive": _clean(get("effective-directive", "violated-directive"), 80),
            "blocked": _url(get("blocked-uri"), origin_only=True),
            "source": _url(get("source-file")),
            "line": _clean(get("line-number"), 10),
            "sample": _clean(get("script-sample"), 60),
            "disposition": _clean(get("disposition") or "enforce", 12),
        }
    if report.get("type") == "csp-violation" and isinstance(report.get("body"), dict):  # report-to
        b = report["body"]
        return {
            "document": _url(b.get("documentURL")),
            "directive": _clean(b.get("effectiveDirective"), 80),
            "blocked": _url(b.get("blockedURL"), origin_only=True),
            "source": _url(b.get("sourceFile")),
            "line": _clean(b.get("lineNumber"), 10),
            "sample": _clean(b.get("sample"), 60),
            "disposition": _clean(b.get("disposition") or "enforce", 12),
        }
    return None


def is_extension_noise(v: dict) -> bool:
    """Violations caused by browser extensions, not by our pages."""
    prefixes = ("chrome-extension:", "moz-extension:", "safari-web-extension:", "safari-extension:")
    return any((v.get(k) or "").startswith(prefixes) for k in ("blocked", "source"))


@router.post("/api/v1/csp-report", status_code=204, include_in_schema=False)
async def csp_report(request: Request) -> Response:
    client_ip = request.client.host if request.client else "unknown"
    await enforce_rate_limit(client_ip, "csp:report", limit=60, window_seconds=60)

    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > MAX_BODY_BYTES:
        return Response(status_code=204)
    body = b""
    async for chunk in request.stream():
        body += chunk
        if len(body) > MAX_BODY_BYTES:
            return Response(status_code=204)
    try:
        data = json.loads(body or b"null")
    except (ValueError, UnicodeDecodeError):
        return Response(status_code=204)

    reports = data if isinstance(data, list) else [data]
    for report in reports[:MAX_REPORTS_PER_REQUEST]:
        v = _normalise(report)
        if v is None:
            continue
        logger.warning(
            "csp_violation disposition=%s directive=%s blocked=%s document=%s source=%s line=%s sample=%r noise=%s ip=%s",
            v["disposition"], v["directive"], v["blocked"], v["document"], v["source"], v["line"],
            v["sample"], is_extension_noise(v), _clean(client_ip, 45),
        )
    return Response(status_code=204)
