from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import FileResponse, RedirectResponse

router = APIRouter(tags=["admin-portal"])

_ROOT = Path(__file__).resolve().parents[3]
_PUBLIC_DIR = _ROOT / "frontend" / "public"


@router.get("/login")
async def legacy_login_redirect():
    return RedirectResponse(url="/admin/login", status_code=301)


@router.get("/apply")
async def operator_apply_page():
    return FileResponse(_PUBLIC_DIR / "apply.html")
