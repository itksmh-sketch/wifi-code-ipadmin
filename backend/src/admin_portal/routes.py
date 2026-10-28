from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from src.admin_portal.public_values import get_public_values
from src.middleware.csp_pages import page_csp_headers, serve_page

router = APIRouter(tags=["admin-portal"])

# parents[2] = backend/ root — works both on host and in Docker (/app)
_BACKEND_DIR = Path(__file__).resolve().parents[2]
_STATIC_DIR = _BACKEND_DIR / "static"
_PLATFORM_UI_DIR = (_STATIC_DIR / "platform-ui").resolve()
_templates = Jinja2Templates(directory=_STATIC_DIR)


@router.get("/platform-ui/{file_path:path}", include_in_schema=False)
async def platform_ui_asset(file_path: str):
    # Platform identity assets (tokens.css, auth.css, icons.svg), shared by the
    # vanilla apply page and the React admin SPA — one copy, no build step.
    full = (_PLATFORM_UI_DIR / file_path).resolve()
    if full.is_relative_to(_PLATFORM_UI_DIR) and full.is_file():
        return FileResponse(full)
    raise HTTPException(status_code=404, detail="Not found")


# Served only when the public values have never loaded (database down since
# the process started). No inline style or script, like the pages themselves.
_UNAVAILABLE_PAGE = (
    '<!DOCTYPE html><html lang="en"><head><meta charset="UTF-8">'
    '<meta name="viewport" content="width=device-width, initial-scale=1.0">'
    "<title>IpAdmin</title></head><body><h1>IpAdmin</h1>"
    "<p>This page is temporarily unavailable. Please try again in a minute.</p>"
    '<p><a href="/admin/login">Operator sign in</a></p></body></html>'
)


async def _public_page(name: str, request: Request):
    """Render a public page with the fee, trial and support email a new
    operator gets today (see public_values.py for where they come from and how
    they are cached). Jinja autoescapes .html, and the pages have no inline
    script or style, so the file's CSP needs no hashes. no-cache: browsers
    re-check every time, so a price change is never stuck in one.
    """
    values = await get_public_values()
    if values is None:
        return HTMLResponse(_UNAVAILABLE_PAGE, status_code=503,
                            headers={"Retry-After": "30", "Cache-Control": "no-store"})
    headers = page_csp_headers(_STATIC_DIR / name, request)
    headers["Cache-Control"] = "no-cache"
    return _templates.TemplateResponse(request, name, {
        "fee": values.fee,
        "trial_days": values.trial_days,
        "support_email": values.support_email,
        "app_url": values.app_url,
    }, headers=headers)


@router.get("/", include_in_schema=False)
async def landing_page(request: Request):
    # Public product page: features, pricing, contact. No tokens.
    return await _public_page("index.html", request)


@router.get("/guides", include_in_schema=False)
async def docs_page(request: Request):
    # Operator tutorials. Not /docs: that path stays a 404 so the switched-off
    # API docs can't quietly return (tests/test_api_docs_disabled.py).
    return await _public_page("docs.html", request)


@router.get("/login")
async def legacy_login_redirect():
    return RedirectResponse(url="/admin/login", status_code=301)


@router.get("/apply")
async def operator_apply_page(request: Request):
    return serve_page(_STATIC_DIR / "apply.html", request)
