import os

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.templating import Jinja2Templates

from src.middleware.csp_pages import page_csp_headers

router = APIRouter(tags=["reseller-portal"])

# Serve reseller portal files from a directory that exists inside the backend container.
# The backend container only mounts ./backend -> /app, so keep reseller assets under /app.
RESELLER_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "src", "reseller")
templates = Jinja2Templates(directory=RESELLER_DIR)


def _page(name: str, request: Request):
    # The hash-based CSP comes from the template file itself (no Jinja syntax
    # in these pages, so the file is exactly what gets served).
    return templates.TemplateResponse(request, name,
                                      headers=page_csp_headers(os.path.join(RESELLER_DIR, name), request))


@router.get("/reseller/login", response_class=HTMLResponse)
async def reseller_login_page(request: Request):
    return _page("login.html", request)


@router.get("/reseller/dashboard", response_class=HTMLResponse)
async def reseller_dashboard_page(request: Request):
    return _page("dashboard.html", request)


@router.get("/reseller/buy", response_class=HTMLResponse)
async def reseller_buy_page(request: Request):
    return _page("buy.html", request)


@router.get("/reseller/vouchers", response_class=HTMLResponse)
async def reseller_vouchers_page(request: Request):
    return _page("vouchers.html", request)


@router.get("/reseller/wallet", response_class=HTMLResponse)
async def reseller_wallet_page(request: Request):
    return _page("wallet.html", request)


@router.get("/reseller/sales", response_class=HTMLResponse)
async def reseller_sales_page(request: Request):
    return _page("sales.html", request)


@router.get("/reseller/print", response_class=HTMLResponse)
async def reseller_print_page(request: Request):
    return _page("print.html", request)


@router.get("/reseller/statics/{file_path:path}")
async def reseller_statics(file_path: str):
    full_path = os.path.join(RESELLER_DIR, "statics", file_path)
    if os.path.exists(full_path):
        return FileResponse(full_path)
    raise HTTPException(status_code=404, detail="File not found")

