"""页面入口与健康检查：落地页 /、应用页 /app、/api/health。"""
import os
from datetime import datetime

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, RedirectResponse

from api.auth import get_current_user

router = APIRouter()

_STATIC_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "static")


@router.get("/")
async def index():
    """返回欢迎落地页。"""
    return FileResponse(os.path.join(_STATIC_DIR, "index.html"))


@router.get("/app")
async def app_page(request: Request):
    """主应用：未登录重定向到落地页。"""
    if not get_current_user(request):
        return RedirectResponse("/", status_code=302)
    return FileResponse(os.path.join(_STATIC_DIR, "app.html"))


@router.get("/api/health")
async def health():
    """健康检查。"""
    return {"status": "ok", "timestamp": datetime.now().isoformat()}
