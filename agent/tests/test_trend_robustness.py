import os
import sys

CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(CURRENT_DIR)
PROJECT_PARENT = os.path.dirname(PROJECT_ROOT)
for p in (PROJECT_ROOT, PROJECT_PARENT):
    if p not in sys.path:
        sys.path.insert(0, p)

import pandas as pd
from analysis.trend_analysis import TrendAnalysis


def test_build_trend_summary_coerces_non_numeric():
    """value_col 是文本列时,build_trend_summary 应返回提示而非 str/str 崩溃。"""
    df = pd.DataFrame({
        "Country Name": ["A", "B", "C"],
        "Indicator Name": ["foo", "bar", "baz"],
    })
    result = TrendAnalysis.build_trend_summary(
        df, value_col="Indicator Name", month_col="Country Name"
    )
    assert "非数值" in result["trend_summary"]
    assert result["growth_rate"] == "N/A"
    assert result["anomaly_months"] == []


def test_build_trend_summary_all_nan_numeric():
    """value_col 全 NaN(数值 dtype 但无有效值)也应返回提示。"""
    df = pd.DataFrame({
        "Month": [1, 2, 3],
        "val": [float("nan"), float("nan"), float("nan")],
    })
    result = TrendAnalysis.build_trend_summary(df, value_col="val", month_col="Month")
    assert "非数值" in result["trend_summary"] or "无有效" in result["trend_summary"]
    assert result["growth_rate"] == "N/A"


def test_build_trend_summary_normal_numeric_unchanged():
    """正常数值列不回归,趋势计算照常。"""
    df = pd.DataFrame({"Month": [1, 2, 3], "total_revenue": [100.0, 120.0, 90.0]})
    result = TrendAnalysis.build_trend_summary(
        df, value_col="total_revenue", month_col="Month"
    )
    assert "趋势" in result["trend_summary"]
    assert "overall_growth_pct" in result
