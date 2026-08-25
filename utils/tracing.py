"""OpenTelemetry 初始化与 Span 工具（可观测性基建，见 docs/specs/2026-08-21-enterprise-observability-plan.md §2.2）。

开关逻辑（决策 D1）：未设置 OTEL_EXPORTER_OTLP_ENDPOINT 时保持 NoOp tracer——
本地开发零开销零报错；docker-compose 注入 endpoint 后自动启用真实导出。

使用方式（traced 是全仓唯一的 Span 生命周期入口：计时/状态/异常自动收尾）：
    from utils.tracing import traced

    with traced("sql.generate", agent_name="sql") as span:
        span.set_attribute("sql.length", len(sql))   # 块内补属性
        ...
状态由返回值决定的场景用 mark_success=False 自管（如 planner.step）。
无法用 with 包裹整段的场景（异步生成器、循环多返回点）用
attach_current_span/detach_current_span 手动对。
"""
import contextlib
import os
import threading
import time

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


@contextlib.contextmanager
def traced(span_name: str, attrs: dict | None = None, *, mark_success: bool = True):
    """通用上下文管理器：Span + 计时 + 状态/异常自动收尾（全仓唯一生命周期入口）。

    Args:
        span_name: Span 名称（如 "llm.call"）
        attrs: 进入时写入的属性字典（键用 OTel 惯例的点号风格，如 {"agent.name": "sql"}）
        mark_success: 正常退出时是否写 status=success。状态由返回值决定的场景
            （如 planner.step 的 ok/error）传 False 由调用方自管。

    yield 出 Span 供块内补属性；异常统一 record_exception 后原样上抛。
    """
    with get_tracer().start_as_current_span(span_name) as span:
        for k, v in (attrs or {}).items():
            span.set_attribute(k, v)
        start = time.perf_counter()
        try:
            yield span
            span.set_attribute(ATTR_DURATION_MS, round((time.perf_counter() - start) * 1000, 1))
            if mark_success:
                span.set_attribute(ATTR_STATUS, "success")
        except Exception as e:
            record_exception(span, e)
            raise


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
