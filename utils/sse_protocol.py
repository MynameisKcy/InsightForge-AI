"""SSE 线协议词汇：token 常量 + 帧构造/解析的唯一属主（Python 侧锚点）。

/api/chat 的 SSE 协议 token 契约与 api/static/js/app.js 锁步：JS 侧的
``SSE_PROTOCOL`` 字面量必须与本模块 TOKENS 双向一致，由
tests/test_sse_protocol.py 的契约测试钉住。发射方（api/chat_stream.py、
agent/react_agent.py）与消费方（scripts/benchmark.py）一律经本模块构造/
解析帧，禁止手写 ``[TOKEN:`` 字面量与魔法切片。

两种帧形（``data: `` 前缀 + ``\\n\\n`` 结尾）：
  裸/空格式  [TOKEN] 或 [TOKEN]payload —— DONE/KEEPALIVE/SESSIONS_RELOAD、
             ERROR/THINKING/SESSION/TRACE（payload 可含 ']'）
  包裹式     [TOKEN:payload] —— STEP/METRICS/DECISION 的 JSON 与 CHART 的
             URL；行尾最后一个 ']' 是终结符，payload 内部可含 ']'
"""
import re

# ── data 行前缀 / 帧尾 ──
DATA_PREFIX = "data: "
FRAME_END = "\n\n"

# ── token 名（不带括号）──
THINKING = "THINKING"            # [THINKING]思考文案：状态行指示
SESSION = "SESSION"              # [SESSION]session_id：通知前端当前会话
SESSIONS_RELOAD = "SESSIONS_RELOAD"  # 无 payload：触发前端会话列表刷新
TRACE = "TRACE"                  # [TRACE]trace_id：Jaeger 链路检索用（前端忽略不渲染）
STEP = "STEP"                    # [STEP:{json}]：步骤清单事件（包裹式）
KEEPALIVE = "KEEPALIVE"          # 无 payload：心跳保活
DONE = "DONE"                    # 无 payload：流正常结束
ERROR = "ERROR"                  # [ERROR] {msg}：流异常结束（历史格式含一个空格分隔）
CHART = "CHART"                  # [CHART:url]：新图表 web URL（包裹式）
METRICS = "METRICS"              # [METRICS:{json}]：Token/成本看板累计值（包裹式）
DECISION = "DECISION"            # [DECISION:{json}]：决策卡片（工具调用/LLM 推理）
STEP_TIMING = "STEP_TIMING"      # [STEP_TIMING:{json}]：单阶段耗时(包装式),前端展示 per-step timing

#: 全部合法 token（消费方据此白名单放行；不在表内的 [xxx] 文本回落正文）
TOKENS = frozenset({
    THINKING, SESSION, SESSIONS_RELOAD, TRACE, STEP, KEEPALIVE,
    DONE, ERROR, CHART, METRICS, DECISION, STEP_TIMING,
})

#: 包裹式 token：帧形为 [TOKEN:payload]，尾 ']' 是终结符而非 payload 的一部分
WRAPPED_TOKENS = frozenset({STEP, CHART, METRICS, DECISION, STEP_TIMING})

_TOKEN_NAME = re.compile(r"[A-Z_]+")


def frame(token: str, payload: str = "") -> str:
    """发射侧唯一入口：构造完整 SSE 帧 ``data: [TOKEN]payload\n\n``。

    ERROR 保持历史线格式的空格分隔（``[ERROR] msg``，前端 slice(7) 与
    benchmark data[7:] 按此消费）；包裹式 token 走 ``[TOKEN:payload]``。
    """
    if token in WRAPPED_TOKENS:
        return f"{DATA_PREFIX}[{token}:{payload}]{FRAME_END}"
    sep = " " if token == ERROR else ""
    return f"{DATA_PREFIX}[{token}]{sep}{payload}{FRAME_END}"


def parse_frame(data: str) -> tuple[str, str]:
    """消费侧唯一入口：把一行 data 内容解析为 (token, payload)。

    非 token 行（正文、以 '[' 起始但非协议词的文本如 "[注]..."）返回
    ("", 原文)，调用方据 TOKENS 白名单决定放行为内容。
    """
    if not data.startswith("["):
        return "", data
    m = _TOKEN_NAME.match(data, 1)
    if m is None or m.end() == 1:  # 无大写 token 名 → 正文
        return "", data
    token = m.group(0)
    rest = data[m.end():]
    if rest.startswith(":"):   # 包裹式：剥掉行尾终结符 ']'
        payload = rest[1:]
        return token, payload[:-1] if payload.endswith("]") else payload
    if rest.startswith("]"):   # 裸/空格式：']' 后全是 payload
        return token, rest[1:]
    return "", data            # '[TOKEN' 后既非 ':' 也非 ']' → 正文


def inband_thinking(text: str) -> str:
    """带内思考块（ReactAgent yield 的 chunk，经 chat_stream.thinking_token 识别）。

    注意这是 chunk 流内标记而非完整 SSE 帧——无 data: 前缀与空行结尾。
    """
    return f"[{THINKING}]{text}\n"
