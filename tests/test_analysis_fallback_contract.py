import unittest
from unittest.mock import patch

from agents.analysis_agent import AnalysisAgent
from analysis.analysis_module import (
    ProductAnalysisAdapter,
    RiskAnalysisAdapter,
    TrendAnalysisAdapter,
)


class AdapterFallbackContractTests(unittest.TestCase):
    def test_trend_adapter_seeds_insight_from_trend_summary(self):
        r = {"trend_summary": "持续上升"}
        TrendAnalysisAdapter().apply_insight_fallback(r)
        self.assertEqual(r["insight"], "持续上升")
        self.assertEqual(r["key_findings"], [])
        self.assertEqual(r["recommendation"], "")
        self.assertNotIn("top_item_analysis", r)   # 不含产品键
        self.assertNotIn("risk_assessment", r)      # 不含风险键

    def test_product_adapter_own_keys_only(self):
        r = {}
        ProductAnalysisAdapter().apply_insight_fallback(r)
        self.assertEqual(r, {"insight": "", "top_item_analysis": "",
                             "low_performer_analysis": "", "recommendations": []})

    def test_risk_adapter_own_keys_only(self):
        r = {}
        RiskAnalysisAdapter().apply_insight_fallback(r)
        self.assertEqual(r, {"risk_assessment": "", "key_risks": [], "mitigation": []})

    def test_agent_delegates_fallback_to_adapter(self):
        class _FakeAdapter:
            insight_prompt = "{data_json}"
            def analyze(self, df): return {"ok": True}
            def apply_insight_fallback(self, result): result["fallback_applied"] = True

        ag = AnalysisAgent(_FakeAdapter(), model=object())
        with patch.object(AnalysisAgent, "_call_llm", side_effect=RuntimeError):
            result = ag.run({"dataframe_json": '[{"a": 1}]'})
        self.assertTrue(result.get("fallback_applied"))


if __name__ == "__main__":
    unittest.main()
