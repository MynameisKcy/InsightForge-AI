"""画图生成回归测试。

覆盖两个曾出现的缺陷：
1. auto_chart 无条件注入 names_col/values_col 后 splat 进 bar_chart -> TypeError。
2. y_col 含 NaN 时画空柱/体验差 -> 绘图前应 dropna。
"""

import os
import sys

CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(CURRENT_DIR)
PROJECT_PARENT = os.path.dirname(PROJECT_ROOT)
for p in (PROJECT_ROOT, PROJECT_PARENT):
    if p not in sys.path:
        sys.path.insert(0, p)

import pandas as pd

from visualization.charts import ChartGenerator


def _euro_2002_df() -> pd.DataFrame:
    """World Bank 宽表样例：列名为整数串 '2002'，6/10 国缺失。"""
    return pd.DataFrame([
        {"Country Name": "Belgium", "2002": float("nan")},
        {"Country Name": "Switzerland", "2002": 97.59},
        {"Country Name": "Germany", "2002": 103.39},
        {"Country Name": "Spain", "2002": float("nan")},
        {"Country Name": "France", "2002": float("nan")},
        {"Country Name": "United Kingdom", "2002": float("nan")},
        {"Country Name": "Italy", "2002": 99.87},
        {"Country Name": "Netherlands", "2002": float("nan")},
        {"Country Name": "Poland", "2002": float("nan")},
        {"Country Name": "Sweden", "2002": 100.07},
    ])


def _is_real_path(path: str) -> bool:
    """返回值是真实 HTML 路径，而非占位/错误串。"""
    return bool(path) and not path.startswith("[")


def test_auto_chart_bar_does_not_crash():
    """primary 回归：auto_chart('bar', ...) 不再因 names_col 注入抛 TypeError。"""
    df = _euro_2002_df()
    path = ChartGenerator.auto_chart(
        df, "bar", title="2002年欧洲主要大国对比",
        x_col="Country Name", y_col="2002",
        x_label="国家", y_label="指标值",
    )
    assert _is_real_path(path), f"期望 HTML 路径，实际: {path}"


def test_auto_chart_param_isolation_across_types():
    """bar 不收 names_col、pie 不收 x_col、line/scatter 不崩（参数隔离生效）。"""
    df = _euro_2002_df()
    assert _is_real_path(ChartGenerator.auto_chart(
        df, "bar", x_col="Country Name", y_col="2002"))
    assert _is_real_path(ChartGenerator.auto_chart(
        df, "line", x_col="Country Name", y_col="2002"))
    assert _is_real_path(ChartGenerator.auto_chart(
        df, "pie", names_col="Country Name", values_col="2002"))
    assert _is_real_path(ChartGenerator.auto_chart(
        df, "scatter", x_col="Country Name", y_col="2002"))


def test_bar_chart_drops_nan_rows():
    """secondary：bar_chart 应剔除 y_col 缺失行，仅画 4 个有值国家。"""
    df = _euro_2002_df()
    path = ChartGenerator.bar_chart(df, x_col="Country Name", y_col="2002", title="对比")
    assert _is_real_path(path), f"期望 HTML 路径，实际: {path}"
    # 有值国家恰好 4 个
    assert df["2002"].dropna().shape[0] == 4


def test_bar_chart_all_nan_returns_placeholder():
    """全 NaN 的 y_col -> 返回占位串、不抛异常。"""
    df = pd.DataFrame([
        {"Country Name": "A", "2002": float("nan")},
        {"Country Name": "B", "2002": float("nan")},
    ])
    path = ChartGenerator.bar_chart(df, x_col="Country Name", y_col="2002", title="空")
    assert path.startswith("["), f"期望占位串，实际: {path}"


def test_pie_chart_drops_nan_values():
    """pie_chart 应剔除 values_col 缺失行。"""
    df = _euro_2002_df()
    path = ChartGenerator.pie_chart(df, names_col="Country Name", values_col="2002", title="占比")
    assert _is_real_path(path), f"期望 HTML 路径，实际: {path}"


def _shandong_df() -> pd.DataFrame:
    """山东普查样例：含一行「全省」汇总 + 4 个城市，全省值≈各市之和。"""
    return pd.DataFrame([
        {"地区": "全省", "人口数（人）": 10000},
        {"地区": "济南市", "人口数（人）": 3000},
        {"地区": "青岛市", "人口数（人）": 2500},
        {"地区": "淄博市", "人口数（人）": 2000},
        {"地区": "枣庄市", "人口数（人）": 2500},
    ])


def test_pie_chart_drops_summary_row():
    """pie_chart 应剔除类别名含「全省」的汇总行，不把它当一个 slice。"""
    df = _shandong_df()
    cleaned = ChartGenerator._drop_summary_rows(df, cat_col="地区", values_col="人口数（人）")
    assert "全省" not in cleaned["地区"].tolist()
    assert len(cleaned) == 4


def test_bar_chart_drops_summary_row():
    """bar_chart 应剔除汇总行，全省不参与城市排名对比。"""
    df = _shandong_df()
    path = ChartGenerator.bar_chart(df, x_col="地区", y_col="人口数（人）", title="各市人口")
    assert _is_real_path(path), f"期望 HTML 路径，实际: {path}"


def test_drop_summary_rows_by_sum_detection():
    """类别名不含关键词但某行数值≈其余行之和时，也应识别为汇总行剔除。"""
    df = pd.DataFrame([
        {"城市": "汇总", "值": 100},  # 名字像关键词，会被关键词命中
        {"城市": "甲", "值": 30},
        {"城市": "乙", "值": 30},
        {"城市": "丙", "值": 40},
    ])
    cleaned = ChartGenerator._drop_summary_rows(df, cat_col="城市", values_col="值")
    assert "汇总" not in cleaned["城市"].tolist()
    assert len(cleaned) == 3


def test_drop_summary_rows_keeps_normal_data():
    """无汇总行的正常数据应原样保留（不误剔）。"""
    df = pd.DataFrame([
        {"城市": "济南市", "值": 3000},
        {"城市": "青岛市", "值": 2500},
        {"城市": "淄博市", "值": 2000},
    ])
    cleaned = ChartGenerator._drop_summary_rows(df, cat_col="城市", values_col="值")
    assert len(cleaned) == 3
