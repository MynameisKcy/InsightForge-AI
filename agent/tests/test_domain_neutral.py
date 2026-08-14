"""Tests for domain-neutral pipeline generalization.

Verifies the analysis pipeline no longer locks to sales/product/profit:
- ProductAnalysis (分组对比分析) auto-detects dimension×measure on non-sales
  data (population/traffic) instead of forcing qty×price.
- AnomalyDetection uses a generic measure column and emits measure_anomalies.
- PlannerAgent._default_plan routes domain-neutral queries (人口分布/流量对比)
  to the 分组对比 (product_analysis) stage.

Pure-logic tests; PlannerAgent is constructed via __new__ to avoid __init__.
"""
import os
import sys
import unittest

import pandas as pd

CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(CURRENT_DIR)
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from agents.planner_agent import PlannerAgent
from analysis.product_analysis import ProductAnalysis
from analysis.anomaly_detection import AnomalyDetection


def _bare_planner():
    return PlannerAgent.__new__(PlannerAgent)


class DefaultPlanDomainNeutralTests(unittest.TestCase):
    def test_population_query_routes_to_breakdown(self):
        plan = _bare_planner()._default_plan("分析各区人口分布")
        self.assertIn("product_analysis", [s["agent"] for s in plan])

    def test_traffic_query_routes_to_breakdown(self):
        plan = _bare_planner()._default_plan("各路口流量对比")
        self.assertIn("product_analysis", [s["agent"] for s in plan])


class ProductAnalysisDomainNeutralTests(unittest.TestCase):
    def test_population_data_uses_generic_measure(self):
        df = pd.DataFrame({
            "region": ["华东", "华北", "华南", "华东", "华北"],
            "population": [100, 80, 120, 100, 80],
        })
        summary = ProductAnalysis.build_product_summary(df, top_n=3)
        # 走通用路径，不是销售快路径
        self.assertFalse(summary["is_sales"])
        self.assertEqual(summary["dimension_col"], "region")
        self.assertEqual(summary["measure_col"], "total_value")
        self.assertTrue(summary["top_products"])
        top = summary["top_products"][0]
        self.assertIn("region", top)
        self.assertIn("total_value", top)
        # 不应出现销售快路径的 qty×price 产物
        self.assertNotIn("total_revenue", top)

    def test_sales_data_keeps_revenue_fastpath(self):
        df = pd.DataFrame({
            "Product_Description": ["A", "B", "A"],
            "Avg_Price": [10, 20, 10],
            "Quantity": [5, 2, 3],
        })
        summary = ProductAnalysis.build_product_summary(df, top_n=2)
        self.assertTrue(summary["is_sales"])
        self.assertEqual(summary["measure_col"], "total_revenue")
        self.assertTrue(summary["top_products"])
        self.assertIn("total_revenue", summary["top_products"][0])

    def test_metadata_labels_present(self):
        df = pd.DataFrame({"region": ["a", "b"], "population": [1, 2]})
        summary = ProductAnalysis.build_product_summary(df)
        for k in ("dimension_col", "category_col", "measure_col",
                  "dimension_label", "category_label", "measure_label"):
            self.assertIn(k, summary)


class AnomalyDetectionDomainNeutralTests(unittest.TestCase):
    def test_non_sales_data_returns_measure_anomalies(self):
        df = pd.DataFrame({
            "Month": [1, 2, 3, 4, 5, 6],
            "traffic": [100, 105, 98, 400, 110, 102],  # 400 is an outlier
        })
        summary = AnomalyDetection.build_risk_summary(df)
        # 键已泛化为 measure_anomalies（不再叫 revenue_anomalies）
        self.assertIn("measure_anomalies", summary)
        self.assertNotIn("revenue_anomalies", summary)
        self.assertIn("risk_level", summary)

    def test_non_sales_data_no_crash_without_time_col(self):
        df = pd.DataFrame({
            "city": ["A", "B", "C", "D"],
            "count": [10, 20, 30, 40],
        })
        summary = AnomalyDetection.build_risk_summary(df)
        self.assertIn("measure_anomalies", summary)


if __name__ == "__main__":
    unittest.main()
