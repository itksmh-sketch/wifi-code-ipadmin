from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse, RedirectResponse

router = APIRouter(tags=["admin-portal"])

# parents[2] = backend/ root — works both on host and in Docker (/app)
_BACKEND_DIR = Path(__file__).resolve().parents[2]
_STATIC_DIR = _BACKEND_DIR / "static"
_PLATFORM_UI_DIR = (_STATIC_DIR / "platform-ui").resolve()


@router.get("/platform-ui/{file_path:path}", include_in_schema=False)
async def platform_ui_asset(file_path: str):
    # Platform identity assets (tokens.css, auth.css, icons.svg), shared by the
    # vanilla apply page and the React admin SPA — one copy, no build step.
    full = (_PLATFORM_UI_DIR / file_path).resolve()
    if full.is_relative_to(_PLATFORM_UI_DIR) and full.is_file():
        return FileResponse(full)
    raise HTTPException(status_code=404, detail="Not found")


@router.get("/login")
async def legacy_login_redirect():
    return RedirectResponse(url="/admin/login", status_code=301)


@router.get("/apply")
async def operator_apply_page():
    return FileResponse(_STATIC_DIR / "apply.html")
