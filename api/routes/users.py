"""用户账号路由：注册/登录/登出/当前用户信息/昵称/改密。"""
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from api.auth import (
    AUTH_COOKIE_NAME,
    extract_token,
    invalidate_token,
    require_auth,
    set_auth_cookie,
)
from database.user_db import user_db
from utils.logger_handler import logger

router = APIRouter()


@router.post("/api/register")
async def api_register(request: Request):
    """用户注册。注册成功后自动登录并返回 token。"""
    body = await request.json()
    account = body.get("account", "").strip()
    password = body.get("password", "")
    result = user_db.register(account, password)
    if result.get("success"):
        # 注册成功后自动登录
        try:
            login_result = user_db.login(account, password)
            if login_result.get("success"):
                resp = JSONResponse(content={
                    "success": True,
                    "user_id": login_result.get("user_id"),
                    "account": login_result.get("account"),
                    "token": login_result.get("token"),
                })
                # 注册后自动登录：同样写入 cookie，保证随后导航到 /app 正常
                set_auth_cookie(resp, login_result.get("token"), remember=False)
                return resp
            else:
                return JSONResponse(content={
                    "success": True,
                    "message": "注册成功，但自动登录失败，请手动登录",
                })
        except Exception as e:
            logger.error(f"Auto-login after registration failed: {e}")
            return JSONResponse(content={
                "success": True,
                "message": "注册成功，请手动登录",
            })
    return JSONResponse(content=result, status_code=400)


@router.post("/api/login")
async def api_login(request: Request):
    """用户登录。返回 token。"""
    body = await request.json()
    account = body.get("account", "").strip()
    password = body.get("password", "")
    remember = bool(body.get("remember", False))
    result = user_db.login(account, password)
    if result.get("success"):
        resp = JSONResponse(content=result)
        # 关键修复：写入 token cookie，使随后导航到 /app 能被服务端识别为已登录，
        # 避免 /app 持续 302 回落地页形成重定向死循环。
        set_auth_cookie(resp, result["token"], remember)
        return resp
    return JSONResponse(content=result, status_code=401)


@router.post("/api/logout")
async def api_logout(request: Request):
    """用户登出。"""
    token = request.headers.get("Authorization", "").replace("Bearer ", "")
    if not token:
        token = request.cookies.get(AUTH_COOKIE_NAME, "")
    if token:
        user_db.logout(token)
        invalidate_token(token)
    resp = JSONResponse(content={"success": True})
    # 清除会话 cookie，避免登出后仍能凭 cookie 通过 /app 导航鉴权
    resp.delete_cookie(AUTH_COOKIE_NAME, path="/")
    return resp


@router.get("/api/me")
async def api_me(user=Depends(require_auth)):
    """返回当前登录用户信息。未登录 401。"""
    return JSONResponse({
        "user_id": user["user_id"],
        "account": user["account"],
        "nickname": user.get("nickname"),
    })


# ── 用户个人信息（昵称 / 密码） ──


@router.get("/api/profile")
async def api_get_profile(request: Request, user=Depends(require_auth)):
    """返回当前用户个人信息：account、昵称。"""
    user_id = user["user_id"]
    user_info = user_db.get_user(user_id) or {}
    return JSONResponse(content={
        "user_id": user_id,
        "account": user_info.get("account", ""),
        "nickname": user_info.get("nickname") or "",
    })


@router.post("/api/profile")
async def api_update_profile(request: Request, user=Depends(require_auth)):
    """更新昵称。body: {"nickname": "..."}"""
    user_id = user["user_id"]
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "请求体不是有效 JSON"}, status_code=400)
    nickname = (body.get("nickname") or "").strip()
    if len(nickname) > 30:
        nickname = nickname[:30]
    user_db.update_profile(user_id, nickname=nickname)
    # 失效短缓存，使后续 /api/me 立即读到新昵称
    invalidate_token(extract_token(request))
    return JSONResponse(content={"ok": True, "nickname": nickname})


@router.post("/api/password")
async def api_change_password(request: Request, user=Depends(require_auth)):
    """修改密码。body: {"old_password": "...", "new_password": "..."}"""
    user_id = user["user_id"]
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "请求体不是有效 JSON"}, status_code=400)
    result = user_db.change_password(
        user_id, body.get("old_password", ""), body.get("new_password", "")
    )
    if not result.get("success"):
        return JSONResponse(result, status_code=400)
    # 改密成功后失效短缓存，强制后续请求重新查库
    invalidate_token(extract_token(request))
    return JSONResponse(result)
