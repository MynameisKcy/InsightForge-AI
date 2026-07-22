"""统一鉴权：require_auth 依赖 + validate_token 进程内短缓存。"""
import time
from fastapi import Request, HTTPException

try:
    from database.user_db import user_db
except ImportError:
    from agent.database.user_db import user_db

# token -> (user_dict, expire_ts)；30s 短缓存，token 24h 有效，安全
_CACHE_TTL = 30.0
_token_cache: dict[str, tuple[dict, float]] = {}


def extract_token(request: Request) -> str | None:
    """从 Authorization: Bearer <token> 提取 token。"""
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return None


def validate_token_cached(token: str | None) -> dict | None:
    """包 LRU 短缓存的 validate_token。命中不查库。"""
    if not token:
        return None
    now = time.time()
    cached = _token_cache.get(token)
    if cached and cached[1] > now:
        return cached[0]
    user = user_db.validate_token(token)
    _token_cache[token] = (user, now + _CACHE_TTL)
    # 简易清理：缓存超 1000 项时丢掉过期项
    if len(_token_cache) > 1000:
        for k in [k for k, v in _token_cache.items() if v[1] <= now]:
            _token_cache.pop(k, None)
    return user


def invalidate_token(token: str | None) -> None:
    """Evict a token from the LRU cache (call on logout / revocation)."""
    if token:
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
