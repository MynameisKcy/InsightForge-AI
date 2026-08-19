"""
Query Rewriter（分析桥接处）：在 PlannerAgent 入口、_create_plan 之前，结合
近期对话历史把用户当前 query 改写为**自包含**的分析需求，消解代词/指代
（"它/这个/那个/上个月/刚才说的产品"），使多轮 query（如"分析它的趋势"）
以无歧义形式进入规划。

独立组件（非 BaseAgent 子类）：它不是流水线阶段，而是 _create_plan 之前的
预处理步骤。失败/无历史时回退原始 query，保证可用性（与 rerank 降级策略一致）。
设计见 docs/adr/0002-query-rewriting-two-points.md。
"""

from model.factory import get_chat_model
from utils.logger_handler import logger

REWRITE_SYSTEM_PROMPT = """你是一个查询改写器。给定用户的当前问题与最近的对话历史，将当前问题改写为一个自包含的数据分析需求——消解其中的代词与指代（如"它/这个/那个/上个月/刚才说的产品"等），使改写后的问题脱离对话上下文也能被独立理解。

规则：
1. 只根据历史消解指代，不得新增用户未表达的分析范围或意图，不得臆造数据。
2. 不要回答问题，不要解释，不要输出 JSON 或列表。
3. 直接输出改写后的问题，一句话，不加引号或前缀。
4. 若当前问题已自包含、无需改写，原样输出。
"""

# LLM 常见的包裹/前缀，输出时清理掉
_QUOTE_CHARS = '"\'“”‘’「」『』'
_PREFIXES = ("改写后的问题：", "改写后的问题:", "改写：", "改写:", "结果：", "结果:")


class QueryRewriter:
    """查询改写器（分析桥接处）：消解对话指代，产出自包含 query。

    standalone 组件，复用 get_chat_model(user_id) 的按用户隔离 LLM。
    不继承 BaseAgent：BaseAgent 的 run()->dict 契约面向 _agent_map 流水线阶段，
    而本组件是 _create_plan 之前的预处理，返回 str。
    """

    def __init__(self, user_id: str | None = None):
        self.user_id = user_id
        self.model = get_chat_model(user_id)

    @staticmethod
    def _format_history(history: list[dict]) -> str:
        if not history:
            return "（无历史）"
        lines = []
        for h in history[-12:]:  # 最近 12 条消息（≈6 轮）
            role = "用户" if h.get("role") == "user" else "助手"
            content = str(h.get("content", ""))[:300]
            lines.append(f"{role}: {content}")
        return "\n".join(lines)

    @staticmethod
    def _clean(text: str) -> str:
        text = text.strip().strip(_QUOTE_CHARS)
        for prefix in _PREFIXES:
            if text.startswith(prefix):
                text = text[len(prefix):].strip()
        return text.strip(_QUOTE_CHARS)

    def _call_llm(self, messages: list[dict]) -> str:
        from langchain_core.messages import HumanMessage, SystemMessage

        lc_messages = []
        for m in messages:
            if m["role"] == "system":
                lc_messages.append(SystemMessage(content=m["content"]))
            elif m["role"] == "user":
                lc_messages.append(HumanMessage(content=m["content"]))
        return self.model.invoke(lc_messages).content.strip()

    def rewrite(self, query: str, history: list[dict] | None = None) -> str:
        """返回自包含的改写 query；无历史/失败/空结果时回退原始 query。"""
        if not query:
            return query
        # 无历史则无指代可消解，直接原样返回（同时避免无谓的 LLM 调用）
        if not history:
            return query
        messages = [
            {"role": "system", "content": REWRITE_SYSTEM_PROMPT},
            {"role": "user", "content": (
                f"最近对话历史：\n{self._format_history(history)}\n\n"
                f"当前问题：{query}\n\n改写后的问题："
            )},
        ]
        try:
            rewritten = self._clean(self._call_llm(messages))
            if rewritten and rewritten != query.strip():
                logger.info('[query_rewrite] "%s" -> "%s"', query, rewritten)
                return rewritten
            logger.info('[query_rewrite] "%s" (unchanged)', query)
            return query
        except Exception as e:
            logger.warning("Query rewrite failed, using original query: %s", e)
            return query
