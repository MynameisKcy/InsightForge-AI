
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


from agents.trend_agent import TrendAgent


def _df_to_json(records):
    import json
    return json.dumps(records, ensure_ascii=False)


def test_trend_agent_text_only_df_no_crash():
    """纯文本列 df 喂 TrendAgent.run,不抛 str/str,返回入口层 error 提示。

    断言 entry-layer 的 error 键存在(而非 OR 底层非数值回退):
    修复后 TrendAgent.run 对全文本列直接返回 {"error": ...};
    若回退到 df.columns[-1] 文本列,只会走到 Task 1 底层 coerce 返回
    非 error 的"非数值"trend_summary,本断言即失败。
    """
    df_json = _df_to_json([
        {"Country Name": "A", "Indicator Name": "foo"},
        {"Country Name": "B", "Indicator Name": "bar"},
    ])
    result = TrendAgent().run({"dataframe_json": df_json})
    assert "error" in result  # 入口层显式拦截,非底层非数值回退
    assert "str" not in str(result)  # 无原始 TypeError 痕迹


def test_trend_agent_picks_numeric_column():
    """混合列(数值在前+文本在后)且未指定 value_col,应选数值列而非最后一列文本。

    数值列放第一列:回退代码取 df.columns[-1]="Country Name"(文本)会走
    底层非数值路径,trend_summary 含"非数值";修复后选第一列数值列 val,
    trend_summary 为真实趋势不含"非数值"。
    """
    df_json = _df_to_json([
        {"val": 100.0, "Country Name": "A"},
        {"val": 120.0, "Country Name": "B"},
        {"val": 90.0, "Country Name": "C"},
    ])
    result = TrendAgent().run({"dataframe_json": df_json})
    # 不应崩溃,且应产生趋势摘要(选了 val 数值列)
    assert "trend_summary" in result
    assert "error" not in result or result.get("error") is None
    # 修复选数值列 -> 真实趋势;回退选末列文本 -> 底层"非数值"提示
    assert "非数值" not in result.get("trend_summary", "")
