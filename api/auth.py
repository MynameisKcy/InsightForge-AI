"""统一鉴权：require_auth 依赖 + validate_token 进程内短缓存。"""
import threading
import time

from fastapi import HTTPException, Request

from database.user_db import user_db

# token -> (user_dict, expire_ts)；30s 短缓存，token 24h 有效，安全
_CACHE_TTL = 30.0
_token_cache: dict[str, tuple[dict, float]] = {}
# 多线程写保护（uvicorn 单进程多线程 / TestClient 线程池并发请求）；
# dict 原子性下竞态只是重复查库，但超限清理的遍历-删除与并发写可能 RuntimeError
_token_cache_lock = threading.Lock()


def extract_token(request: Request) -> str | None:
    """提取会话 token：优先 Authorization: Bearer <token>，无头时回退 token cookie。

    关键：浏览器的页面级导航（直接访问 /app、点击 <a href="/app">、
    window.location.href='/app'）不会携带 Authorization 头 —— 该头只在
    fetch/XHR 中由前端手动添加，token 又存于 localStorage，导航时同样读不到。
    若页面路由（GET /app）仅凭 Authorization 头鉴权，已登录用户导航过来也会
    被判为未登录而 302 回落地页，与 fetch 调用的 /api/me（带头，返回 200）
    形成 /app → / → /api/me → /app 的重定向死循环。
    因此这里回退到 cookie，使服务端能在导航场景下正确识别会话。
    """
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        tok = auth[7:].strip()
        if tok:
            return tok
    # 回退：页面导航场景从 cookie 读取（mock request 可能无 cookies 属性）
    cookies = getattr(request, "cookies", None)
    if cookies:
        tok = cookies.get("token")
        if tok:
            return tok
    return None


def validate_token_cached(token: str | None) -> dict | None:
    """包 LRU 短缓存的 validate_token。命中不查库。"""
    if not token:
        return None
    now = time.time()
    with _token_cache_lock:
        cached = _token_cache.get(token)
        if cached and cached[1] > now:
            return cached[0]
    # 查库在锁外：慢操作不阻塞其他请求的缓存读
    user = user_db.validate_token(token)
    with _token_cache_lock:
        _token_cache[token] = (user, now + _CACHE_TTL)
        # 简易清理：缓存超 1000 项时丢掉过期项
        if len(_token_cache) > 1000:
            for k in [k for k, v in _token_cache.items() if v[1] <= now]:
                _token_cache.pop(k, None)
    return user


def invalidate_token(token: str | None) -> None:
    """Evict a token from the LRU cache (call on logout / revocation)."""
    if token:
        with _token_cache_lock:
            _token_cache.pop(token, None)


def require_auth(request: Request) -> dict:
    """FastAPI 依赖：业务路由强制鉴权，失败 401。返回用户 dict。"""
    token = extract_token(request)
    user = validate_token_cached(token)
    if not user:
        raise HTTPException(status_code=401, detail="未登录或会话已过期")
    return user


def get_current_user(request: Request) -> dict | None:
    """非抛错版，供 GET /app 重定向判断用。"""
    return validate_token_cached(extract_token(request))


# ── 会话 cookie：让页面级导航（GET /app）也能被服务端鉴权 ──
# token 服务端有效期 24h（见 database.user_db.login），cookie 生命周期与之对齐。
AUTH_COOKIE_NAME = "token"
AUTH_COOKIE_MAX_AGE = 24 * 3600


def set_auth_cookie(response, token: str, remember: bool = True) -> None:
    """把会话 token 写入 cookie，使浏览器导航到 /app 时携带、被服务端正确识别。

    httponly：防 XSS 读取 cookie 里的 token；
    samesite=lax：允许顶级导航（点击链接 / location.href）携带，同时阻断跨站
                  POST 携带，缓解 CSRF；
    path=/：全站可用；
    remember=False：会话 cookie（关闭浏览器即失效），与前端 sessionStorage 语义一致。
    """
    response.set_cookie(
        key=AUTH_COOKIE_NAME,
        value=token,
        max_age=AUTH_COOKIE_MAX_AGE if remember else None,
        httponly=True,
        samesite="lax",
        path="/",
    )
