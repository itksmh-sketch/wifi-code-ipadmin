from fastapi import FastAPI, Depends, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from redis.asyncio import Redis
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession
from contextlib import asynccontextmanager
import os

from src.db.base import get_db
from src.db.models import Router, Session, Site, Voucher
from src.config import get_settings
from src.logging_config import configure_logging
from src.schemas import DashboardSummary

# Import routers
from src.modules.auth.routes import router as auth_router
from src.modules.towns.routes import router as towns_router
from src.modules.routers.routes import router as routers_router
from src.modules.plans.routes import router as plans_router
from src.modules.vouchers.routes import router as vouchers_router
from src.modules.sessions.routes import router as sessions_router
from src.modules.payments.routes import router as payments_router
from src.modules.payments.credentials_routes import router as payment_credentials_router
from src.modules.webhooks.routes import router as webhooks_router
from src.radius.routes import router as radius_router
from src.portal.routes import router as portal_router
from src.middleware.auth import TenantContext, get_admin_tenant_context
from src.modules.sms.dependencies import get_sms_service
from src.modules.resellers.routes import router as reseller_router
from src.modules.resellers.admin_routes import router as admin_reseller_router
from src.modules.mikrotik.routes import router as mikrotik_router
from src.modules.platform.routes import router as platform_router
from src.admin_portal.routes import router as admin_portal_router
from src.reseller_portal.routes import router as reseller_portal_router
from src.platform_portal.routes import router as platform_portal_router
from src.modules.applications.routes import public_router as applications_public_router
from src.modules.applications.routes import platform_router as applications_platform_router
from src.modules.billing.routes import router as billing_router
from src.modules.webhooks.platform_billing import router as platform_billing_webhook_router

settings = get_settings()


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Initialize SMS service once so misconfig is logged at startup.
    configure_logging()
    get_sms_service()
    yield


app = FastAPI(
    title="ISP Hotspot Voucher & Billing System",
    version="1.0.0",
    lifespan=lifespan,
)

# CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Include routers
app.include_router(auth_router, prefix="/api/v1")
app.include_router(towns_router, prefix="/api/v1")
app.include_router(routers_router, prefix="/api/v1")
app.include_router(plans_router, prefix="/api/v1")
app.include_router(vouchers_router, prefix="/api/v1")
app.include_router(sessions_router, prefix="/api/v1")
app.include_router(payments_router, prefix="/api/v1")
app.include_router(payment_credentials_router, prefix="/api/v1")
app.include_router(radius_router, prefix="/api/v1")
app.include_router(webhooks_router)
app.include_router(portal_router)
app.include_router(reseller_portal_router)
app.include_router(reseller_router, prefix="/api/v1")
app.include_router(admin_reseller_router, prefix="/api/v1")
app.include_router(mikrotik_router, prefix="/api/v1")
app.include_router(platform_router, prefix="/api/v1")
app.include_router(admin_portal_router)
app.include_router(platform_portal_router)
app.include_router(applications_public_router, prefix="/api/v1")
app.include_router(applications_platform_router, prefix="/api/v1")
app.include_router(billing_router, prefix="/api/v1")
app.include_router(platform_billing_webhook_router)


@app.get("/api/v1/health")
async def health_check():
    return {"status": "ok", "version": "1.0.0"}


@app.get("/health")
async def production_health(db: AsyncSession = Depends(get_db)):
    db_status = "ok"
    redis_status = "ok"

    try:
        await db.execute(text("SELECT 1"))
    except Exception:
        db_status = "error"

    redis = Redis.from_url(settings.redis_url)
    try:
        pong = await redis.ping()
        if not pong:
            redis_status = "error"
    except Exception:
        redis_status = "error"
    finally:
        await redis.aclose()

    status_code = "ok" if db_status == "ok" and redis_status == "ok" else "degraded"
    return {"status": status_code, "db": db_status, "redis": redis_status}


@app.get("/api/v1/dashboard", response_model=DashboardSummary)
async def get_dashboard_summary(db: AsyncSession = Depends(get_db), tenant: TenantContext = Depends(get_admin_tenant_context)):
    operator_id = tenant.isp_operator_id
    total_vouchers = (await db.execute(select(func.count()).select_from(Voucher).where(Voucher.isp_operator_id == operator_id))).scalar() or 0
    active_vouchers = (await db.execute(select(func.count()).select_from(Voucher).where(Voucher.isp_operator_id == operator_id, Voucher.status == "active"))).scalar() or 0
    expired_vouchers = (await db.execute(select(func.count()).select_from(Voucher).where(Voucher.isp_operator_id == operator_id, Voucher.status == "expired"))).scalar() or 0
    exhausted_vouchers = (await db.execute(select(func.count()).select_from(Voucher).where(Voucher.isp_operator_id == operator_id, Voucher.status == "exhausted"))).scalar() or 0
    disabled_vouchers = (await db.execute(select(func.count()).select_from(Voucher).where(Voucher.isp_operator_id == operator_id, Voucher.status == "disabled"))).scalar() or 0
    active_sessions = (await db.execute(select(func.count()).select_from(Session).where(Session.isp_operator_id == operator_id, Session.stopped_at.is_(None)))).scalar() or 0
    total_sessions = (await db.execute(select(func.count()).select_from(Session).where(Session.isp_operator_id == operator_id))).scalar() or 0
    active_sites = (await db.execute(select(func.count()).select_from(Site).where(Site.isp_operator_id == operator_id))).scalar() or 0
    offline_routers_count = (
        await db.execute(
            select(func.count()).select_from(Router).where(
                Router.is_active == True,  # noqa: E712
                Router.is_online == False,  # noqa: E712
                Router.isp_operator_id == operator_id,
            )
        )
    ).scalar() or 0

    return DashboardSummary(
        total_vouchers=total_vouchers,
        active_vouchers=active_vouchers,
        expired_vouchers=expired_vouchers,
        exhausted_vouchers=exhausted_vouchers,
        disabled_vouchers=disabled_vouchers,
        active_sessions=active_sessions,
        total_sessions=total_sessions,
        active_sites=active_sites,
        total_sites=active_sites,
        offline_routers_count=offline_routers_count,
    )


# Serve React admin SPA (production build output: backend/static/admin/)
_ADMIN_STATIC = os.path.join(os.path.dirname(__file__), "..", "static", "admin")
_ADMIN_ASSETS = os.path.join(_ADMIN_STATIC, "assets")

if os.path.exists(_ADMIN_ASSETS):
    app.mount("/admin/assets", StaticFiles(directory=_ADMIN_ASSETS), name="admin-assets")

if os.path.exists(_ADMIN_STATIC):
    @app.get("/admin/{full_path:path}", include_in_schema=False)
    async def serve_admin_spa(full_path: str):
        index = os.path.join(_ADMIN_STATIC, "index.html")
        if os.path.exists(index):
            return FileResponse(index)
        return {"error": "Admin dashboard not built yet. Run: cd frontend && npm run build"}
