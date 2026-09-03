"""tracing 模块契约测试（架构评审 R2 候选4：观测埋点收口）。

钉住 traced() 上下文管理器的生命周期契约：计时、成功/失败状态、静态属性、
mark_success=False 自管状态分支，以及 NoOp 开关（无 OTLP endpoint 时零开销零报错）。
不触达全局 TracerProvider（OTel 全局 provider 进程内仅可设置一次）：测试用本地
TracerProvider + InMemorySpanExporter，经 monkeypatch 替换模块级 _tracer。
"""

import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode

import utils.tracing as tracing


@pytest.fixture
def exporter(monkeypatch):
    """用本地可观测的 tracer 替换模块级 _tracer，yield 已导出 span 的读取口。"""
    exp = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exp))
    monkeypatch.setattr(tracing, "_tracer", provider.get_tracer("test"))
    return exp


def _last(exporter):
    spans = exporter.get_finished_spans()
    assert spans, "没有已结束的 span"
    return spans[-1]


def test_traced_success_sets_static_mid_attrs_and_status(exporter):
    """正常路径：attrs 字典静态属性（支持 OTel 点号键）落盘、块内补属性生效、status=success、计时非负。"""
    with tracing.traced("unit.op", attrs={"unit.name": "sql"}) as span:
        span.set_attribute("unit.mid", 7)

    s = _last(exporter)
    assert s.name == "unit.op"
    assert s.attributes["unit.name"] == "sql"
    assert s.attributes["unit.mid"] == 7
    assert s.attributes[tracing.ATTR_STATUS] == "success"
    assert s.attributes[tracing.ATTR_DURATION_MS] >= 0


def test_traced_exception_marks_error_and_reraises(exporter):
    """异常路径：status=ERROR + error.message 属性 + 异常事件，且原异常向上抛。"""
    with pytest.raises(RuntimeError, match="boom"), tracing.traced("unit.fail"):
        raise RuntimeError("boom")

    s = _last(exporter)
    assert s.status.status_code == StatusCode.ERROR
    assert "boom" in s.attributes[tracing.ATTR_ERROR]
    assert any(e.name == "exception" for e in s.events)


def test_traced_mark_success_false_leaves_status_to_caller(exporter):
    """mark_success=False（planner.step 场景）：状态由返回值决定，CM 不得覆盖。"""
    with tracing.traced("planner.step", mark_success=False) as span:
        span.set_attribute(tracing.ATTR_STATUS, "error")

    s = _last(exporter)
    assert s.attributes[tracing.ATTR_STATUS] == "error"


def test_record_usage_writes_token_attrs(exporter):
    """usage_metadata 存在时写入 llm.input/output_tokens。"""
    with tracing.traced("llm.call") as span:
        tracing.record_usage(span, {"input_tokens": 11, "output_tokens": 7})

    s = _last(exporter)
    assert s.attributes["llm.input_tokens"] == 11
    assert s.attributes["llm.output_tokens"] == 7


def test_record_usage_none_is_silent(exporter):
    """usage 缺失时静默跳过，不写 token 属性也不抛。"""
    with tracing.traced("llm.call") as span:
        tracing.record_usage(span, None)

    s = _last(exporter)
    assert "llm.input_tokens" not in s.attributes


def test_noop_mode_without_endpoint(monkeypatch):
    """未配置 OTLP endpoint：懒初始化得到 NoOp tracer，CM 正常进出、异常照抛、无 trace_id。"""
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    monkeypatch.setattr(tracing, "_tracer", None)

    with tracing.traced("unit.noop"):
        assert tracing.span_context() == ""  # NoOp span 无有效上下文
    with pytest.raises(ValueError, match="x"), tracing.traced("unit.noop2"):
        raise ValueError("x")


def test_span_context_empty_without_active_span(exporter):
    """有真实 tracer 但无活动 span 时返回空串（SSE [TRACE] 不下发）。"""
    assert tracing.span_context() == ""


def test_attach_current_span_roundtrip(exporter):
    """手动场景契约：attach 后 span_context 返回 32 位 hex 且与 span 一致，detach/end 收尾。"""
    span, token = tracing.attach_current_span("http.request")
    try:
        tid = tracing.span_context()
        assert len(tid) == 32
        assert tid == f"{span.get_span_context().trace_id:032x}"
    finally:
        tracing.detach_current_span(token)
        span.end()

    assert tracing.span_context() == ""
