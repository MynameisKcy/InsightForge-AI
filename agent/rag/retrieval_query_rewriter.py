"""
Retrieval Query Rewriter（RAG 检索处）：在 rag_service.retriever_docs 的粗召回
之前做**多查询扩展**——生成 N 条语义相关但表述不同的改写，配合原始 query 多路
粗召回并合并去重，扩大候选池的召回率；精排（rerank）仍用原始 query 打分，
保证精度不受影响。

独立组件（非 BaseAgent 子类）：仅做 query 扩展，不参与流水线。
失败时回退为 [原始 query]，检索行为退化为现状（与 rerank 降级策略一致）。
设计见 docs/adr/0002-query-rewriting-two-points.md。
"""
import os
import sys

CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(CURRENT_DIR)
for path in (PROJECT_ROOT, os.path.dirname(PROJECT_ROOT)):
    if path not in sys.path:
        sys.path.insert(0, path)

try:
    from agent.model.factory import get_chat_model
    from agent.utils.logger_handler import logger
except ModuleNotFoundError:
    from model.factory import get_chat_model
    from utils.logger_handler import logger


EXPAND_SYSTEM_PROMPT = """你是一个检索查询扩展器。给定一个检索查询，生成 3 条语义相关但表述不同的改写，用于向量检索召回更多相关文档。

规则：
1. 每条改写换一种表述/视角（同义词替换、口语化/书面化转换、补全隐含实体等），不要简单重复原句。
2. 不要回答查询，不要解释，不要输出多余的编号或标点。
3. 每行一条改写，共 3 行，按行输出。
"""

_QUOTE_CHARS = '"\'“”‘’「」『』'


class RetrievalQueryRewriter:
    """检索查询扩展器：多查询改写，扩大 RAG 粗召回召回率。

    standalone 组件。模型按 user_id 延迟取用（expand 时传 user_id），
    以适配 RagSummarizerService 单例下仍能按用户隔离 LLM 配置。
    """

    DEFAULT_N = 3

    def __init__(self, n: int | None = None):
        self.n = n or self.DEFAULT_N

    def _call_llm(self, messages: list[dict], user_id: str | None = None) -> str:
        from langchain_core.messages import HumanMessage, SystemMessage

        model = get_chat_model(user_id)
        lc_messages = []
        for m in messages:
            if m["role"] == "system":
                lc_messages.append(SystemMessage(content=m["content"]))
            elif m["role"] == "user":
                lc_messages.append(HumanMessage(content=m["content"]))
        return model.invoke(lc_messages).content.strip()

    @staticmethod
    def _clean_line(line: str) -> str:
        # 去掉行首编号（"1." "1、" "1)" "1）"）与引号包裹
        line = line.strip()
        i = 0
        while i < len(line) and (line[i].isdigit() or line[i] in ".、)）"):
            i += 1
        line = line[i:].strip()
        return line.strip(_QUOTE_CHARS).strip()

    def expand(self, query: str, user_id: str | None = None) -> list[str]:
        """返回 [原始 query, 改写1, ...]；失败时回退为 [原始 query]。"""
        if not query:
            return [query] if query else []
        messages = [
            {"role": "system", "content": EXPAND_SYSTEM_PROMPT},
            {"role": "user", "content": f"检索查询：{query}\n\n3 条改写："},
        ]
        try:
            raw = self._call_llm(messages, user_id)
            paraphrases = [self._clean_line(line) for line in raw.splitlines()]
            paraphrases = [p for p in paraphrases if p and p != query.strip()]
            # 去重保序，限制到 n 条
            seen: set[str] = set()
            unique: list[str] = []
            for p in paraphrases:
                if p not in seen:
                    seen.add(p)
                    unique.append(p)
                if len(unique) >= self.n:
                    break
            expanded = [query] + unique
            logger.info(
                '[retrieval_expand] "%s" -> %d queries: %s',
                query, len(expanded), expanded,
            )
            return expanded
        except Exception as e:
            logger.warning("Query expand failed, using original query only: %s", e)
            return [query]
