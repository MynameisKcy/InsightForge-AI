"""utils/sse_protocol.py 单元测试：帧构造 / 帧解析 / 带内块 + 跨语言锁步契约。

SSE 线协议词汇的单一属主是 utils/sse_protocol.py（Python 侧）与
api/static/js/app.js 的 SSE_PROTOCOL 字面量（JS 侧）。本文件后半部分
读取 app.js 源码做两侧词汇集合的双向比对——前端无测试基建，用此钉住锁步。
"""
import re
from pathlib import Path

from utils.sse_protocol import (
    CHART,
    DECISION,
    DONE,
    ERROR,
    KEEPALIVE,
    METRICS,
    SESSION,
    SESSIONS_RELOAD,
    STEP,
    STEP_TIMING,
    THINKING,
    TOKENS,
    TRACE,
    WRAPPED_TOKENS,
    frame,
    inband_thinking,
    parse_frame,
)

_APP_JS = Path(__file__).resolve().parent.parent / "api" / "static" / "js" / "app.js"


# ── token 清单 ──

def test_token_inventory_complete():
    assert TOKENS == {
        THINKING, SESSION, SESSIONS_RELOAD, CHART, STEP, KEEPALIVE,
        DONE, ERROR, TRACE, METRICS, DECISION, STEP_TIMING,
    }


# ── 帧构造（发射侧唯一入口；字节级对齐既有线格式）──

def test_frame_bare_token():
    assert frame(DONE) == "data: [DONE]\n\n"
    assert frame(KEEPALIVE) == "data: [KEEPALIVE]\n\n"
    assert frame(SESSIONS_RELOAD) == "data: [SESSIONS_RELOAD]\n\n"


def test_frame_payload_token():
    assert frame(THINKING, "解析问题中") == "data: [THINKING]解析问题中\n\n"
    assert frame(SESSION, "sess_42") == "data: [SESSION]sess_42\n\n"
    assert frame(TRACE, "abc123") == "data: [TRACE]abc123\n\n"


def test_frame_error_keeps_historic_space_separator():
    # 既有线格式：[ERROR] 后带一个空格（前端 slice(7)/基准 data[7:] 按此消费）
    assert frame(ERROR, "boom") == "data: [ERROR] boom\n\n"


def test_frame_wrapped_tokens():
    assert frame(CHART, "/reports/charts/a.html") == \
        "data: [CHART:/reports/charts/a.html]\n\n"
    assert frame(STEP, '{"type": "step", "index": 1}') == \
        'data: [STEP:{"type": "step", "index": 1}]\n\n'


# ── 帧解析（消费侧唯一入口；两种帧形）──

def test_parse_bare_and_space_forms():
    assert parse_frame("[DONE]") == (DONE, "")
    assert parse_frame("[KEEPALIVE]") == (KEEPALIVE, "")
    assert parse_frame("[ERROR] boom") == (ERROR, " boom")     # 保留前导空格=历史切片语义
    assert parse_frame("[THINKING]想一下") == (THINKING, "想一下")
    assert parse_frame("[SESSION]sess_42") == (SESSION, "sess_42")


def test_parse_wrapped_form_tolerates_brackets_in_payload():
    # 包裹式：尾 ']' 是终结符，payload 内部的 ']' 不截断
    js = '{"steps":[{"step":1}]}'
    assert parse_frame(f"[STEP:{js}]") == (STEP, js)
    assert parse_frame("[CHART:/reports/charts/x].html]") == (CHART, "/reports/charts/x].html")
    assert parse_frame('[METRICS:{"k":[1,2]}]') == (METRICS, '{"k":[1,2]}')
    # 畸形帧（缺终结符）：与现行 slice(6,-1) 一致——无条件剥掉行尾一个 ']'
    # （无法区分「payload 以 ] 结尾」与「payload+终结符」），交由 JSON.parse 失败兜底
    assert parse_frame("[STEP:{oops]") == (STEP, "{oops")


def test_parse_non_token_lines_fall_back_to_content():
    assert parse_frame("普通正文。") == ("", "普通正文。")
    # 非协议词（含中文/小写方括号文本）不是 token——回落正文
    assert parse_frame("[注] 这不是token") == ("", "[注] 这不是token")
    assert parse_frame("[note] lowercase") == ("", "[note] lowercase")
    assert parse_frame("[") == ("", "[")
    assert parse_frame("") == ("", "")


def test_round_trip_all_tokens():
    from utils.sse_protocol import DATA_PREFIX, FRAME_END
    for tok in sorted(TOKENS):
        # 按真实线格式取 payload：包裹式故意带内嵌 ']' 钉住「尾括号才是终结符」；
        # 纯裸 token（DONE/KEEPALIVE/SESSIONS_RELOAD）线上从不携带 payload——
        # parse 对 [TOKEN]xxx 按正文回落，与现网 JS `=== '[DONE]'` 语义一致
        if tok in WRAPPED_TOKENS:
            payload = "x]: y"
        elif tok in (DONE, KEEPALIVE, SESSIONS_RELOAD):
            payload = ""
        else:
            payload = "载荷"
        data = frame(tok, payload)
        # parse_frame 收「一行 data 内容」：剥 data: 前缀与帧尾空行
        body = data[len(DATA_PREFIX):len(data) - len(FRAME_END)]
        got_tok, got_payload = parse_frame(body)
        assert got_tok == tok
        if tok == ERROR:
            assert got_payload == " " + payload   # 历史空格分隔
        else:
            assert got_payload == payload


# ── 带内思考块（react_agent chunk 专用，无 data: 前缀）──

def test_inband_thinking_format():
    assert inband_thinking("查询数据库") == "[THINKING]查询数据库\n"


# ── 跨语言锁步契约：app.js 的 SSE_PROTOCOL 与 Python 侧双向一致 ──

def _js_protocol_literals() -> dict[str, str]:
    """从 app.js 提取 var SSE_PROTOCOL = { KEY: '[TOKEN]', ... } 字面量。"""
    src = _APP_JS.read_text(encoding="utf-8")
    m = re.search(r"var SSE_PROTOCOL = \{(.*?)\};", src, re.S)
    assert m, "app.js 缺少 SSE_PROTOCOL 协议表（与 utils/sse_protocol.py 锁步）"
    pairs = dict(re.findall(r"([A-Z_]+):\s*'(\[[A-Z_]+\])'", m.group(1)))
    assert pairs, "SSE_PROTOCOL 表为空或格式不符"
    return pairs


def test_js_protocol_matches_python_tokens():
    js = _js_protocol_literals()
    py = {f"[{tok}]" for tok in TOKENS}
    missing_in_js = py - set(js.values())
    unknown_in_js = set(js.values()) - py
    assert not missing_in_js, f"JS 缺少后端会下发的 token: {missing_in_js}"
    assert not unknown_in_js, f"JS 出现后端不存在的 token: {unknown_in_js}"


def test_js_has_frame_parser():
    src = _APP_JS.read_text(encoding="utf-8")
    assert "function parseSSEFrame" in src, "app.js 缺少统一帧解析器 parseSSEFrame"
