"""
意图路由器：对用户 query 做三档分类 —— query / analysis / chat。

分类策略（主路径 = LLM few-shot，规则为 fallback）：
- 主分类 = classify_intent_llm（LLM few-shot，语义上限更高；含"确认语前缀 +
  分析意图"这类规则处理不了的边界）。在 wrap_model_call 中间件热路径里跑，
  每条新消息首字前多一次模型往返；middleware 按 runtime.context 缓存，
  同轮多次模型调用只分类一次，不破坏流式节奏。
- LLM 异常/超时 → 回退规则分类（classify_intent，关键词+长度+问号判定）；
  两层都挂才落默认档。middleware 永不因分类失败而挂。
- 无法判定默认 analysis（与项目"解析失败走全任务"策略一致——宁愿重一点
  也别让用户点完按钮发现没生成报告）。
- 可测：规则路径为纯函数 + dataclass（15+ 用例）；LLM 路径 mock 覆盖
  （映射 / schema 失败 / 回退 / 归一化透传）。

工具集选择策略（与 middleware 配合）：
- chat 档：会话/闲聊工具 + RAG（业务术语出口）+ 报告上下文触发器
  （fill_report_context_for_report 是态切换，不应被裁掉，否则报告生成旁路会断）
- query 档：单点数据查询/快速洞察/文件列表/文档报告等查询类工具
- analysis 档：全集（含 run_full_analysis）
档位目录与推导见 agent_tools.for_intent（ADR-0004）：每个工具声明固有 min_intent，
模型可见集 = rank(min_intent) <= rank(intent) 的全部工具；本模块不再维护工具清单。

为什么不把 CHAT 抽到只剩 get_current_month：
实际使用中用户会问"X 是什么"（业务术语）→ 期望查 RAG；问"我上个月用得怎么样"
→ 期望查 RAG + 报告旁路。CHAT 完全不调 RAG 会逼用户在"闲聊"和"知识问答"之间二选一。
"""

import re
from collections.abc import Iterable
from dataclasses import dataclass
from enum import Enum

from utils.logger_handler import logger


class Intent(str, Enum):
    CHAT = "chat"
    QUERY = "query"
    ANALYSIS = "analysis"


@dataclass(frozen=True)
class IntentResult:
    intent: Intent
    matched_rule: str
    confidence: str


# ── 关键词集合（双语 + 大小写不敏感）──
# 触发 ANALYSIS 的高权重词：分析/报告/趋势/出图/可视化 等显式意图
_ANALYSIS_KEYWORDS: tuple[str, ...] = (
    # 中文动词/名词
    "分析", "对比", "比较", "评估", "诊断", "洞察", "趋势", "规律", "分布", "占比",
    "相关性", "报告", "看板", "仪表盘", "画像",
    # 中文图表/可视化
    "画", "出图", "作图", "可视化", "图表", "趋势图", "对比图", "饼图", "柱状图", "折线图",
    "散点图", "热力图",
    # 中文"完整链路"暗示
    "生成分析", "生成报告", "出报告", "做一份", "写一份",
    # 英文
    "analyze", "analysis", "compare", "comparison", "trend", "distribution",
    "report", "visualize", "visualization", "chart", "graph", "plot", "dashboard",
    "insight",
)

# 触发 QUERY 的中权重词：X 是多少/谁最多 等单点查询
_QUERY_KEYWORDS: tuple[str, ...] = (
    # 中文疑问
    "多少", "几个", "几个", "多大", "多高", "多低", "哪里", "哪个", "哪些", "何时", "几次",
    "总数", "合计", "平均", "最大", "最小", "最高", "最低", "最多", "最少",
    "排名第", "排第几", "top", "Top", "TOP",
    # 中文单点查询动词
    "查", "查一下", "查询", "看下", "看看", "告诉我", "帮我找", "查一查",
    # 英文
    "how many", "how much", "which", "where", "when", "count", "sum", "avg",
    "average", "max", "min", "top", "bottom",
)

# 触发 CHAT 的低权重词：问候/感谢/澄清/不知道
_CHAT_KEYWORDS: tuple[str, ...] = (
    # 问候
    "你好", "您好", "hi", "hello", "hey", "在吗", "在么",
    # 致谢
    "谢谢", "感谢", "thx", "thanks", "thank you",
    # 澄清/确认
    "好的", "明白", "懂了", "ok", "OK", "okay", "是的", "对", "没错", "收到",
    # 不知道/不想答
    "不知道", "不清楚", "随便", "都行", "无所谓",
)


def _strip_whitespace(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


# 会话引导词（按长度降序匹配）：礼貌/确认/请求引导前缀。
# 归一化只剥前缀；英文词带词边界（okay 不能被 ok 剥掉）。
_PREFIX_STRIPS: tuple[str, ...] = (
    "麻烦您", "麻烦你", "麻烦", "帮我一下", "帮我", "请帮我", "请",
    "好的呀", "好的呢", "好嘞", "好的", "好",
    "明白了", "明白", "知道了", "知道啦", "收到", "嗯嗯", "嗯",
    "你好", "您好", "hello", "hi", "hey", "哈喽", "嗨",
    "谢谢", "感谢", "thanks", "thx",
    "okay", "ok", "好的没问题", "没问题", "可以",
    "我想问一下", "想问一下", "我想", "我想要", "请问", "那个",
)


def _normalize_query(text: str) -> str:
    """剥离首部会话引导词，返回任务主体（供规则与 LLM 分类共用）。

    只处理前缀；剥离后为空（纯会话语）返回原文，让问候/确认规则按原文判定。
    "好的，帮我分析一下销售趋势" → "分析一下销售趋势"：
    关键词判定吃主体、闲聊判定吃原文，两条路径都受益。
    """
    s = (text or "").strip()
    changed = False
    while s:
        for p in _PREFIX_STRIPS:
            pl = p.lower()
            if s.lower().startswith(pl):
                # 英文词边界：ok 不剥 okay / ok_xxx；中文前缀无此问题
                if p.isascii() and len(s) > len(p) and s[len(p)].isalnum():
                    continue
                s = s[len(p):].lstrip(" ，,、：:；;")
                changed = True
                break
        else:
            break
    return s if (s and changed) else (text or "").strip()


def _has_any(text: str, keywords: Iterable[str]) -> tuple[bool, str]:
    """大小写不敏感地检查 text 是否包含 keywords 任一词。返回 (命中, 命中的词)。"""
    lower = text.lower()
    for kw in keywords:
        if kw.lower() in lower:
            return True, kw
    return False, ""


def _has_question_mark(text: str) -> bool:
    """末尾带问号/问号变体（中文？/英文?）。"""
    return bool(re.search(r"[?？]\s*$", text.strip()))


def _starts_with_greeting_or_thanks(text: str) -> bool:
    """首段是问候/致谢/确认——典型闲聊入口。"""
    head = text[:8].lower()
    starters = (
        "你好", "您好", "hello", "hi ", "hey ", "thanks", "thank you", "thx",
        "谢谢", "感谢", "好的", "ok", "okay", "明白", "懂了", "收到",
    )
    return any(head.startswith(s.lower()) for s in starters)


def classify_intent(query: str) -> IntentResult:
    """对用户 query 做三档意图分类。

    判定优先级（高 → 低）：
    1. 显式分析/出图/报告关键词 → ANALYSIS（高置信）
    2. 问候/致谢开头（且无分析关键词）→ CHAT
    3. 单点查询关键词 + 问号 → QUERY
    4. 短问句（< 20 字 + 问号）→ QUERY
    5. 显式查询词（查/查询/top/count）→ QUERY
    6. 默认 → ANALYSIS（"解析失败走全任务"项目策略）

    规则 1 高于规则 2 的原因：「好的，帮我分析一下销售趋势」这类
    「先确认上轮、再发新分析指令」的对话句式很常见——若问候前缀优先，
    run_full_analysis 会被裁掉，模型只能闲聊式回答（误路由实锤修正）。
    纯问候（"你好"/"谢谢"/"好的"）不含分析关键词，仍落 CHAT。

    Returns:
        IntentResult(intent, matched_rule, confidence)
    """
    text = _strip_whitespace(query)
    if not text:
        # 空 query 在 chat 主循环里就不会调 ReAct；兜底返回 ANALYSIS
        return IntentResult(Intent.ANALYSIS, "empty", "default")

    # 归一化：剥首部会话引导词（好的/请/帮我/你好/OK…）。
    # 关键词判定（规则 1/3/4/5/6）吃主体，闲聊判定（规则 2）吃原文——
    # 引导词本身就是闲聊信号，不该掩盖其后的任务意图。
    body = _normalize_query(text)

    # 规则 1：显式分析/可视化关键词 → ANALYSIS（高置信）
    hit, kw = _has_any(body, _ANALYSIS_KEYWORDS)
    if hit:
        return IntentResult(Intent.ANALYSIS, f"keyword:{kw}", "high")

    # 规则 2：问候/致谢开头 → CHAT（高置信；用原文，纯会话语不被归一化吞掉）
    if _starts_with_greeting_or_thanks(text):
        return IntentResult(Intent.CHAT, "greeting", "high")

    # 规则 3：强单点查询信号（"X 是多少/谁最多/top N/count/查"）→ QUERY
    #   - 不强制问号：用户口语 "3月销售多少" 不一定带问号
    #   - 但 "多少" 这种泛词需要组合约束，否则会误吞"还有多少没分析"
    #   - 解决：把强信号词（多少/几/top/count/查/查一下 等）单独一组，命中即 QUERY
    _strong_query_signals = (
        "多少", "几个", "多大", "多高", "多低", "排名第", "top", "Top", "TOP",
        "总数", "合计", "最大", "最小", "最多", "最少", "最高", "最低",
        "查", "查一下", "查一查", "查询", "看下", "看看", "count", "sum", "avg",
    )
    hit, kw = _has_any(body, _strong_query_signals)
    if hit:
        # 进一步过滤：ANALYSIS 关键词优先（避免"分析 3 月销售多少"被错判）
        analysis_hit, _ = _has_any(body, _ANALYSIS_KEYWORDS)
        if analysis_hit:
            return IntentResult(Intent.ANALYSIS, f"analysis_wins:{kw}", "high")
        return IntentResult(Intent.QUERY, f"keyword:{kw}", "high")

    # 规则 4：其他 QUERY 关键词 + 问号（"哪些"/"何时"等）→ QUERY（中置信）
    hit, kw = _has_any(body, _QUERY_KEYWORDS)
    if hit and _has_question_mark(body):
        return IntentResult(Intent.QUERY, f"keyword+question:{kw}", "medium")

    # 规则 5：短问句（< 20 字 + 问号）→ QUERY（中置信）
    if len(body) < 20 and _has_question_mark(body):
        return IntentResult(Intent.QUERY, "short_question", "medium")

    # 规则 6：超长 query（> 80 字）几乎一定是复杂需求 → ANALYSIS（低置信）
    if len(body) > 80:
        return IntentResult(Intent.ANALYSIS, "long_query", "low")

    # 默认：走全任务（项目策略：解析失败走全链路，宁重勿轻）
    return IntentResult(Intent.ANALYSIS, "default", "default")


def is_intent(text: str, intent: Intent) -> bool:
    """便捷包装：query 是否属于指定意图。"""
    return classify_intent(text).intent == intent


# ── LLM few-shot 分类器（主路径）────────────────────────────
# 主分类交给 LLM few-shot：语义理解上限更高，few-shot 样本从规则词表
# 提炼（含"确认语前缀 + 分析意图"这类规则处理不了的边界）。
# 规则层定位：LLM 不可用/超时/校验失败时的 fallback（热路径免费路径，
# 但非主分类）。
#
# 延迟代价：LLM 分类在首字之前多一次模型往返，middleware 按 runtime.context
# 缓存（同轮只跑一次）；失败回退规则（middleware 永不挂）。

INTENT_SCHEMA: dict = {
    "type": "object",
    "required": ["intent"],
    "properties": {
        "intent": {"type": "enum", "values": ("chat", "query", "analysis")},
    },
}

_INTENT_FEWSHOT_BODY = """你是意图分类器。判断用户最新一条消息的意图，只输出 JSON。

三类定义：
- chat：问候、致谢、确认、闲聊、追问澄清——没有数据任务
- query：单点数据查询，一步可答（问某个数值/排名/统计）
- analysis：多步分析、对比、趋势、出图、生成报告

示例：
用户：你好
意图：chat

用户：谢谢
意图：chat

用户：好的，明白了
意图：chat

用户：3月销售多少？
意图：query

用户：哪个地区人口最多？
意图：query

用户：TOP 5 客户是哪些？
意图：query

用户：分析3月销售趋势
意图：analysis

用户：对比各区人口分布
意图：analysis

用户：生成一份季度报告并导出
意图：analysis

用户：好的，帮我分析一下销售趋势
意图：analysis

注意：即使开头有"好的/明白/OK/收到"等确认语，只要后面是明确的数据任务，仍按任务意图分类。

只输出 JSON：{"intent": "chat" 或 "query" 或 "analysis"}
"""


def _build_classify_messages(query: str) -> list[dict]:
    """few-shot prompt + 当前 query（字符串拼接，避免 JSON 示例花括号被 format 吞掉）。"""
    return [{"role": "user", "content": _INTENT_FEWSHOT_BODY + "\n用户：" + (query or "") + "\n意图："}]


def classify_intent_llm(query: str, user_id: str | None = None) -> IntentResult:
    """LLM few-shot 分类（主路径）。schema 校验不过抛 ValueError（调用方回退规则）。

    入参为归一化后的主体（classify_with_fallback 负责剥离引导词）；
    每次调用 1 次模型往返；middleware 层按 runtime.context 缓存同轮结果。
    """
    from agents.base import BaseAgent

    body = _normalize_query(query)
    agent = BaseAgent(user_id=user_id)
    out = agent._call_llm_with_schema(_build_classify_messages(body), INTENT_SCHEMA, retries=0)
    if out is None:
        raise ValueError("intent classification failed schema validation")
    intent = Intent(str(out.get("intent", "")).strip().lower())
    return IntentResult(intent, "llm_fewshot", "high")


def classify_with_fallback(query: str, user_id: str | None = None) -> IntentResult:
    """LLM few-shot 优先，异常/超时回退规则分类（middleware 热路径入口）。

    归一化在此做一次（剥首部会话引导词），两条路径吃同一主体。
    """
    body = _normalize_query(query)
    try:
        return classify_intent_llm(body, user_id=user_id)
    except Exception as e:
        logger.warning(f"[intent] LLM classify failed, fallback to rules: {e}")
        return classify_intent(body)
