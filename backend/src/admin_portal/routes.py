from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import FileResponse

router = APIRouter(tags=["admin-portal"])

_ROOT = Path(__file__).resolve().parents[3]
_ADMIN_DIR = _ROOT / "frontend" / "admin" / "routers"
_PUBLIC_DIR = _ROOT / "frontend" / "public"


@router.get("/admin/login")
async def admin_login_page():
    return FileResponse(_ADMIN_DIR / "login.html")


@router.get("/login")
async def legacy_admin_login_page():
    return FileResponse(_ADMIN_DIR / "login.html")


@router.get("/admin/routers")
@router.get("/admin/routers/")
async def admin_router_list_page():
    return FileResponse(_ADMIN_DIR / "list.html")


@router.get("/admin/routers/new")
async def admin_router_wizard_page():
    return FileResponse(_ADMIN_DIR / "wizard.html")


@router.get("/admin/routers/{router_id}")
async def admin_router_detail_page(router_id: str):
    return FileResponse(_ADMIN_DIR / "detail.html")


@router.get("/apply")
async def operator_apply_page():
    return FileResponse(_PUBLIC_DIR / "apply.html")
