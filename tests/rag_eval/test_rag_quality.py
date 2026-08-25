"""RAG 端到端质量评估（ragas：faithfulness / answer_relevancy / context_precision）。

流程：真实检索（retriever_docs）得 contexts → 真实生成链（svc.chain）得 answer →
ragas evaluate（judge LLM 复用项目模型工厂）。依赖 agent/requirements-eval.txt，
未安装 ragas 时跳过（快速回归请用 test_rag_retrieval_hit.py）。

运行：
    pip install -r agent/requirements-eval.txt
    cd agent && python -m pytest tests/rag_eval/test_rag_quality.py -m rag_eval -v -s

通过标准（与 docs/specs 方案 §5.3 一致）：
    faithfulness >= 0.80  answer_relevancy >= 0.85  context_precision >= 0.75
"""
import pytest

pytestmark = pytest.mark.rag_eval

ragas = pytest.importorskip("ragas", reason="未安装评估依赖：pip install -r agent/requirements-eval.txt")

from tests.rag_eval.test_cases import TEST_CASES


@pytest.fixture(scope="session")
def eval_rows(rag_service, retrieved_contexts):
    """构造 ragas 数据集行：question / answer（真实生成）/ contexts（真实检索）/ ground_truth。"""
    rows = []
    for case in TEST_CASES:
        contexts = retrieved_contexts[case["question"]]
        context_text = "\n".join(f"[参考资料{i}] {c}" for i, c in enumerate(contexts, 1))
        # 与 rag_summarize 内部一致的生成方式（chain.invoke），避免二次检索
        answer = str(rag_service.chain.invoke({"input": case["question"], "context": context_text}))
        rows.append({
            "question": case["question"],
            "answer": answer,
            "contexts": contexts,
            "ground_truth": case["ground_truth"],
        })
    return rows


def test_rag_quality(eval_rows):
    from ragas import EvaluationDataset, evaluate
    from ragas.llms import LangchainLLMWrapper
    from ragas.metrics import answer_relevancy, context_precision, faithfulness

    try:
        from agent.model.factory import get_chat_model
    except ModuleNotFoundError:
        from model.factory import get_chat_model

    dataset = EvaluationDataset.from_list(eval_rows)
    results = evaluate(
        dataset,
        metrics=[faithfulness, answer_relevancy, context_precision],
        llm=LangchainLLMWrapper(get_chat_model()),
        show_progress=True,
    )

    scores = {m: float(results[m]) for m in ("faithfulness", "answer_relevancy", "context_precision")}
    print("\nragas 评估结果:")
    for k, v in scores.items():
        print(f"  {k}: {v:.2%}")

    assert scores["faithfulness"] >= 0.80, f"faithfulness {scores['faithfulness']:.2%} < 80%"
    assert scores["answer_relevancy"] >= 0.85, f"answer_relevancy {scores['answer_relevancy']:.2%} < 85%"
    assert scores["context_precision"] >= 0.75, f"context_precision {scores['context_precision']:.2%} < 75%"


if __name__ == "__main__":
    pytest.main([__file__, "-m", "rag_eval", "-v", "-s"])
