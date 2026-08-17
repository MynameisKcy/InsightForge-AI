"""
FastAPI Server: 为 AI Data Analyst Multi-Agent System 提供 Web API 和页面。
运行方式: uvicorn api.fastapi_server:app --host 0.0.0.0 --port 8502
"""

import asyncio
import json
import os
import re as re_module
import sys
import threading
import traceback
import uuid
from datetime import datetime
from typing import AsyncGenerator

# ── 方案C：加载 .env（DASHSCOPE_API_KEY），须早于任何会实例化模型的导入 ──
try:
    from dotenv import load_dotenv
    _env_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env")
    load_dotenv(_env_path, override=False)
except ImportError:
    pass


def _split_sentences(text: str) -> list[str]:
    """将文本按句子分割，保持分隔符在句尾。仅按中文标点 + 换行拆分。

    刻意不拆分英文句号（.）、感叹号（!）、问号（?）——中文输出中这些符号
    常出现在数字（3.26%）、Markdown 标记（**粗体**）、URL 等非句末语境，
    按它们拆分会导致换行断裂。
    """
    parts = re_module.split(r'(?<=[。！？\n])\s*', text)
    return [p for p in parts if p.strip()]


async def _stream_with_heartbeat(sync_gen_factory, heartbeat: str, interval: float = 15,
                                 progress_emitter=None):
    """把同步生成器放进后台线程执行，主协程带心跳消费。

    问题：ReactAgent.execute_stream 在 run_full_analysis 等长工具执行期间，
    同步迭代器会阻塞，async generate() 数分钟不 yield 任何字节，
    前端 idle 超时 abort。此包装用线程跑同步迭代，把每个 chunk 经
    loop.call_soon_threadsafe 推入 asyncio.Queue；主协程 wait_for 队列，
    interval 秒内无新数据则 yield 一个心跳保活。

    yield (kind, value)：kind 为 "heartbeat"（已格式化 SSE 行，直接 yield）、
    "chunk"（原始 chunk 文本，交调用方处理）、"progress"（步骤事件 dict，转 [STEP] 下发）。
    """
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue = asyncio.Queue()
    if progress_emitter is not None:
        progress_emitter.bind(loop, queue)   # 让 PlannerAgent 的步骤事件直注同一 queue
    # 线程异常载体
    error_box: list = []

    def _producer():
        try:
            for chunk in sync_gen_factory():
                loop.call_soon_threadsafe(queue.put_nowait, ("chunk", chunk))
        except Exception as e:  # 线程内异常推回主协程
            error_box.append(e)
            logger.error(f"_stream_with_heartbeat producer error: {e}\n{traceback.format_exc()}")
        finally:
            loop.call_soon_threadsafe(queue.put_nowait, ("done", None))

    t = threading.Thread(target=_producer, daemon=True)
    t.start()
    try:
        while True:
            try:
                kind, value = await asyncio.wait_for(queue.get(), timeout=interval)
            except asyncio.TimeoutError:
                yield ("heartbeat", heartbeat)  # 心跳保活
                continue
            if kind == "done":
                break
            yield (kind, value)   # "chunk" 或 "progress"
    finally:
        # 线程异常在主协程抛出，触发上层 except → [ERROR]
        if progress_emitter is not None:
            progress_emitter.close()
        if error_box:
            raise error_box[0]


_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from fastapi import FastAPI, Request, Header, UploadFile, File, Depends
from fastapi.responses import HTMLResponse, StreamingResponse, JSONResponse, RedirectResponse, FileResponse
from fastapi.staticfiles import StaticFiles

from utils.logger_handler import logger
from utils.path_tool import get_abs_path

# ── 记忆系统 & 用户认证 & 数据解析 ──
from memory.service import MemoryService

from database.user_db import user_db
from database.data_resolver import DataResolver

from utils.progress_emitter import ProgressEmitter

app = FastAPI(title="AI Data Analyst", version="1.0.0")
_memory_service = None  # MemoryService 懒加载单例（llm 按用户解析，由首次请求触发）


def _get_memory_service(user_id: str = "default") -> MemoryService:
    """获取 MemoryService 单例（懒加载；summarizer 经 llm_factory(user_id) 按用户解析模型）。

    user_id 仅供未来扩展（当前单例不持有用户状态）；按用户解析发生在
    summarizer 调用时——工厂收到调用方的 user_id，后台闲置 finalize 线程
    与请求线程互不干扰（消除了旧 _memory_llm_user 共享字典的竞态）。
    """
    global _memory_service
    if _memory_service is None:
        from model.factory import get_chat_model

        def _llm_factory(uid: str):
            def _llm_call(messages: list[dict]) -> str:
                from langchain_core.messages import HumanMessage
                llm = get_chat_model(uid)
                return llm.invoke(
                    [HumanMessage(content=m["content"]) for m in messages]
                ).content

            return _llm_call

        _memory_service = MemoryService(_llm_factory)
    return _memory_service


# ── 静态资源禁用浏览器强缓存 ──
# 开发期频繁改 JS/CSS，若浏览器强缓存旧文件，会出现"图标没更新 / 样式没生效"的
# 假性 bug（实为缓存）。no-cache 表示每次都带 ETag 向服务器验证：文件未变返回 304
# （省带宽），文件变了返回 200 新内容。仅作用于 /static/，不影响 SSE(/api/chat)。
@app.middleware("http")
async def _no_cache_static(request: Request, call_next):
    response = await call_next(request)
    if request.url.path.startswith("/static/"):
        response.headers["Cache-Control"] = "no-cache, must-revalidate"
    return response


# ── 鉴权依赖 + 静态资源目录 ──
from api.auth import require_auth, get_current_user, invalidate_token, extract_token, validate_token_cached  # noqa: E402

_STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")

# ── 会话 cookie：让页面级导航（GET /app）也能被服务端鉴权 ──
# token 服务端有效期 24h（见 database.user_db.login），cookie 生命周期与之对齐。
_AUTH_COOKIE_NAME = "token"
_AUTH_COOKIE_MAX_AGE = 24 * 3600


def _set_auth_cookie(response, token: str, remember: bool = True) -> None:
    """把会话 token 写入 cookie，使浏览器导航到 /app 时携带、被服务端正确识别。

    httponly：防 XSS 读取 cookie 里的 token；
    samesite=lax：允许顶级导航（点击链接 / location.href）携带，同时阻断跨站
                  POST 携带，缓解 CSRF；
    path=/：全站可用；
    remember=False：会话 cookie（关闭浏览器即失效），与前端 sessionStorage 语义一致。
    """
    response.set_cookie(
        key=_AUTH_COOKIE_NAME,
        value=token,
        max_age=_AUTH_COOKIE_MAX_AGE if remember else None,
        httponly=True,
        samesite="lax",
        path="/",
    )


@app.middleware("http")
async def refresh_auth_cookie(request: Request, call_next):
    """鉴权兜底：任何携带有效 Bearer token 的请求，若尚未持有 token cookie，
    则在响应上补种，使随后浏览器页面导航（GET /app、<a href="/app">）能被识别。

    必要性：旧版本只把 token 放在响应体、存于前端 localStorage，从不下发 cookie，
    导致已登录用户导航 /app 时服务端读不到会话而 302。本中间件保证——无论用户是
    重新登录（login 已 set-cookie），还是带着旧 localStorage token 直接访问，
    只要一次带 header 的鉴权请求成功（如落地页 initAuthState 调 /api/me），
    cookie 即被补齐，/app 导航必然通过，彻底消除重定向循环。
    """
    response = await call_next(request)
    if not request.cookies.get(_AUTH_COOKIE_NAME):
        auth = request.headers.get("authorization", "")
        if auth.lower().startswith("bearer "):
            tok = auth[7:].strip()
            if tok and validate_token_cached(tok):
                _set_auth_cookie(response, tok, remember=True)
    return response


# ── 延迟初始化 Agent（按 user_id 隔离：各用户独立实例，用自己的 LLM 配置） ──
# 旧设计是进程级单例，导致 per-user LLM 配置要么失效（单例已建）要么泄漏（首次构建用
# 某用户配置服务所有人）。改为按 user_id 缓存独立实例，配置变更时丢弃对应用户实例。
_react_agents = {}    # user_id -> ReactAgent
# PlannerAgent 的 per-user 缓存由 agent_tools 统一持有（run_full_analysis 工具的入口），
# 见 agent_tools._get_or_create_analyst；此处不再重复缓存，避免两份实例需要分别失效。


def _get_react_agent(user_id: str):
    if user_id not in _react_agents:
        from agent.react_agent import ReactAgent
        _react_agents[user_id] = ReactAgent(user_id=user_id)
    return _react_agents[user_id]


def _get_planner_agent(user_id: str):
    """委托给 agent_tools 的 per-user PlannerAgent 缓存（单一真相源）。

    /api/analysis（ADR-0001 已弃用，前端不再调用）与 run_full_analysis 工具共用同一份
    per-user 实例缓存，配置变更时只需失效一处（见 _invalidate_user_agents）。
    """
    from agent.tools.agent_tools import _get_or_create_analyst
    return _get_or_create_analyst(user_id)


def _invalidate_user_agents(user_id: str):
    """配置保存后调用：丢弃该用户的 Agent 实例，下次请求按新配置重建。
    配合 factory.reload_model_config(user_id) 一并清模型缓存。"""
    _react_agents.pop(user_id, None)
    # PlannerAgent 实例缓存由 agent_tools 持有，统一在此失效
    from agent.tools.agent_tools import invalidate_analyst
    invalidate_analyst(user_id)


# ── 知识库服务（单例） ──
_vector_store_service = None


def _get_vector_store():
    """延迟初始化向量库服务（方案C：运行时知识库管理）。"""
    global _vector_store_service
    if _vector_store_service is None:
        from rag.vector_store import VectorStoreService
        _vector_store_service = VectorStoreService()
    return _vector_store_service


# ── Routes ──

@app.post("/api/register")
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
                _set_auth_cookie(resp, login_result.get("token"), remember=False)
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


@app.post("/api/login")
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
        _set_auth_cookie(resp, result["token"], remember)
        return resp
    return JSONResponse(content=result, status_code=401)


@app.post("/api/logout")
async def api_logout(request: Request):
    """用户登出。"""
    token = request.headers.get("Authorization", "").replace("Bearer ", "")
    if not token:
        token = request.cookies.get(_AUTH_COOKIE_NAME, "")
    if token:
        user_db.logout(token)
        invalidate_token(token)
    resp = JSONResponse(content={"success": True})
    # 清除会话 cookie，避免登出后仍能凭 cookie 通过 /app 导航鉴权
    resp.delete_cookie(_AUTH_COOKIE_NAME, path="/")
    return resp


@app.get("/api/me")
async def api_me(user=Depends(require_auth)):
    """返回当前登录用户信息。未登录 401。"""
    return JSONResponse({
        "user_id": user["user_id"],
        "account": user["account"],
        "nickname": user.get("nickname"),
    })


@app.get("/")
async def index():
    """返回欢迎落地页。"""
    return FileResponse(os.path.join(_STATIC_DIR, "index.html"))


@app.get("/app")
async def app_page(request: Request):
    """主应用：未登录重定向到落地页。"""
    if not get_current_user(request):
        return RedirectResponse("/", status_code=302)
    return FileResponse(os.path.join(_STATIC_DIR, "app.html"))


# ── 静态资源（落地页/应用页前端文件） ──
if os.path.isdir(_STATIC_DIR):
    app.mount("/static", StaticFiles(directory=_STATIC_DIR), name="static")


@app.post("/api/chat")
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
        turn = _get_memory_service(user_id).begin_turn(user_id, session_id, query)
    except PermissionError as e:
        return JSONResponse({"error": str(e)}, status_code=404)
    session_id = turn.session_id
    mem_context = turn.mem_context
    new_session = turn.is_new_session

    agent = _get_react_agent(user_id)

    async def generate() -> AsyncGenerator[str, None]:
        full_response = ""
        # ── 记录分析前已有的图表文件，用于后续检测新图表 ──
        charts_dir = get_abs_path("reports/charts")
        existing_charts = set()
        if os.path.isdir(charts_dir):
            for f in os.listdir(charts_dir):
                if f.endswith(".html"):
                    existing_charts.add(os.path.join(charts_dir, f))
        try:
            # 通知前端 session_id
            yield f"data: [SESSION]{session_id}\n\n"
            if new_session:
                yield f"data: [SESSIONS_RELOAD]\n\n"

            emitter = ProgressEmitter()
            async for kind, value in _stream_with_heartbeat(
                lambda: agent.execute_stream(query, history=mem_context,
                                             user_id=user_id, session_id=session_id,
                                             progress_emitter=emitter),
                heartbeat="data: [KEEPALIVE]\n\n",
                interval=15,
                progress_emitter=emitter,
            ):
                if kind == "heartbeat":
                    # 纯保活：前端 resetIdle 即可，不再覆盖思考文案
                    yield value
                    continue
                if kind == "progress":
                    # 步骤进度事件：下发 [STEP:json]，前端渲染步骤清单
                    yield f"data: [STEP:{json.dumps(value, ensure_ascii=False)}]\n\n"
                    continue
                # kind == "chunk"
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
                _get_memory_service(user_id).end_turn(
                    user_id, session_id, query, cleaned,
                    input_tokens=getattr(agent, '_last_input_tokens', None),
                )
            yield "data: [DONE]\n\n"
        except Exception as e:
            logger.error(f"Chat streaming error: {e}")
            yield f"data: [ERROR] {str(e)}\n\n"

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@app.post("/api/analysis")
async def api_analysis(request: Request, user=Depends(require_auth)):
    """数据分析：同步返回 JSON（带记忆管理）。"""
    body = await request.json()
    query = body.get("query", "").strip()
    if not query:
        return JSONResponse({"error": "query is required"}, status_code=400)

    user_id = user["user_id"]
    session_id = body.get("session_id", "").strip()
    try:
        turn = _get_memory_service(user_id).begin_turn(user_id, session_id, query)
        session_id = turn.session_id
    except PermissionError as e:
        return JSONResponse({"error": str(e)}, status_code=404)

    try:
        analyst = _get_planner_agent(user_id)
        result = analyst.run({"query": query, "user_id": user_id})

        # 将分析结果摘要存入记忆
        report = result.get("report", {})
        summary_text = report.get("markdown", str(result.get("title", "")))
        if summary_text:
            _get_memory_service(user_id).end_turn(
                user_id, session_id, query, f"[分析结果] {summary_text[:500]}",
            )

        # 序列化时处理非 JSON 兼容类型 + 转换图表路径为 Web 可访问
        result = _sanitize_result(result)
        result = _normalize_paths(result)
        return JSONResponse(content=result)
    except Exception as e:
        logger.error(f"Analysis error: {traceback.format_exc()}")
        return JSONResponse(
            {"success": False, "errors": [str(e)]},
            status_code=500,
        )


_EXPORT_FORMATS = {"md", "docx", "pdf", "html"}


@app.post("/api/report/export")
async def api_export_report(request: Request, user=Depends(require_auth)):
    """导出报告为 Word/Markdown/PDF/HTML。

    入参 JSON: {"markdown": str, "title": str, "format": "md"|"docx"|"pdf"|"html"}
    返回 FileResponse 触发浏览器下载。
    """
    body = await request.json()
    markdown = body.get("markdown", "")
    title = body.get("title", "数据分析报告") or "数据分析报告"
    fmt = (body.get("format", "") or "").lower().strip()

    if not markdown.strip():
        return JSONResponse({"error": "No markdown content"}, status_code=400)
    if fmt not in _EXPORT_FORMATS:
        return JSONResponse({"error": f"Unsupported format: {fmt}"}, status_code=400)

    try:
        from agents.export_agent import ExportAgent
        # user_id 必传：Agent 的 LLM 按用户解析（网页设置 > .env），
        # 不传则钉死 .env 默认模型（免费额度耗尽时 403）
        result = ExportAgent(user_id=user["user_id"]).run({
            "markdown": markdown,
            "title": title,
            "formats": [fmt],
        })
        files = result.get("files", [])
        if not files:
            errs = result.get("errors", [])
            msg = errs[0] if errs else f"{fmt} export produced no file"
            return JSONResponse({"error": msg}, status_code=502)
        fpath = files[0]["path"]
        if not os.path.exists(fpath):
            return JSONResponse({"error": "Export file missing"}, status_code=502)
        # 浏览器下载文件名（FileResponse 的 filename 参数会自动生成
        # RFC 5987 filename*=UTF-8''... 头，正确处理中文等非 ASCII 文件名；
        # 不手写 Content-Disposition 以免 latin-1 编码失败）
        download_name = os.path.basename(fpath)
        return FileResponse(fpath, filename=download_name)
    except Exception as e:
        logger.error(f"Export error: {traceback.format_exc()}")
        return JSONResponse({"error": str(e)}, status_code=500)


@app.get("/api/conversation/history")
async def api_conversation_history(request: Request, limit: int = 20, user=Depends(require_auth)):
    """获取用户历史会话记录（长期记忆）。遗留兼容端点（ADR-0003 后前端改用 /api/sessions）。"""
    user_id = user["user_id"]
    turns = _get_memory_service(user_id).get_conversation_history(user_id, limit)
    return JSONResponse(content={"user_id": user_id, "turns": turns, "count": len(turns)})


@app.get("/api/sessions")
async def api_list_sessions(request: Request, user=Depends(require_auth)):
    """获取用户的所有会话列表（按最近活跃排序）。"""
    user_id = user["user_id"]
    sessions = _get_memory_service(user_id).list_sessions(user_id)
    return JSONResponse(content={"user_id": user_id, "sessions": sessions, "count": len(sessions)})


@app.get("/api/sessions/{session_id}")
async def api_get_session(request: Request, session_id: str, user=Depends(require_auth)):
    """获取指定会话的完整对话历史。IDOR 由外观 _assert_owner 统一处理。"""
    user_id = user["user_id"]
    try:
        conversation = _get_memory_service(user_id).get_session(user_id, session_id)
    except PermissionError:
        return JSONResponse({"error": "会话不存在或无权访问"}, status_code=404)
    return JSONResponse(content={
        "session_id": session_id,
        "user_id": user_id,
        "conversation": conversation,
        "count": len(conversation),
    })


@app.delete("/api/sessions/{session_id}")
async def api_delete_session(request: Request, session_id: str, user=Depends(require_auth)):
    """删除指定会话及其全部记忆（LTM + Session Memory + 跨会话 embedding）。IDOR 由外观处理。"""
    user_id = user["user_id"]
    try:
        _get_memory_service(user_id).delete_session(user_id, session_id)
    except PermissionError:
        return JSONResponse({"error": "会话不存在或无权访问"}, status_code=404)
    return JSONResponse(content={"ok": True, "session_id": session_id})


@app.patch("/api/sessions/{session_id}")
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
        _get_memory_service(user_id).rename_session(user_id, session_id, title)
    except PermissionError:
        return JSONResponse({"error": "会话不存在或无权访问"}, status_code=404)
    return JSONResponse(content={"ok": True, "session_id": session_id, "title": title})


@app.get("/api/health")
async def health():
    """健康检查。"""
    return {"status": "ok", "timestamp": datetime.now().isoformat()}


# ── 数据集管理 ──

def _datasets_dir() -> str:
    """用户上传的数据集存放目录。"""
    d = get_abs_path("data/datasets")
    os.makedirs(d, exist_ok=True)
    return d

_ALLOWED_DATASET_TYPES = {"csv", "xlsx", "xls"}
_MAX_DATASET_SIZE = 100 * 1024 * 1024  # 100MB


@app.get("/api/datasets")
async def list_datasets(request: Request, user=Depends(require_auth)):
    """列出所有可用数据集。"""
    user_id = user["user_id"]
    from database.datasources_db import datasources_db
    datasets = datasources_db.list_datasets(owner_user_id=user_id)
    return JSONResponse({"datasets": datasets, "count": len(datasets)})


@app.post("/api/datasets/upload")
async def upload_dataset(request: Request, file: UploadFile = File(...), user=Depends(require_auth)):
    """上传 CSV/Excel 文件，解析并加载到 DuckDB。"""
    user_id = user["user_id"]

    fname = os.path.basename(file.filename or "")
    ext = os.path.splitext(fname)[1].lower().lstrip(".")
    if ext not in _ALLOWED_DATASET_TYPES:
        return JSONResponse(
            {"success": False, "error": f"不支持的文件类型: {ext}，仅支持 CSV/XLSX/XLS"},
            status_code=400,
        )

    content = await file.read()
    if len(content) > _MAX_DATASET_SIZE:
        return JSONResponse(
            {"success": False, "error": f"文件超过大小限制(100MB)"},
            status_code=413,
        )

    # 保存文件
    ds_dir = _datasets_dir()
    # 生成安全的表名：文件名去扩展名，替换非法字符
    base_name = os.path.splitext(fname)[0]
    safe_name = re_module.sub(r'[^A-Za-z0-9]+', '_', base_name).strip('_')
    if not safe_name or not safe_name[0].isalpha():
        safe_name = "ds_" + (safe_name or "upload")

    # 处理同名冲突
    from database.datasources_db import datasources_db

    table_name = safe_name
    counter = 2
    while datasources_db.get_dataset(table_name, owner_user_id=user_id):
        table_name = f"{safe_name}_{counter}"
        counter += 1

    fpath = os.path.join(ds_dir, f"{table_name}.{ext}")
    with open(fpath, "wb") as out:
        out.write(content)

    # 加载到 DuckDB
    from database.duckdb_manager import init_duckdb
    from database.safety import safe_ident

    try:
        db = init_duckdb(user_id=user_id)
        if ext == "csv":
            load_result = db.load_csv_dataset(fpath, table_name)
        else:
            load_result = db.load_excel_dataset(fpath, table_name)

        if not load_result["success"]:
            # 加载失败，删除文件
            os.remove(fpath)
            return JSONResponse({"success": False, "error": load_result["error"]}, status_code=400)

        # 解析 schema
        qname = safe_ident(table_name)
        cols = db.execute(f"DESCRIBE {qname}").fetchall()
        schema_json = json.dumps([
            {"name": c[0], "type": c[1]} for c in cols
        ], ensure_ascii=False)

        # 获取样本数据（前5行）
        try:
            sample_df = db.query_df(f"SELECT * FROM {qname} LIMIT 5")
            sample_data = sample_df.to_dict(orient="records")
        except Exception:
            sample_data = []

        # 写入元数据（带 owner_user_id 实现多用户隔离）
        source_type = "csv" if ext == "csv" else "excel"
        # display_name: 原始文件名去扩展名，保留中文，供侧边栏展示与 DataResolver 匹配
        # （table_name 是安全化 ASCII 名，用户无法对应；display_name 是用户能认得的名字）
        display_name = os.path.splitext(fname)[0].strip()
        meta_result = datasources_db.add_dataset(
            name=table_name,
            source_type=source_type,
            file_path=fpath,
            table_name=table_name,
            schema_json=schema_json,
            row_count=load_result["row_count"],
            owner_user_id=user_id,
            display_name=display_name,
        )
        # 元数据写入失败（如 UNIQUE 冲突）：删除文件并明确报错，避免"上传报成功但侧边栏查不到"
        if not meta_result.get("success"):
            try:
                os.remove(fpath)
            except OSError:
                pass
            return JSONResponse(
                {"success": False, "error": f"元数据写入失败: {meta_result.get('error', '未知错误')}"},
                status_code=400,
            )

        return JSONResponse({
            "success": True,
            "name": table_name,
            "display_name": display_name,
            "source_type": source_type,
            "row_count": load_result["row_count"],
            "columns": [c[0] for c in cols],
            "sample": _json_safe(sample_data),
        })

    except Exception as e:
        logger.error(f"Dataset upload failed: {traceback.format_exc()}")
        if os.path.exists(fpath):
            os.remove(fpath)
        return JSONResponse({"success": False, "error": str(e)}, status_code=500)


@app.delete("/api/datasets/{name}")
async def delete_dataset(request: Request, name: str, user=Depends(require_auth)):
    """删除数据集（卸载 DuckDB 表 + 删除文件 + 删除元数据）。"""
    user_id = user["user_id"]
    if "/" in name or "\\" in name or name in ("", ".", ".."):
        return JSONResponse({"error": "非法数据集名"}, status_code=400)

    from database.datasources_db import datasources_db

    ds = datasources_db.get_dataset(name, owner_user_id=user_id)
    if not ds:
        return JSONResponse({"error": f"数据集 '{name}' 不存在或不属于当前用户"}, status_code=404)

    # 从 DuckDB 删除表
    from database.duckdb_manager import init_duckdb

    try:
        db = init_duckdb(user_id=user_id)
        db.drop_table(ds["table_name"])
    except Exception as e:
        logger.warning(f"Failed to drop table {ds['table_name']}: {e}")

    # 删除文件（路径穿越防护：仅允许删除 datasets 目录下的文件）
    if ds["file_path"] and os.path.exists(ds["file_path"]):
        try:
            allowed_dir = os.path.abspath(_datasets_dir())
            real_path = os.path.realpath(ds["file_path"])
            if real_path.startswith(allowed_dir + os.sep):
                os.remove(real_path)
            else:
                logger.warning(f"Refusing to delete file outside datasets dir: {real_path}")
        except Exception as e:
            logger.warning(f"Failed to delete file {ds['file_path']}: {e}")

    # 删除元数据（带归属校验，防越权）
    datasources_db.delete_dataset(name, owner_user_id=user_id)
    return JSONResponse({"success": True})


@app.get("/api/datasets/{name}/schema")
async def get_dataset_schema(request: Request, name: str, user=Depends(require_auth)):
    """获取数据集的详细 schema。"""
    user_id = user["user_id"]

    from database.datasources_db import datasources_db

    ds = datasources_db.get_dataset(name, owner_user_id=user_id)
    if not ds:
        return JSONResponse({"error": f"数据集 '{name}' 不存在或不属于当前用户"}, status_code=404)

    # 从 DuckDB 获取实时 schema
    from database.duckdb_manager import init_duckdb
    from database.safety import safe_ident

    try:
        db = init_duckdb(user_id=user_id)
        qname = safe_ident(ds['table_name'])
        cols = db.execute(f"DESCRIBE {qname}").fetchall()
        stats = db.execute(f"SUMMARIZE {qname}").fetchall()
        sample_df = db.query_df(f"SELECT * FROM {qname} LIMIT 5")

        return JSONResponse({
            "name": name,
            "table_name": ds["table_name"],
            "source_type": ds["source_type"],
            "row_count": ds["row_count"],
            "columns": [{"name": c[0], "type": c[1]} for c in cols],
            "statistics": [
                {"column": s[0], "type": s[1], "min": str(s[2]) if s[2] is not None else None,
                 "max": str(s[3]) if s[3] is not None else None,
                 "avg": str(s[4]) if s[4] is not None else None,
                 "std": str(s[5]) if s[5] is not None else None,
                 "count": s[6], "null_count": s[7]}
                for s in stats
            ],
            "sample": _json_safe(sample_df.to_dict(orient="records")),
        })
    except Exception as e:
        # DuckDB 中表可能尚未加载，返回元数据中的 schema_json
        import json as _json
        return JSONResponse({
            "name": name,
            "table_name": ds["table_name"],
            "source_type": ds["source_type"],
            "row_count": ds["row_count"],
            "columns": _json.loads(ds.get("schema_json", "[]")),
            "note": "DuckDB 中未加载，显示的是缓存 schema",
        })


@app.post("/api/datasources/reload")
async def reload_datasources(request: Request, user=Depends(require_auth)):
    """热加载 datasources.yml 配置的数据库连接。"""
    user_id = user["user_id"]

    from database.duckdb_manager import init_duckdb

    try:
        db = init_duckdb(user_id=user_id)
        result = db.register_external_databases()
        return JSONResponse({"success": True, **result})
    except Exception as e:
        logger.error(f"Datasource reload failed: {traceback.format_exc()}")
        return JSONResponse({"success": False, "error": str(e)}, status_code=500)


# ── 知识库管理（方案C-5） ──

def _kb_data_dir(user_id: str | None = None) -> str:
    from utils.config_handler import chroma_conf
    base = get_abs_path(chroma_conf["data_path"])
    # 按用户分目录：data/<user_id>/，消除同名文件覆盖与磁盘级跨用户泄露
    if not user_id:
        return base
    return os.path.join(base, user_id)


def _kb_allowed_types() -> tuple:
    from utils.config_handler import chroma_conf
    return tuple(chroma_conf["allowed_knowledge_file_type"])


# ── 账号设置（需求①：配置管理 + 热重载） ──
from database.user_settings_db import user_settings_db

from model.factory import reload_model_config


@app.get("/api/settings/status")
async def get_settings_status(request: Request, user=Depends(require_auth)):
    """返回当前用户是否已配置。前端登录后据此决定是否弹提示。"""
    user_id = user["user_id"]
    return {"configured": user_settings_db.has(user_id), "authed": True}


@app.get("/api/settings")
async def get_settings(request: Request, user=Depends(require_auth)):
    """返回当前用户配置（API Key 掩码）。未登录 401，未配置返回 null。"""
    user_id = user["user_id"]
    data = user_settings_db.get_masked(user_id)
    if data is None:
        return {"configured": False, "settings": None}
    return {"configured": True, "settings": data}


@app.post("/api/settings")
async def save_settings(request: Request, user=Depends(require_auth)):
    """保存用户配置并触发热重载。前端回传掩码值时不覆盖已存明文 key。"""
    user_id = user["user_id"]
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "请求体不是有效 JSON"}, status_code=400)
    allowed = {"llm_api_key", "llm_model_name", "embedding_model_name", "llm_base_url",
               "vector_db_host", "vector_db_port", "vector_db_collection",
               "vector_db_tenant", "local_db_conn"}
    cleaned = {k: v for k, v in body.items() if k in allowed}
    # 前端回传掩码值（含 ****）：不覆盖已存明文 key
    if "****" in str(cleaned.get("llm_api_key", "")):
        cleaned.pop("llm_api_key", None)
        existing = user_settings_db.get(user_id) or {}
        if existing.get("llm_api_key"):
            cleaned["llm_api_key"] = existing["llm_api_key"]
    try:
        user_settings_db.upsert(user_id, cleaned)
        reload_model_config(user_id)            # 失效该用户模型缓存
        _invalidate_user_agents(user_id)        # 丢弃该用户 Agent 实例，下次按新配置重建
        return {"ok": True}
    except Exception as e:
        logger.exception("保存配置失败")
        return JSONResponse({"ok": False, "error": f"保存失败: {e}"}, status_code=500)


# ── 用户个人信息（昵称 / 密码） ──


@app.get("/api/profile")
async def api_get_profile(request: Request, user=Depends(require_auth)):
    """返回当前用户个人信息：account、昵称、头像 URL。"""
    user_id = user["user_id"]
    user_info = user_db.get_user(user_id) or {}
    return JSONResponse(content={
        "user_id": user_id,
        "account": user_info.get("account", ""),
        "nickname": user_info.get("nickname") or "",
    })


@app.post("/api/profile")
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


@app.post("/api/password")
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


@app.get("/api/knowledge/files")
async def kb_list_files(request: Request, user=Depends(require_auth)):
    """列出 data/ 下知识库文件，含大小/类型/md5/是否已入库。"""
    user_id = user["user_id"]
    data_dir = _kb_data_dir(user_id)
    allowed = _kb_allowed_types()
    from utils.file_handler import get_file_md5_hex

    vs = _get_vector_store()
    ingested_md5 = vs._load_md5_store()
    # "已入库"以 chroma 实际数据为准（md5 仅去重提示，可能与 chroma 偏离）
    chroma_sources = vs.chroma_sources(user_id)
    chroma_basenames = {os.path.basename(s) for s in chroma_sources}
    files = []
    if os.path.isdir(data_dir):
        for fname in sorted(os.listdir(data_dir)):
            fpath = os.path.join(data_dir, fname)
            if not os.path.isfile(fpath):
                continue
            ext = os.path.splitext(fname)[1].lower().lstrip(".")
            if ext not in allowed:
                continue
            size = os.path.getsize(fpath)
            md5 = get_file_md5_hex(fpath) or ""
            files.append({
                "filename": fname,
                "size": size,
                "type": ext,
                "md5": md5,
                "ingested": (fpath in chroma_sources) or (fname in chroma_basenames),
            })
    return JSONResponse({"files": files, "count": len(files)})


@app.get("/api/files")
async def list_all_files(request: Request, user=Depends(require_auth)):
    """统一文件列表（需求②）：文本类（Chroma 知识库）+ 表格类（DuckDB 数据集）。"""
    user_id = user["user_id"]
    files = []
    # ── 文本类（PDF/Word/TXT/MD，进 Chroma）──
    data_dir = _kb_data_dir(user_id)
    allowed = _kb_allowed_types()
    from utils.file_handler import get_file_md5_hex
    try:
        vs = _get_vector_store()
        ingested_md5 = vs._load_md5_store()
        # "已入库"以 chroma 实际数据为准（md5 仅去重提示，可能与 chroma 偏离）
        chroma_sources = vs.chroma_sources(user_id)
        chroma_basenames = {os.path.basename(s) for s in chroma_sources}
    except Exception as e:
        logger.warning(f"加载向量库 md5 失败: {e}")
        ingested_md5 = set()
        chroma_sources = set()
        chroma_basenames = set()
    if os.path.isdir(data_dir):
        for fname in sorted(os.listdir(data_dir)):
            fpath = os.path.join(data_dir, fname)
            if not os.path.isfile(fpath):
                continue
            ext = os.path.splitext(fname)[1].lower().lstrip(".")
            if ext not in allowed:
                continue
            md5 = get_file_md5_hex(fpath) or ""
            ingested = (fpath in chroma_sources) or (fname in chroma_basenames)
            status = "已完成" if ingested else "处理中"
            files.append({
                "name": fname, "type": "text",
                "size": os.path.getsize(fpath),
                "upload_time": "", "status": status, "source": "chroma",
            })
    # ── 表格类（CSV/Excel，进 DuckDB）──
    from database.datasources_db import datasources_db
    try:
        for d in datasources_db.list_datasets(owner_user_id=user_id):
            files.append({
                "name": d.get("name"), "type": "table",
                "size": d.get("row_count"),
                "upload_time": d.get("created_at", ""),
                "status": "已完成", "source": "duckdb",
                "table_name": d.get("table_name"),
            })
    except Exception as e:
        logger.warning(f"列数据集失败: {e}")
    return JSONResponse({"files": files, "count": len(files)})


@app.post("/api/knowledge/upload")
async def kb_upload(request: Request, files: list[UploadFile] = File(...), user=Depends(require_auth)):
    """上传文件到 data/ 并增量入库。"""
    user_id = user["user_id"]
    data_dir = _kb_data_dir(user_id)
    allowed = _kb_allowed_types()
    os.makedirs(data_dir, exist_ok=True)
    vs = _get_vector_store()

    results = []
    for f in files:
        fname = os.path.basename(f.filename or "")
        ext = os.path.splitext(fname)[1].lower().lstrip(".")
        if ext not in allowed:
            results.append({"filename": fname, "success": False,
                            "error": f"不支持的文件类型: {ext}"})
            continue
        # 大文件保护：超过 100MB 拒绝；PDF/Excel >50MB 给预估提示
        size = f.size if hasattr(f, "size") and f.size is not None else 0
        if size > _MAX_DATASET_SIZE:
            results.append({"filename": fname, "success": False,
                            "error": "文件过大（超过 100MB 上限），请拆分或压缩后再上传"})
            continue
        advisory = ""
        if ext in ("pdf", "docx") and size > 50 * 1024 * 1024:
            mb = max(1, round(size / 1024 / 1024))
            advisory = f"文件较大（{mb}MB），解析入库预计较慢，请耐心等待"
        fpath = os.path.join(data_dir, fname)
        try:
            content = await f.read()
            with open(fpath, "wb") as out:
                out.write(content)
            chunks, skipped = vs.load_single_document(fpath, user_id)
            results.append({
                "filename": fname,
                "success": True,
                "chunks": chunks,
                "skipped": skipped,
                "advisory": advisory,
            })
        except Exception as e:
            logger.error(f"知识库上传入库失败 {fname}: {traceback.format_exc()}")
            results.append({"filename": fname, "success": False, "error": str(e)})
    return JSONResponse({"results": results})


@app.delete("/api/knowledge/files/{filename}")
async def kb_delete_file(request: Request, filename: str, user=Depends(require_auth)):
    """删除 data/ 下指定文件，并从向量库移除其分片。"""
    user_id = user["user_id"]
    # 防路径穿越
    if "/" in filename or "\\" in filename or filename in ("", ".", ".."):
        return JSONResponse({"error": "非法文件名"}, status_code=400)
    data_dir = _kb_data_dir(user_id)
    fpath = os.path.join(data_dir, filename)
    if not os.path.isfile(fpath):
        return JSONResponse({"error": "文件不存在"}, status_code=404)
    try:
        vs = _get_vector_store()
        removed = vs.delete_by_source(fpath, user_id)
        os.remove(fpath)
        return JSONResponse({"success": True, "removed_chunks": removed})
    except Exception as e:
        logger.error(f"知识库删除失败 {filename}: {traceback.format_exc()}")
        return JSONResponse({"error": str(e)}, status_code=500)


@app.post("/api/knowledge/reindex")
async def kb_reindex(request: Request, user=Depends(require_auth)):
    """清空向量库并全量重建索引（二次确认通过 confirm=true 才执行）。"""
    user_id = user["user_id"]
    body = await request.json() if request.headers.get("content-type", "").startswith("application/json") else {}
    if not body.get("confirm"):
        return JSONResponse({"error": "需传 confirm=true 以确认全量重建"}, status_code=400)
    try:
        vs = _get_vector_store()
        result = vs.reindex_all(user_id)
        return JSONResponse({"success": True, **result})
    except Exception as e:
        logger.error(f"知识库重建失败: {traceback.format_exc()}")
        return JSONResponse({"error": str(e)}, status_code=500)


@app.get("/api/knowledge/stats")
async def kb_stats(request: Request, user=Depends(require_auth)):
    """返回知识库统计信息。"""
    user_id = user["user_id"]
    try:
        vs = _get_vector_store()
        return JSONResponse(vs.get_stats(user_id))
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


# ── 静态文件（报告和图表） ──
_reports_dir = get_abs_path("reports")
if os.path.exists(_reports_dir):
    app.mount("/reports", StaticFiles(directory=_reports_dir), name="reports")

def _sanitize_result(result: dict) -> dict:
    """确保结果可 JSON 序列化。"""
    sanitized = {}
    for key, value in result.items():
        if key == "results":
            sanitized[key] = _sanitize_dict(value)
        elif isinstance(value, dict):
            sanitized[key] = _sanitize_dict(value)
        elif isinstance(value, list):
            sanitized[key] = [_sanitize_dict(v) if isinstance(v, dict) else v for v in value]
        else:
            sanitized[key] = value
    return sanitized


def _json_safe(obj):
    """递归把 NaN/Infinity 转成 None（JSON null）。

    DuckDB 数值列含空单元格时，pandas to_dict 会产出 float('nan')，
    FastAPI JSONResponse（allow_nan=False）序列化会抛
    "Out of range float values are not JSON compliant"。此处统一清洗。
    """
    import math
    if isinstance(obj, float):
        if math.isnan(obj) or math.isinf(obj):
            return None
        return obj
    if isinstance(obj, dict):
        return {k: _json_safe(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_json_safe(v) for v in obj]
    return obj



def _sanitize_dict(d: dict) -> dict:
    """递归清理字典中的非 JSON 类型。"""
    if not isinstance(d, dict):
        return d
    clean = {}
    for k, v in d.items():
        if isinstance(v, dict):
            clean[k] = _sanitize_dict(v)
        elif isinstance(v, list):
            clean[k] = [_sanitize_dict(i) if isinstance(i, dict) else i for i in v]
        elif isinstance(v, (str, int, float, bool, type(None))):
            clean[k] = v
        else:
            clean[k] = str(v)
    return clean


def _to_web_path(abs_path: str) -> str:
    """将绝对路径转为 Web 可访问的相对路径。
    D:\\...\\reports\\charts\\foo.html → /reports/charts/foo.html
    """
    import re
    # 标准化路径分隔符
    normalized = abs_path.replace("\\", "/")
    # 提取 reports/ 之后的部分
    match = re.search(r"/reports/(.+)", normalized)
    if match:
        return f"/reports/{match.group(1)}"
    # 如果路径已经以 / 开头且存在，直接返回
    if normalized.startswith("/reports/"):
        return normalized
    # 无法转换，返回原名
    return normalized


def _normalize_paths(obj):
    """递归转换 dict/list 中的绝对路径为 Web URL。"""
    if isinstance(obj, dict):
        result = {}
        for k, v in obj.items():
            # 转换 path/url 字段
            if k in ("path", "file_path") and isinstance(v, str) and (":\\" in v or ":/" in v):
                result[k] = v  # 保留原始路径
                result["url"] = _to_web_path(v)  # 添加 Web 可访问 URL
            elif k == "charts" and isinstance(v, list):
                result[k] = [
                    {**c, "url": _to_web_path(c.get("path", ""))}
                    if isinstance(c, dict) and c.get("path") else c
                    for c in v
                ]
            elif isinstance(v, (dict, list)):
                result[k] = _normalize_paths(v)
            else:
                result[k] = v
        return result
    elif isinstance(obj, list):
        return [_normalize_paths(item) if isinstance(item, (dict, list)) else item for item in obj]
    return obj


def start_server(host: str = "0.0.0.0", port: int = 8502):
    """启动 FastAPI 服务。"""
    import uvicorn
    uvicorn.run(app, host=host, port=port)


if __name__ == "__main__":
    start_server()
