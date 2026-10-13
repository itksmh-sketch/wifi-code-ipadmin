from __future__ import annotations

import os
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse

from src.middleware.csp_pages import serve_page

router = APIRouter(tags=["platform-portal"])

_DIR = Path(__file__).resolve().parent


@router.get("/platform/login", include_in_schema=False)
async def platform_login_page(request: Request):
    return serve_page(_DIR / "login.html", request)


@router.get("/platform/setup", include_in_schema=False)
async def platform_setup_page(request: Request):
    """Security setup wizard. Every other signed-in page redirects here (via
    statics/setup-guard.js) while the owner's setup is pending."""
    return serve_page(_DIR / "setup.html", request)


@router.get("/platform/operators/new", include_in_schema=False)
async def platform_operators_new_page(request: Request):
    return serve_page(_DIR / "operators_new.html", request)


@router.get("/platform/operators", include_in_schema=False)
async def platform_operators_page(request: Request):
    return serve_page(_DIR / "operators.html", request)


# Registered after /platform/operators/new so the literal path keeps winning —
# FastAPI matches in declaration order and "new" would otherwise bind to operator_id.
@router.get("/platform/operators/{operator_id}", include_in_schema=False)
async def platform_operator_detail_page(operator_id: str, request: Request):
    return serve_page(_DIR / "operator_detail.html", request)


@router.get("/platform/settings", include_in_schema=False)
async def platform_settings_page(request: Request):
    return serve_page(_DIR / "settings.html", request)


@router.get("/platform/notification-templates", include_in_schema=False)
async def platform_notification_templates_page(request: Request):
    return serve_page(_DIR / "notification_templates.html", request)


@router.get("/platform/billing", include_in_schema=False)
async def platform_billing_page(request: Request):
    return serve_page(_DIR / "billing.html", request)


@router.get("/platform/providers", include_in_schema=False)
async def platform_providers_page(request: Request):
    return serve_page(_DIR / "providers.html", request)


@router.get("/platform/transactions", include_in_schema=False)
async def platform_transactions_page(request: Request):
    return serve_page(_DIR / "transactions.html", request)


@router.get("/platform/health", include_in_schema=False)
async def platform_health_page(request: Request):
    return serve_page(_DIR / "health.html", request)


@router.get("/platform/analytics", include_in_schema=False)
async def platform_analytics_page(request: Request):
    return serve_page(_DIR / "analytics.html", request)


@router.get("/platform/audit", include_in_schema=False)
async def platform_audit_page(request: Request):
    """The audit log. No inline script or style at all: everything is in
    statics/audit.js and statics/audit.css."""
    return serve_page(_DIR / "audit.html", request)


@router.get("/platform/styleguide", include_in_schema=False)
async def platform_styleguide_page(request: Request):
    """Tokens and components from statics/app.css. Gated client-side like every
    other signed-in page: it renders nothing until /api/v1/platform/me accepts
    the platform-owner token."""
    return serve_page(_DIR / "styleguide.html", request)


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
