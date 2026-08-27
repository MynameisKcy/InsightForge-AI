"""确定性召回指标（recall@k / MRR）——方案一验收尺，也是子项目2切分策略 A/B 的度量基础。

口径（spec §5）：chunk 含任一 keyword 子串即视为相关；recall@k 作用于融合后、
rerank 前的候选池；MRR 分母恒为用例数、未命中 RR=0；keyword 含数字字符的用例
归入数字型子集单独报告（Hybrid 靶心）。
运行：python -m pytest tests/rag_eval/test_recall_metrics.py -m rag_eval -v -s
"""
import re

import pytest

pytestmark = pytest.mark.rag_eval

from tests.rag_eval.test_cases import TEST_CASES


def _is_relevant(page_content: str, keywords: list) -> bool:
    return any(kw in page_content for kw in keywords)


def _is_numeric_case(keywords: list) -> bool:
    return any(re.search(r"\d", kw) for kw in keywords)


def _metrics(pool: dict, cases: list, k: int) -> tuple[float, float]:
    hits = 0
    rr_sum = 0.0
    for case in cases:
        top_k = pool.get(case["question"], [])[:k]
        rank = next(
            (i for i, d in enumerate(top_k, start=1)
             if _is_relevant(d.page_content, case["keywords"])),
            None,
        )
        if rank is not None:
            hits += 1
            rr_sum += 1.0 / rank
    n = len(cases)
    return hits / n, rr_sum / n


def test_bm25_leg_alive(rag_service):
    """守卫：直灌语料绕过写回调时 BM25 会静默为空（spec §5.3）。"""
    if getattr(rag_service, "_bm25", None) is None:
        pytest.skip("hybrid 未启用（基线模式）")
    assert rag_service._bm25.search("云帆CRM", k=5, viewer_id="u_rag_eval")


def test_recall_and_mrr(coarse_contexts):
    numeric = [c for c in TEST_CASES if _is_numeric_case(c["keywords"])]
    print("\n=== 召回指标（数字回填 spec 附录B） ===")
    for label, cases in (("全部", TEST_CASES), ("数字型子集", numeric)):
        r15, _mrr15 = _metrics(coarse_contexts, cases, 15)
        r20, mrr20 = _metrics(coarse_contexts, cases, 20)
        print(f"[{label}] n={len(cases)} recall@15={r15:.0%} "
              f"recall@20={r20:.0%} MRR@20={mrr20:.3f}")
    r20_all, _ = _metrics(coarse_contexts, TEST_CASES, 20)
    empty = [q for q, p in coarse_contexts.items() if not p]
    assert not empty, f"候选池为空的用例: {empty}"
    assert r20_all >= 0.80, f"recall@20={r20_all:.0%} < 80%"
