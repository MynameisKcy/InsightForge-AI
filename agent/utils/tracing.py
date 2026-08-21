"""OpenTelemetry 初始化与 Span 工具（可观测性基建，见 docs/specs/2026-08-21-enterprise-observability-plan.md §2.2）。

开关逻辑（决策 D1）：未设置 OTEL_EXPORTER_OTLP_ENDPOINT 时保持 NoOp tracer——
本地开发零开销零报错；docker-compose 注入 endpoint 后自动启用真实导出。

使用方式：
    try:
        from agent.utils.tracing import get_tracer, traced
    except ModuleNotFoundError:
        from utils.tracing import get_tracer, traced

    @traced("sql.execute")
    def execute(query: str) -> dict: ...
"""
import contextlib
import os
import threading
import time
from functools import wraps

from opentelemetry import trace
from opentelemetry.context import attach as _ctx_attach
from opentelemetry.context import detach as _ctx_detach
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.trace import Status, StatusCode

_tracer = None
_init_lock = threading.Lock()

# Span 属性统一走 OTel 语义惯例的扁平 key，Jaeger 列表直接可读
ATTR_STATUS = "status"
ATTR_DURATION_MS = "duration_ms"
ATTR_ERROR = "error.message"


def init_tracing(service_name: str | None = None):
    """初始化 OTel（FastAPI 启动时调用一次）。endpoint 未配置时返回 NoOp tracer。"""
    global _tracer
    with _init_lock:
        if _tracer is not None:
            return _tracer
        endpoint = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT")
        if not endpoint:
            # 不挂 SDK provider：全局默认 ProxyTracer 无 processor，完全 no-op
            _tracer = trace.get_tracer(__name__)
            return _tracer
        resource = Resource.create({
            "service.name": service_name or os.getenv("OTEL_SERVICE_NAME", "insightforge"),
        })
        provider = TracerProvider(resource=resource)
        provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
        trace.set_tracer_provider(provider)
        _tracer = trace.get_tracer(__name__)
        return _tracer


def get_tracer():
    """获取全局 tracer（懒加载，线程安全）。"""
    global _tracer
    if _tracer is None:
        _tracer = init_tracing()
    return _tracer


def record_exception(span: trace.Span, exc: Exception) -> None:
    """异常统一记录：status=ERROR + record_exception，Jaeger 中红色高亮。"""
    span.set_status(Status(StatusCode.ERROR))
    span.set_attribute(ATTR_ERROR, str(exc)[:200])
    span.record_exception(exc)


def record_usage(span: trace.Span, usage: dict | None) -> None:
    """把 LangChain usage_metadata 写入 Span（缺失时静默跳过）。"""
    if usage:
        span.set_attribute("llm.input_tokens", usage.get("input_tokens", 0))
        span.set_attribute("llm.output_tokens", usage.get("output_tokens", 0))


def traced(span_name: str, **static_attrs):
    """通用装饰器：同步函数 Span + 计时 + 异常记录。

    Args:
        span_name: Span 名称（如 "sql.execute"）
        **static_attrs: 写入 Span 的静态属性（如 agent_name="sql"）
    """
    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            with get_tracer().start_as_current_span(span_name) as span:
                for k, v in static_attrs.items():
                    span.set_attribute(k, v)
                start = time.perf_counter()
                try:
                    result = func(*args, **kwargs)
                    span.set_attribute(ATTR_DURATION_MS, round((time.perf_counter() - start) * 1000, 1))
                    span.set_attribute(ATTR_STATUS, "success")
                    return result
                except Exception as e:
                    record_exception(span, e)
                    raise
        return wrapper
    return decorator


def span_context():
    """返回当前 trace_id（hex 32 位），无活动 Span 时返回空串。用于 SSE [TRACE] 事件。"""
    ctx = trace.get_current_span().get_span_context()
    if not ctx.is_valid:
        return ""
    return f"{ctx.trace_id:032x}"


def attach_current_span(name: str):
    """启动 Span 并设为当前上下文，返回 (span, token)。

    适用于无法用 with 包裹整段的场景（如含多个 yield 的异步生成器、
    循环内多返回点的方法）。收尾时调 detach_current_span(token) + span.end()。
    注意：start_as_current_span() 不进 with 块时返回的是上下文管理器而非 Span，
    手动场景必须用本函数。
    """
    span = get_tracer().start_span(name)
    token = _ctx_attach(trace.set_span_in_context(span))
    return span, token


def detach_current_span(token) -> None:
    """恢复 attach 前的上下文（token 由 attach_current_span 返回）。"""
    with contextlib.suppress(Exception):
        _ctx_detach(token)
