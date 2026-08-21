"""RAG 检索命中率评估（确定性，不依赖 ragas）。

对每条用例跑真实检索（retriever_docs：查询改写 → 粗召回 → rerank），
检查 ground_truth 关键词是否出现在检索结果中。
可作为 ragas 评估之外的快速回归信号（只花 embedding + rerank 费用，不花 LLM 生成）。

运行：cd agent && python -m pytest tests/rag_eval/test_rag_retrieval_hit.py -m rag_eval -v
"""
import pytest

pytestmark = pytest.mark.rag_eval

from tests.rag_eval.test_cases import TEST_CASES


def test_retrieval_hit_rate(retrieved_contexts):
    """检索命中率 >= 80%（20 条用例至少 16 条的关键词命中检索结果）。"""
    hits = 0
    misses = []
    for case in TEST_CASES:
        contexts = retrieved_contexts.get(case["question"], [])
        joined = "\n".join(contexts)
        if any(kw in joined for kw in case["keywords"]):
            hits += 1
        else:
            misses.append(case["question"])

    rate = hits / len(TEST_CASES)
    print(f"\n检索命中率: {hits}/{len(TEST_CASES)} = {rate:.0%}")
    if misses:
        print("未命中用例:")
        for q in misses:
            print(f"  - {q}")
    assert rate >= 0.80, f"检索命中率 {rate:.0%} < 80%，未命中: {misses}"


def test_retrieval_non_empty(retrieved_contexts):
    """每条用例都应检索到非空上下文（空检索直接判失败，排查 embed/入库问题）。"""
    empty = [q for q, ctxs in retrieved_contexts.items() if not ctxs]
    assert not empty, f"以下问题检索结果为空: {empty}"


if __name__ == "__main__":
    pytest.main([__file__, "-m", "rag_eval", "-v", "-s"])
