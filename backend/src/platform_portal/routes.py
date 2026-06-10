from __future__ import annotations

import os
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

router = APIRouter(tags=["platform-portal"])

_DIR = Path(__file__).resolve().parent


@router.get("/platform/login", include_in_schema=False)
async def platform_login_page():
    return FileResponse(_DIR / "login.html")


@router.get("/platform/operators/new", include_in_schema=False)
async def platform_operators_new_page():
    return FileResponse(_DIR / "operators_new.html")


@router.get("/platform/operators", include_in_schema=False)
async def platform_operators_page():
    return FileResponse(_DIR / "operators.html")


@router.get("/platform", include_in_schema=False)
async def platform_root_redirect():
    from fastapi.responses import RedirectResponse
    return RedirectResponse(url="/platform/operators", status_code=302)


@router.get("/platform/statics/{file_path:path}", include_in_schema=False)
async def platform_statics(file_path: str):
    full = _DIR / "statics" / file_path
    if full.resolve().is_relative_to((_DIR / "statics").resolve()) and full.exists():
        return FileResponse(full)
    raise HTTPException(status_code=404, detail="Not found")
