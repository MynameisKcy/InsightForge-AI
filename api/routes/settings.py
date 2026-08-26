"""账号设置路由（需求①：配置管理 + 热重载）：/api/settings 系列。

user_settings_db 经属主模块 ``database.user_settings_db`` 动态解析（而非
from-import 固定绑定）：tests 以 importlib.reload + 重绑模块属性的方式换库，
动态属主查找与其对齐（见 test_settings_api._fresh_settings）。
"""
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

import database.user_settings_db as usd
from api import deps
from api.auth import require_auth
from api.errors import error_response
from model.factory import reload_model_config
from utils.logger_handler import logger

router = APIRouter()


@router.get("/api/settings/status")
async def get_settings_status(request: Request, user=Depends(require_auth)):
    """返回当前用户是否已配置。前端登录后据此决定是否弹提示。"""
    user_id = user["user_id"]
    return {"configured": usd.user_settings_db.has(user_id), "authed": True}


@router.get("/api/settings")
async def get_settings(request: Request, user=Depends(require_auth)):
    """返回当前用户配置（API Key 掩码）。未登录 401，未配置返回 null。"""
    user_id = user["user_id"]
    data = usd.user_settings_db.get_masked(user_id)
    if data is None:
        return {"configured": False, "settings": None}
    return {"configured": True, "settings": data}


@router.post("/api/settings")
async def save_settings(request: Request, user=Depends(require_auth)):
    """保存用户配置并触发热重载。前端回传掩码值时不覆盖已存明文 key。"""
    user_id = user["user_id"]
    try:
        body = await request.json()
    except Exception:
        return error_response("请求体不是有效 JSON", 400)
    allowed = {"llm_api_key", "llm_model_name", "embedding_model_name", "llm_base_url",
               "vector_db_host", "vector_db_port", "vector_db_collection",
               "vector_db_tenant", "local_db_conn"}
    cleaned = {k: v for k, v in body.items() if k in allowed}
    # 前端回传掩码值（含 ****）：不覆盖已存明文 key
    if "****" in str(cleaned.get("llm_api_key", "")):
        cleaned.pop("llm_api_key", None)
        existing = usd.user_settings_db.get(user_id) or {}
        if existing.get("llm_api_key"):
            cleaned["llm_api_key"] = existing["llm_api_key"]
    try:
        usd.user_settings_db.upsert(user_id, cleaned)
        reload_model_config(user_id)            # 失效该用户模型缓存
        deps._invalidate_user_agents(user_id)   # 丢弃该用户 Agent 实例，下次按新配置重建
        return {"ok": True}
    except Exception as e:
        logger.exception("保存配置失败")
        return error_response(f"保存失败: {e}", 500)
