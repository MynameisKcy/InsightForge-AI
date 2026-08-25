"""聊天流线协议：agent chunk 流 → SSE token 流的翻译层。

route（api/routes/chat.py）只负责请求解析/auth/deps 接缝/StreamingResponse 组装；
本模块全权负责线协议：preamble token、进度事件路由、[THINKING] 切片、
句子节奏、断连采样（心跳期必检 + 每 20 chunk 抽检）、图表 diff 下发与
正文嵌入、持久化（end_turn）与取消路径三不做。
SSE token 契约与 api/static/ JS 锁步；线程/心跳桥在 api/sse.py，本模块是其消费方。
依赖（agent/memory_service/is_disconnected/charts_dir）全部显式参数注入，
单元测试直接传桩，不经过 HTTP 层。
"""
import asyncio
import json
import os
from collections.abc import AsyncGenerator

from api.serialization import _to_web_path
from api.sse import _split_sentences, _stream_with_heartbeat
from utils import sse_protocol as sp
from utils.logger_handler import logger
from utils.progress_emitter import ProgressEmitter
from utils.tracing import (
    attach_current_span,
    detach_current_span,
    record_exception,
    span_context,
)

_KEEPALIVE_FRAME = sp.frame(sp.KEEPALIVE)


def progress_event_token(event: dict) -> str:
    """进度事件按 type 路由为 SSE 帧：metrics→看板 / decision→决策卡 / 其余→步骤清单。"""
    etype = event.get("type", "")
    if etype == "metrics":
        return sp.frame(sp.METRICS, json.dumps(event, ensure_ascii=False))
    if etype == "decision":
        return sp.frame(sp.DECISION, json.dumps(event, ensure_ascii=False))
    return sp.frame(sp.STEP, json.dumps(event, ensure_ascii=False))


def thinking_token(chunk: str) -> str | None:
    """思考状态指示帧；非 THINKING 块返回 None。"""
    stripped = chunk.strip()
    marker = f"[{sp.THINKING}]"
    if stripped.startswith(marker):
        return sp.frame(sp.THINKING, stripped[len(marker):])
    return None


def snapshot_charts(charts_dir: str) -> set[str]:
    """流前快照：记录已存在的 .html 图表文件全路径。"""
    existing: set[str] = set()
    if os.path.isdir(charts_dir):
        for f in os.listdir(charts_dir):
            if f.endswith(".html"):
                existing.add(os.path.join(charts_dir, f))
    return existing


def diff_new_charts(charts_dir: str, existing: set[str]) -> list[str]:
    """流后扫描快照之外的新 .html 图表，返回 web URL 列表（按文件名排序）。"""
    urls: list[str] = []
    if os.path.isdir(charts_dir):
        for f in sorted(os.listdir(charts_dir)):
            if f.endswith(".html"):
                fpath = os.path.join(charts_dir, f)
                if fpath not in existing:
                    urls.append(_to_web_path(fpath))
    return urls


async def stream_chat_sse(
        *,
        agent,
        memory_service,
        query: str,
        user_id: str,
        session_id: str,
        mem_context,
        new_session: bool,
        cancel,
        is_disconnected,
        charts_dir: str,
        heartbeat_interval: float = 15,
) -> AsyncGenerator[str, None]:
    """把 agent.execute_stream 的同步块流翻译成 SSE 帧流。

    is_disconnected：async callable（route 传 request.is_disconnected），
    使断连采样可脱离 FastAPI Request 单测。heartbeat_interval 仅测试注入缩短；
    生产行为恒为 15s。
    """
    full_response = ""
    # ── 请求级根 Span：SSE 流式期间保持打开，子 Span 经 copy_context 关联 ──
    root_span, _root_token = attach_current_span("http.request")
    root_span.set_attribute("http.route", "/api/chat")
    root_span.set_attribute("http.user_id", user_id)
    root_span.set_attribute("http.session_id", session_id)
    root_span.set_attribute("http.query_length", len(query))
    existing_charts = snapshot_charts(charts_dir)
    try:
        # 通知前端 session_id + trace_id（可在 Jaeger 中直接检索本次请求链路）
        yield sp.frame(sp.SESSION, session_id)
        trace_id = span_context()
        if trace_id:
            yield sp.frame(sp.TRACE, trace_id)
        if new_session:
            yield sp.frame(sp.SESSIONS_RELOAD)

        emitter = ProgressEmitter()
        cancelled = False
        _chunk_polls = 0
        async for kind, value in _stream_with_heartbeat(
            lambda: agent.execute_stream(query, history=mem_context,
                                         user_id=user_id, session_id=session_id,
                                         progress_emitter=emitter,
                                         cancel_token=cancel),
            heartbeat=_KEEPALIVE_FRAME,
            interval=heartbeat_interval,
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
                if await is_disconnected():
                    cancel.cancel()
                    cancelled = True
                    break
                # 纯保活：前端 resetIdle 即可，不再覆盖思考文案
                yield value
                continue
            if kind == "progress":
                yield progress_event_token(value)
                continue
            # kind == "chunk"
            _chunk_polls += 1
            if _chunk_polls % 20 == 0 and await is_disconnected():
                cancel.cancel()
                cancelled = True
                break
            chunk = value
            if not chunk:
                continue
            stripped = chunk.strip()
            think = thinking_token(stripped)
            if think:
                yield think
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

        # ── 检测新生成的图表文件并发送给前端，URL 嵌入正文供历史会话恢复 ──
        chart_urls = diff_new_charts(charts_dir, existing_charts)
        for url in chart_urls:
            yield sp.frame(sp.CHART, url)
        if chart_urls:
            full_response += "\n\n" + "\n".join(f"[{sp.CHART}:{u}]" for u in chart_urls)

        # 存入短期 + 长期记忆（由 MemoryService 外观统一编排）
        cleaned = full_response.strip()
        if cleaned:
            memory_service.end_turn(
                user_id, session_id, query, cleaned,
                input_tokens=getattr(agent, '_last_input_tokens', None),
            )
        yield sp.frame(sp.DONE)
    except Exception as e:
        record_exception(root_span, e)
        logger.error(f"Chat streaming error: {e}")
        yield sp.frame(sp.ERROR, str(e))
    finally:
        detach_current_span(_root_token)
        root_span.end()
