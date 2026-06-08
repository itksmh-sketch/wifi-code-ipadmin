import os

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.templating import Jinja2Templates

router = APIRouter(tags=["reseller-portal"])

# Serve reseller portal files from a directory that exists inside the backend container.
# The backend container only mounts ./backend -> /app, so keep reseller assets under /app.
RESELLER_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "src", "reseller")
templates = Jinja2Templates(directory=RESELLER_DIR)


@router.get("/reseller/login", response_class=HTMLResponse)
async def reseller_login_page(request: Request):
    return templates.TemplateResponse("login.html", {"request": request})


@router.get("/reseller/dashboard", response_class=HTMLResponse)
async def reseller_dashboard_page(request: Request):
    return templates.TemplateResponse("dashboard.html", {"request": request})


@router.get("/reseller/buy", response_class=HTMLResponse)
async def reseller_buy_page(request: Request):
    return templates.TemplateResponse("buy.html", {"request": request})


@router.get("/reseller/vouchers", response_class=HTMLResponse)
async def reseller_vouchers_page(request: Request):
    return templates.TemplateResponse("vouchers.html", {"request": request})


@router.get("/reseller/wallet", response_class=HTMLResponse)
async def reseller_wallet_page(request: Request):
    return templates.TemplateResponse("wallet.html", {"request": request})


@router.get("/reseller/sales", response_class=HTMLResponse)
async def reseller_sales_page(request: Request):
    return templates.TemplateResponse("sales.html", {"request": request})


@router.get("/reseller/print", response_class=HTMLResponse)
async def reseller_print_page(request: Request):
    return templates.TemplateResponse("print.html", {"request": request})


@router.get("/reseller/statics/{file_path:path}")
async def reseller_statics(file_path: str):
    full_path = os.path.join(RESELLER_DIR, "statics", file_path)
    if os.path.exists(full_path):
        return FileResponse(full_path)
    raise HTTPException(status_code=404, detail="File not found")

