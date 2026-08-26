"""统一错误信封（架构评审 R2 候选7）。

全仓唯一的错误响应构造点 error_response + app 级异常处理器注册：
- HTTPException（含 auth 401）的 {"detail"} → 规范信封，前端可读到消息；
- RequestValidationError 的 pydantic detail 数组 → 压成一句可读字符串；
- 未捕获异常 → 通用 500 信封（记日志，不泄漏内部信息）。

规范信封是历史各形态的超集：{"error"} / {"success","error"} / {"ok","error"}
的消费者（前端一律读 .error）零改动兼容。成功体形态不属于错误信封范畴。
"""
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from utils.logger_handler import logger


def error_response(message: str, status_code: int = 400, **extra) -> JSONResponse:
    """规范错误信封：{"success": False, "error": message, **extra}。"""
    return JSONResponse({"success": False, "error": message, **extra},
                        status_code=status_code)


def register_exception_handlers(app: FastAPI) -> None:
    """在组合根（fastapi_server）调用一次，接管三类异常的响应形态。"""

    @app.exception_handler(StarletteHTTPException)
    async def _http_exception(request: Request, exc: StarletteHTTPException):
        return error_response(str(exc.detail), exc.status_code)

    @app.exception_handler(RequestValidationError)
    async def _validation_error(request: Request, exc: RequestValidationError):
        parts = []
        for err in exc.errors():
            loc = ".".join(str(x) for x in err.get("loc", []) if x != "body")
            msg = str(err.get("msg", "invalid"))
            parts.append(f"{loc}: {msg}" if loc else msg)
        return error_response("；".join(parts) or "请求参数无效", 422)

    @app.exception_handler(Exception)
    async def _unhandled_exception(request: Request, exc: Exception):
        logger.error(
            f"Unhandled exception on {request.method} {request.url.path}: "
            f"{type(exc).__name__}: {exc}"
        )
        return error_response("Internal Server Error", 500)
