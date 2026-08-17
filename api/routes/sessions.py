"""会话管理路由：/api/sessions CRUD + 遗留 /api/conversation/history。"""
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from api import deps
from api.auth import require_auth

router = APIRouter()


@router.get("/api/conversation/history")
async def api_conversation_history(request: Request, limit: int = 20, user=Depends(require_auth)):
    """获取用户历史会话记录（长期记忆）。遗留兼容端点（ADR-0003 后前端改用 /api/sessions）。"""
    user_id = user["user_id"]
    turns = deps._get_memory_service(user_id).get_conversation_history(user_id, limit)
    return JSONResponse(content={"user_id": user_id, "turns": turns, "count": len(turns)})


@router.get("/api/sessions")
async def api_list_sessions(request: Request, user=Depends(require_auth)):
    """获取用户的所有会话列表（按最近活跃排序）。"""
    user_id = user["user_id"]
    sessions = deps._get_memory_service(user_id).list_sessions(user_id)
    return JSONResponse(content={"user_id": user_id, "sessions": sessions, "count": len(sessions)})


@router.get("/api/sessions/{session_id}")
async def api_get_session(request: Request, session_id: str, user=Depends(require_auth)):
    """获取指定会话的完整对话历史。IDOR 由外观 _assert_owner 统一处理。"""
    user_id = user["user_id"]
    try:
        conversation = deps._get_memory_service(user_id).get_session(user_id, session_id)
    except PermissionError:
        return JSONResponse({"error": "会话不存在或无权访问"}, status_code=404)
    return JSONResponse(content={
        "session_id": session_id,
        "user_id": user_id,
        "conversation": conversation,
        "count": len(conversation),
    })


@router.delete("/api/sessions/{session_id}")
async def api_delete_session(request: Request, session_id: str, user=Depends(require_auth)):
    """删除指定会话及其全部记忆（LTM + Session Memory + 跨会话 embedding）。IDOR 由外观处理。"""
    user_id = user["user_id"]
    try:
        deps._get_memory_service(user_id).delete_session(user_id, session_id)
    except PermissionError:
        return JSONResponse({"error": "会话不存在或无权访问"}, status_code=404)
    return JSONResponse(content={"ok": True, "session_id": session_id})


@router.patch("/api/sessions/{session_id}")
async def api_rename_session(request: Request, session_id: str, user=Depends(require_auth)):
    """重命名会话标题。body: {"title": "新标题"}；IDOR 由外观 _assert_owner 处理。"""
    user_id = user["user_id"]
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "请求体不是有效 JSON"}, status_code=400)
    title = (body.get("title") or "").strip()
    if not title:
        return JSONResponse({"ok": False, "error": "标题不能为空"}, status_code=400)
    if len(title) > 60:
        title = title[:60]
    try:
        deps._get_memory_service(user_id).rename_session(user_id, session_id, title)
    except PermissionError:
        return JSONResponse({"error": "会话不存在或无权访问"}, status_code=404)
    return JSONResponse(content={"ok": True, "session_id": session_id, "title": title})
