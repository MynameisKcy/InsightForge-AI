"""RAG 端到端质量评估（ragas：faithfulness / answer_relevancy / context_precision）。

流程：真实检索（retriever_docs）得 contexts → 经 rag_service.format_context_block
格式化为与生产 rag_summarize 完全相同的上下文块（评审 R2 候选8：评估锁步生产
prompt）→ 真实生成链（svc.chain）得 answer → ragas evaluate（judge LLM 复用
项目模型工厂）。依赖 requirements-eval.txt，未安装 ragas 时跳过
（快速回归请用 test_rag_retrieval_hit.py）。

运行：
    pip install -r requirements-eval.txt
    python -m pytest tests/rag_eval/test_rag_quality.py -m rag_eval -v -s

通过标准（与 docs/specs 方案 §5.3 一致）：
    faithfulness >= 0.80  answer_relevancy >= 0.85  context_precision >= 0.75
"""
import pytest

pytestmark = pytest.mark.rag_eval

ragas = pytest.importorskip("ragas", reason="未安装评估依赖：pip install -r requirements-eval.txt")

from model.factory import get_chat_model
from rag.rag_service import format_context_block

from tests.rag_eval.test_cases import TEST_CASES


@pytest.fixture(scope="session")
def eval_rows(rag_service, retrieved_contexts):
    """构造 ragas 数据集行：question / answer（真实生成）/ contexts（真实检索）/ ground_truth。"""
    rows = []
    for case in TEST_CASES:
        docs = retrieved_contexts[case["question"]]
        # 与生产 rag_summarize 同一格式化函数——生产 prompt 一变，评估自动跟随
        context_text = format_context_block(docs)
        # 与 rag_summarize 内部一致的生成方式（chain.invoke），避免二次检索
        answer = str(rag_service.chain.invoke({"input": case["question"], "context": context_text}))
        rows.append({
            "question": case["question"],
            "answer": answer,
            "contexts": [d.page_content for d in docs],
            "ground_truth": case["ground_truth"],
        })
    return rows


def test_rag_quality(eval_rows):
    from ragas import EvaluationDataset, evaluate
    from ragas.llms import LangchainLLMWrapper
    from ragas.metrics import answer_relevancy, context_precision, faithfulness

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
