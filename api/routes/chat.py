"""统一智能客服 /api/chat：SSE 流式响应。

本路由只负责请求解析/auth/deps 接缝/StreamingResponse 组装；流式线协议
（preamble token、事件路由、断连采样、图表 diff、持久化）在 api/chat_stream.py，
线程/心跳桥在 api/sse.py。SSE 协议 token 契约（[SESSION]/[STEP]/[CHART]/[DONE] 等）
与 api/static/ JS 锁步。
"""
from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse

from api import chat_stream, deps
from api.auth import require_auth
from api.errors import error_response
from utils.cancel_token import CancelToken
from utils.path_tool import get_abs_path
from utils.report_paths import CHARTS_DIR

router = APIRouter()


@router.post("/api/chat")
async def api_chat(request: Request, user=Depends(require_auth)):
    """统一智能客服：流式 SSE 响应（带会话管理、记忆管理、自动调度分析 Agent）。"""
    body = await request.json()
    query = body.get("query", "").strip()
    session_id = body.get("session_id", "").strip()
    if not query:
        return error_response("query is required", 400)

    user_id = user["user_id"]

    # ── 会话管理 + Session Memory（由 MemoryService 外观统一编排）──
    turn, err = deps.begin_memory_turn(user_id, session_id, query)
    if turn is None:
        return error_response(err, 404)

    agent = deps._get_react_agent(user_id)

    # 客户端断连取消通道：流翻译层检测到断连即 cancel()；生产者线程在
    # ReactAgent 流循环 / PlannerAgent 步骤边界协作式退出，止损后续 LLM 调用
    cancel = CancelToken()

    return StreamingResponse(
        chat_stream.stream_chat_sse(
            agent=agent,
            memory_service=deps._get_memory_service(user_id),
            query=query,
            user_id=user_id,
            session_id=turn.session_id,
            mem_context=turn.mem_context,
            new_session=turn.is_new_session,
            cancel=cancel,
            is_disconnected=request.is_disconnected,
            charts_dir=get_abs_path(CHARTS_DIR),
        ),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
