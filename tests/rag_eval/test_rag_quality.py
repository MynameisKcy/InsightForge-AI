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

from model.factory import get_chat_model, get_embed_model
from rag.rag_service import format_context_block

from tests.rag_eval.test_cases import TEST_CASES


@pytest.fixture(scope="session")
def eval_rows(rag_service, retrieved_contexts):
    """构造 ragas 数据集行（ragas 0.4 列名）：user_input / response（真实生成）/ retrieved_contexts（真实检索）/ reference。"""
    rows = []
    for case in TEST_CASES:
        docs = retrieved_contexts[case["question"]]
        # 与生产 rag_summarize 同一格式化函数——生产 prompt 一变，评估自动跟随
        context_text = format_context_block(docs)
        # 与 rag_summarize 内部一致的生成方式（chain.invoke），避免二次检索
        answer = str(rag_service.chain.invoke({"input": case["question"], "context": context_text}))
        rows.append({
            "user_input": case["question"],
            "response": answer,
            "retrieved_contexts": [d.page_content for d in docs],
            "reference": case["ground_truth"],
        })
    return rows


def test_rag_quality(eval_rows):
    # ragas 0.4 有两代 API：collections 系（新，只配 @experiment）与 legacy 系
    # （ragas.metrics 单例 + evaluate(llm=)）。deprecated 的 evaluate() 只收
    # legacy Metric 家族——collections 实例会被 "must be initialised metric
    # objects" 拒收，故走兼容层。judge 复用项目模型工厂（与生成链同源）。
    from ragas import EvaluationDataset, evaluate
    from ragas.embeddings import LangchainEmbeddingsWrapper
    from ragas.llms import LangchainLLMWrapper
    from ragas.metrics import answer_relevancy, context_precision, faithfulness

    model = get_chat_model()
    # kimi-k2.7-code 服务端强制 thinking：n>1 一律 400（enable_thinking/
    # chat_template_kwargs 均无法关闭，已实测）。answer_relevancy 默认
    # strictness=3 要 n=3——降为 1（单问题估计，方差升、期望不变）。
    answer_relevancy.strictness = 1
    # legacy metric 调 embed_query()（LangChain 接口）——须用 Langchain 包装；
    # ragas modern OpenAIEmbeddings 只有 embed_text，会被 AttributeError 杀掉
    answer_relevancy.embeddings = LangchainEmbeddingsWrapper(get_embed_model())
    dataset = EvaluationDataset.from_list(eval_rows)
    # 单 judge 调用 ~70s（kimi 长提示词）；4 并发实测触发连接被掐
    # （APIConnectionError 连环、有效样本 2/20）——2 并发 + 600s 超时稳定
    from ragas.run_config import RunConfig
    results = evaluate(
        dataset,
        metrics=[faithfulness, answer_relevancy, context_precision],
        llm=LangchainLLMWrapper(model),
        show_progress=True,
        run_config=RunConfig(timeout=600, max_retries=3, max_workers=2),
    )

    # ragas 0.4：results[m] 是逐样本 list（judge 超时样本为 NaN）——NaN 感知求均值
    import math

    def _nanmean(values: list) -> float:
        usable = [v for v in values
                  if v is not None and not (isinstance(v, float) and math.isnan(v))]
        if not usable:
            return float("nan")
        return sum(usable) / len(usable)

    scores = {}
    for m in ("faithfulness", "answer_relevancy", "context_precision"):
        per_sample = list(results[m])
        mean = _nanmean(per_sample)
        covered = sum(1 for v in per_sample
                      if v is not None and not (isinstance(v, float) and math.isnan(v)))
        print(f"  {m}: {mean:.2%}（有效样本 {covered}/{len(per_sample)}，其余 judge 超时记 NaN）")
        assert covered * 2 >= len(per_sample), \
            f"{m} 有效样本 {covered}/{len(per_sample)} 不足半数——judge 超时过多，结果无公信力"
        scores[m] = mean

    print("\nragas 评估结果:")

    assert scores["faithfulness"] >= 0.80, f"faithfulness {scores['faithfulness']:.2%} < 80%"
    assert scores["answer_relevancy"] >= 0.85, f"answer_relevancy {scores['answer_relevancy']:.2%} < 85%"
    assert scores["context_precision"] >= 0.75, f"context_precision {scores['context_precision']:.2%} < 75%"


if __name__ == "__main__":
    pytest.main([__file__, "-m", "rag_eval", "-v", "-s"])
