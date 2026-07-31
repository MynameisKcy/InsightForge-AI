import os
import sys

CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(CURRENT_DIR)
PROJECT_PARENT = os.path.dirname(PROJECT_ROOT)
for p in (PROJECT_ROOT, PROJECT_PARENT):
    if p not in sys.path:
        sys.path.insert(0, p)

from database.duckdb_manager import DuckDBManager, _detect_wide_table
import pandas as pd


def test_detect_wide_table_year_columns():
    """≥5 个 4 位年份列名 -> 检测为宽表,返回年份范围。"""
    cols = ["Country Name", "Country Code", "Indicator Name", "Indicator Code",
            "1960", "1961", "1962", "1963", "1964", "2002", "2025"]
    is_wide, rng = _detect_wide_table(cols)
    assert is_wide is True
    assert rng == "1960-2025"


def test_detect_wide_table_not_wide():
    """普通表(无足够年份列)不判为宽表。"""
    cols = ["Month", "Revenue", "2020"]
    is_wide, rng = _detect_wide_table(cols)
    assert is_wide is False
    assert rng is None


def _make_mgr_with_table(table_name, df):
    """辅助:建一个内存 DuckDBManager 并加载 df 为表。"""
    mgr = DuckDBManager(user_id="test_schema")
    mgr.conn.register("__load", df)
    mgr.conn.execute(f'CREATE TABLE "{table_name}" AS SELECT * FROM __load')
    mgr.conn.unregister("__load")
    return mgr


def test_compute_table_profile_low_cardinality_values():
    """低基数分类列(nunique≤15)的画像应含实际取值。"""
    df = pd.DataFrame({
        "Country Name": ["A", "B", "C"],
        "Indicator Name": ["Primary completion rate"] * 3,
        "2002": [97.5, 103.4, float("nan")],
    })
    mgr = _make_mgr_with_table("wb_test", df)
    profile = mgr._compute_table_profile("wb_test")
    col_by_name = {c["name"]: c for c in profile["columns"]}
    # Indicator Name 仅 1 唯一值,应列出取值
    assert col_by_name["Indicator Name"]["nunique"] == 1
    assert "Primary completion rate" in col_by_name["Indicator Name"]["values"]
    # 2002 数值列应有 min/max/non_null
    assert col_by_name["2002"]["min"] == 97.5
    assert col_by_name["2002"]["non_null"] == 2
    assert col_by_name["2002"]["total"] == 3


def test_compute_table_profile_high_cardinality_omits_values():
    """高基数分类列(nunique>15)只标 nunique,不列取值。"""
    df = pd.DataFrame({"id": [str(i) for i in range(20)], "val": [float(i) for i in range(20)]})
    mgr = _make_mgr_with_table("hc_test", df)
    profile = mgr._compute_table_profile("hc_test")
    col_by_name = {c["name"]: c for c in profile["columns"]}
    assert col_by_name["id"]["nunique"] == 20
    assert "values" not in col_by_name["id"] or col_by_name["id"].get("values") is None
