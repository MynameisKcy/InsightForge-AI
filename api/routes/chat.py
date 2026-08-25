"""统一智能客服 /api/chat：SSE 流式响应。

会话/记忆编排经 MemoryService（api.deps 接缝），流式经 api.sse 线程/心跳桥；
SSE 协议 token 契约（[SESSION]/[STEP]/[CHART]/[DONE] 等）与 api/static/ JS 锁步。
"""
import asyncio
import json
import os
from collections.abc import AsyncGenerator

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, StreamingResponse

from api import deps
from api.auth import require_auth
from api.serialization import _to_web_path
from api.sse import _split_sentences, _stream_with_heartbeat
from utils.cancel_token import CancelToken
from utils.logger_handler import logger
from utils.path_tool import get_abs_path
from utils.progress_emitter import ProgressEmitter
from utils.tracing import attach_current_span, detach_current_span, record_exception, span_context

router = APIRouter()


@router.post("/api/chat")
async def api_chat(request: Request, user=Depends(require_auth)):
    """统一智能客服：流式 SSE 响应（带会话管理、记忆管理、自动调度分析 Agent）。"""
    body = await request.json()
    query = body.get("query", "").strip()
    session_id = body.get("session_id", "").strip()
    if not query:
        return JSONResponse({"error": "query is required"}, status_code=400)

    user_id = user["user_id"]

    # ── 会话管理 + Session Memory（由 MemoryService 外观统一编排）──
    try:
        turn = deps._get_memory_service(user_id).begin_turn(user_id, session_id, query)
    except PermissionError as e:
        return JSONResponse({"error": str(e)}, status_code=404)
    session_id = turn.session_id
    mem_context = turn.mem_context
    new_session = turn.is_new_session

    agent = deps._get_react_agent(user_id)

    # 客户端断连取消通道：generate() 检测到断连即 cancel()；生产者线程在
    # ReactAgent 流循环 / PlannerAgent 步骤边界协作式退出，止损后续 LLM 调用
    cancel = CancelToken()

    async def generate() -> AsyncGenerator[str, None]:
        full_response = ""
        # ── 请求级根 Span：SSE 流式期间保持打开，子 Span 经 copy_context 关联 ──
        root_span, _root_token = attach_current_span("http.request")
        root_span.set_attribute("http.route", "/api/chat")
        root_span.set_attribute("http.user_id", user_id)
        root_span.set_attribute("http.session_id", session_id)
        root_span.set_attribute("http.query_length", len(query))
        # ── 记录分析前已有的图表文件，用于后续检测新图表 ──
        charts_dir = get_abs_path("reports/charts")
        existing_charts = set()
        if os.path.isdir(charts_dir):
            for f in os.listdir(charts_dir):
                if f.endswith(".html"):
                    existing_charts.add(os.path.join(charts_dir, f))
        try:
            # 通知前端 session_id + trace_id（可在 Jaeger 中直接检索本次请求链路）
            yield f"data: [SESSION]{session_id}\n\n"
            trace_id = span_context()
            if trace_id:
                yield f"data: [TRACE]{trace_id}\n\n"
            if new_session:
                yield "data: [SESSIONS_RELOAD]\n\n"

            emitter = ProgressEmitter()
            cancelled = False
            _chunk_polls = 0
            async for kind, value in _stream_with_heartbeat(
                lambda: agent.execute_stream(query, history=mem_context,
                                             user_id=user_id, session_id=session_id,
                                             progress_emitter=emitter,
                                             cancel_token=cancel),
                heartbeat="data: [KEEPALIVE]\n\n",
                interval=15,
                progress_emitter=emitter,
                cancel_token=cancel,
            ):
                # 断连检测：卡顿期随心跳必检、流式期每 20 chunk 抽检一次
                # （避免逐 chunk 轮询 receive 通道的开销；发现即置取消 token，
                # 生产者线程在下一个边界退出，本协程停止下发）
                # 注：TestClient 的 ASGI transport 在请求体耗尽后 receive 会
                # 误报 http.disconnect，故不做逐 chunk 检测（测试路径不触发抽检/心跳）。
                if cancel.cancelled:
                    cancelled = True
                    break
                if kind == "heartbeat":
                    if await request.is_disconnected():
                        cancel.cancel()
                        cancelled = True
                        break
                    # 纯保活：前端 resetIdle 即可，不再覆盖思考文案
                    yield value
                    continue
                if kind == "progress":
                    # 进度事件按 type 路由：metrics→Token 看板；decision→决策卡片；其余→步骤清单
                    etype = value.get("type", "")
                    if etype == "metrics":
                        yield f"data: [METRICS:{json.dumps(value, ensure_ascii=False)}]\n\n"
                        continue
                    if etype == "decision":
                        yield f"data: [DECISION:{json.dumps(value, ensure_ascii=False)}]\n\n"
                        continue
                    # 步骤进度事件：下发 [STEP:json]，前端渲染步骤清单
                    yield f"data: [STEP:{json.dumps(value, ensure_ascii=False)}]\n\n"
                    continue
                # kind == "chunk"
                _chunk_polls += 1
                if _chunk_polls % 20 == 0 and await request.is_disconnected():
                    cancel.cancel()
                    cancelled = True
                    break
                chunk = value
                if not chunk:
                    continue
                stripped = chunk.strip()
                # 思考状态指示：立即透传
                if stripped.startswith("[THINKING]"):
                    yield f"data: [THINKING]{stripped[10:]}\n\n"
                    continue
                full_response += chunk
                # ── 流式：按句子拆分，逐个发送 ──
                sentences = _split_sentences(stripped)
                if sentences:
                    for sentence in sentences:
                        yield f"data: {sentence.strip()}\n\n"
                        await asyncio.sleep(0.06)
                else:
                    # 无法拆分的内容（如列表项、标题等）原样输出
                    yield f"data: {stripped}\n\n"
                    await asyncio.sleep(0.03)

            # 取消也可能发生在 _stream_with_heartbeat 内部（其检测到 token 置位后
            # 直接 break，外层 async-for 表现为正常耗尽）——以 token 终态为准
            cancelled = cancelled or cancel.cancelled
            if cancelled:
                # 客户端已断连：响应无人接收，不发 [DONE]、不检测图表、
                # 不把残缺回复写入记忆（完整轮次以下一次成功请求为准）
                logger.info(f"Client disconnected; stream cancelled for session {session_id}")
                return

            # ── 检测新生成的图表文件并发送给前端 ──
            chart_urls = []
            if os.path.isdir(charts_dir):
                for f in sorted(os.listdir(charts_dir)):
                    if f.endswith(".html"):
                        fpath = os.path.join(charts_dir, f)
                        if fpath not in existing_charts:
                            web_url = _to_web_path(fpath)
                            chart_urls.append(web_url)
                            yield f"data: [CHART:{web_url}]\n\n"

            # 将图表 URL 嵌入 full_response，使历史会话加载时也能恢复图表
            if chart_urls:
                full_response += "\n\n" + "\n".join(f"[CHART:{u}]" for u in chart_urls)

            # 存入短期 + 长期记忆（由 MemoryService 外观统一编排）
            cleaned = full_response.strip()
            if cleaned:
                deps._get_memory_service(user_id).end_turn(
                    user_id, session_id, query, cleaned,
                    input_tokens=getattr(agent, '_last_input_tokens', None),
                )
            yield "data: [DONE]\n\n"
        except Exception as e:
            record_exception(root_span, e)
            logger.error(f"Chat streaming error: {e}")
            yield f"data: [ERROR] {str(e)}\n\n"
        finally:
            detach_current_span(_root_token)
            root_span.end()

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
