"""DashScope gte-rerank-v2 精排的统一实现（memory.recall 与 rag_service 共享）。

两处调用方原本各持一份近重复实现；此处抽出单一算法：
候选 <= top_n 早退 → TextReRank.call → status/output 校验 → index 合法性 +
score 阈值过滤 → 写 rerank_score metadata；任意失败/不足回退粗召回前 top_n。
"""

import os

from utils.logger_handler import logger


def rerank_docs(query, docs, top_n, model, score_threshold):
    """gte-rerank-v2 精排 docs（按 query 相关性）；失败/不足回退 docs[:top_n]。

    Args:
        query: 用户查询文本。
        docs: 候选 Document 列表（需有 .page_content / .metadata）。
        top_n: 取前 N 条。
        model: rerank 模型名（如 "gte-rerank-v2"）。
        score_threshold: 最低 relevance_score，低于则丢弃。
    Returns:
        精排后的 Document 列表（写入 metadata["rerank_score"]）；失败回退 docs[:top_n]。
    """
    if not docs:
        return []
    if len(docs) <= top_n:
        return docs[:top_n]
    try:
        from dashscope import TextReRank
        resp = TextReRank.call(
            model=model,
            query=query,
            documents=[d.page_content for d in docs],
            top_n=top_n,
            return_documents=False,
            api_key=os.getenv("DASHSCOPE_API_KEY"),
        )
        status = getattr(resp, "status_code", None)
        output = getattr(resp, "output", None)
        if status != 200 or not output or not output.get("results"):
            code = getattr(resp, "code", "")
            message = getattr(resp, "message", "")
            logger.warning(
                "rerank 未返回有效结果 (status=%s code=%s msg=%s)，回退前 %d 条",
                status, code, message, top_n,
            )
            return docs[:top_n]
        reranked = []
        for item in output.get("results", []):
            idx = item.get("index")
            score = item.get("relevance_score", 0)
            if idx is None or idx < 0 or idx >= len(docs):
                continue
            if score < score_threshold:
                continue
            d = docs[idx]
            d.metadata["rerank_score"] = score
            reranked.append(d)
        logger.info(
            "rerank: %d 候选 -> %d 精排 (阈值 %.2f)", len(docs), len(reranked), score_threshold
        )
        return reranked if reranked else docs[:top_n]
    except Exception as e:
        logger.warning("rerank 调用失败，回退粗召回: %s", e)
        return docs[:top_n]
