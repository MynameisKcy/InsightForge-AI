"""FastAPI Server: 为 AI Data Analyst Multi-Agent System 提供 Web API 和页面。
运行方式: uvicorn api.fastapi_server:app --host 0.0.0.0 --port 8502

本模块是组装根（composition root）：仅负责 .env/sys.path 引导、app 实例、
中间件、静态挂载与路由注册。端点实现按业务域拆分在 api/routes/，共享服务
接缝在 api/deps.py，SSE 流式管道在 api/sse.py，结果序列化清洗在
api/serialization.py。
"""

import os
import sys

# ── 方案C：加载 .env（DASHSCOPE_API_KEY），须早于任何会实例化模型的导入 ──
try:
    from dotenv import load_dotenv
    _env_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env")
    load_dotenv(_env_path, override=False)
except ImportError:
    pass

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles

from api.auth import AUTH_COOKIE_NAME, set_auth_cookie, validate_token_cached
from api.routes import analysis, chat, datasets, knowledge, pages, sessions, settings, users
from utils.path_tool import get_abs_path

app = FastAPI(title="AI Data Analyst", version="1.0.0")


# ── 静态资源禁用浏览器强缓存 ──
# 开发期频繁改 JS/CSS，若浏览器强缓存旧文件，会出现"图标没更新 / 样式不生效"的
# 假性 bug（实为缓存）。no-cache 表示每次都带 ETag 向服务器验证：文件未变返回 304
# （省带宽），文件变了返回 200 新内容。仅作用于 /static/，不影响 SSE(/api/chat)。
@app.middleware("http")
async def _no_cache_static(request: Request, call_next):
    response = await call_next(request)
    if request.url.path.startswith("/static/"):
        response.headers["Cache-Control"] = "no-cache, must-revalidate"
    return response


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
    if not request.cookies.get(AUTH_COOKIE_NAME):
        auth = request.headers.get("authorization", "")
        if auth.lower().startswith("bearer "):
            tok = auth[7:].strip()
            if tok and validate_token_cached(tok):
                set_auth_cookie(response, tok, remember=True)
    return response


# ── 路由注册（端点实现见 api/routes/） ──
app.include_router(pages.router)
app.include_router(users.router)
app.include_router(chat.router)
app.include_router(analysis.router)
app.include_router(sessions.router)
app.include_router(datasets.router)
app.include_router(knowledge.router)
app.include_router(settings.router)


# ── 静态资源（落地页/应用页前端文件） ──
_STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
if os.path.isdir(_STATIC_DIR):
    app.mount("/static", StaticFiles(directory=_STATIC_DIR), name="static")

# ── 静态文件（报告和图表） ──
_reports_dir = get_abs_path("reports")
if os.path.exists(_reports_dir):
    app.mount("/reports", StaticFiles(directory=_reports_dir), name="reports")


def start_server(host: str = "0.0.0.0", port: int = 8502):
    """启动 FastAPI 服务。"""
    import uvicorn
    uvicorn.run(app, host=host, port=port)


if __name__ == "__main__":
    start_server()
