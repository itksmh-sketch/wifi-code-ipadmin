from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import FileResponse, RedirectResponse

router = APIRouter(tags=["admin-portal"])

# parents[2] = backend/ root — works both on host and in Docker (/app)
_BACKEND_DIR = Path(__file__).resolve().parents[2]
_STATIC_DIR = _BACKEND_DIR / "static"


@router.get("/login")
async def legacy_login_redirect():
    return RedirectResponse(url="/admin/login", status_code=301)


@router.get("/apply")
async def operator_apply_page():
    return FileResponse(_STATIC_DIR / "apply.html")
