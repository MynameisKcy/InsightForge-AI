import os
import sys

CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(CURRENT_DIR)
PROJECT_PARENT = os.path.dirname(PROJECT_ROOT)
for p in (PROJECT_ROOT, PROJECT_PARENT):
    if p not in sys.path:
        sys.path.insert(0, p)

from database.duckdb_manager import DuckDBManager
from database.schema import detect_wide_table, compute_table_profile
import pandas as pd


def test_detect_wide_table_year_columns():
    """≥5 个 4 位年份列名 -> 检测为宽表,返回年份范围。"""
    cols = ["Country Name", "Country Code", "Indicator Name", "Indicator Code",
            "1960", "1961", "1962", "1963", "1964", "2002", "2025"]
    is_wide, rng = detect_wide_table(cols)
    assert is_wide is True
    assert rng == "1960-2025"


def test_detect_wide_table_not_wide():
    """普通表(无足够年份列)不判为宽表。"""
    cols = ["Month", "Revenue", "2020"]
    is_wide, rng = detect_wide_table(cols)
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
    profile = compute_table_profile(mgr.conn,"wb_test")
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
    profile = compute_table_profile(mgr.conn,"hc_test")
    col_by_name = {c["name"]: c for c in profile["columns"]}
    assert col_by_name["id"]["nunique"] == 20
    assert "values" not in col_by_name["id"] or col_by_name["id"].get("values") is None


def test_enhanced_schema_text_lists_indicator_values():
    """schema 文本应含 Indicator Name 的取值,让 LLM 看出指标内容。"""
    df = pd.DataFrame({
        "Country Name": ["Germany", "France", "Italy"],
        "Indicator Name": ["Primary completion rate, female"] * 3,
        "2002": [103.4, float("nan"), 99.9],
        "1960": [float("nan")] * 3, "1961": [float("nan")] * 3,
        "1962": [float("nan")] * 3, "1963": [float("nan")] * 3, "1964": [float("nan")] * 3,
    })
    mgr = _make_mgr_with_table("wb_schema", df)
    text = mgr.get_enhanced_schema_text()
    assert "Primary completion rate, female" in text
    assert "宽表" in text  # 宽表标记


def test_enhanced_schema_text_uses_cache(monkeypatch):
    """第二次调用应命中缓存,不重算(用调用计数验证)。"""
    df = pd.DataFrame({"Country Name": ["A", "B"], "val": [1.0, 2.0]})
    mgr = _make_mgr_with_table("cache_test", df)
    from database import schema
    call_count = {"n": 0}
    orig = schema.compute_table_profile
    def counting(conn, table_name):
        call_count["n"] += 1
        return orig(conn, table_name)
    monkeypatch.setattr(schema, "compute_table_profile", counting)
    mgr.get_enhanced_schema_text()
    mgr.get_enhanced_schema_text()
    assert call_count["n"] == 1  # 第二次走缓存


def test_enhanced_schema_text_cache_cleared_on_drop():
    """drop_table 后缓存清空,下次 schema 文本重新计算。"""
    df = pd.DataFrame({"Country Name": ["A", "B"], "val": [1.0, 2.0]})
    mgr = _make_mgr_with_table("drop_cache", df)
    mgr.get_enhanced_schema_text()
    assert "drop_cache" in mgr._profile_cache
    mgr.drop_table("drop_cache")
    assert "drop_cache" not in mgr._profile_cache


def test_load_csv_clears_profile_cache():
    """重新加载同名表应清旧画像缓存,避免 stale profile。"""
    import os
    import tempfile

    # validate_csv_path 要求文件在 data/ 目录下,用同一解析获取可写目录
    try:
        from utils.path_tool import get_abs_path
    except ModuleNotFoundError:
        from agent.utils.path_tool import get_abs_path
    data_dir = get_abs_path("data")
    os.makedirs(data_dir, exist_ok=True)

    mgr = DuckDBManager(user_id="test_schema")
    table = "reload_t"

    def _write_csv(content: str) -> str:
        # NamedTemporaryFile(delete=False) 在 data 目录下建文件,返回路径
        fd, path = tempfile.mkstemp(suffix=".csv", dir=data_dir)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(content)
        except Exception:
            os.unlink(path)
            raise
        return path

    csv1 = _write_csv("a,b\n1,3\n2,4\n")
    csv2 = _write_csv("x,y,z\np,r,5.0\nq,s,6.0\n")
    try:
        assert mgr.load_csv_dataset(csv1, table)["success"] is True
        mgr.get_enhanced_schema_text()  # populate cache
        assert table in mgr._profile_cache
        cached1 = mgr._profile_cache[table]
        assert {"a", "b"} == {c["name"] for c in cached1["columns"]}

        # 第二份 CSV:不同列结构(x/y/z),同名重载
        assert mgr.load_csv_dataset(csv2, table)["success"] is True
        # load_csv_dataset 必须清掉旧画像缓存,否则下次 schema 文本会返回 stale 列
        assert table not in mgr._profile_cache
        # 重新生成应反映新列结构
        mgr.get_enhanced_schema_text()
        cached2 = mgr._profile_cache[table]
        assert {"x", "y", "z"} == {c["name"] for c in cached2["columns"]}
    finally:
        for p in (csv1, csv2):
            if os.path.exists(p):
                os.unlink(p)


def test_compute_table_profile_handles_quote_in_column_name():
    """列名含双引号(脏 CSV 头)时,compute_table_profile 须用 safe_ident 转义,不注入/报错。"""
    df = pd.DataFrame({'we"ird': [1, 2], 'normal': ['a', 'b']})
    mgr = DuckDBManager(user_id="test_schema_qcol")
    mgr.conn.register("__load", df)
    mgr.conn.execute('CREATE TABLE "qcol_test" AS SELECT * FROM __load')
    mgr.conn.unregister("__load")
    profile = compute_table_profile(mgr.conn, "qcol_test")
    col = {c["name"]: c for c in profile["columns"]}
    assert 'we"ird' in col
    assert col['we"ird']["min"] == 1 and col['we"ird']["max"] == 2  # 数值列走 MIN/MAX 泄漏行


def test_reload_csv_clears_profile_cache():
    """reload_csv 重建同名表应清旧画像缓存,避免 stale profile。"""
    import os
    import tempfile

    # validate_csv_path 要求文件在 data/ 目录下
    try:
        from utils.path_tool import get_abs_path
    except ModuleNotFoundError:
        from agent.utils.path_tool import get_abs_path
    data_dir = get_abs_path("data")
    os.makedirs(data_dir, exist_ok=True)

    def _write_csv(content: str) -> str:
        fd, path = tempfile.mkstemp(suffix=".csv", dir=data_dir)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(content)
        except Exception:
            os.unlink(path)
            raise
        return path

    csv1 = _write_csv("a,b\n1,3\n2,4\n")
    csv2 = _write_csv("x,y\np,r\nq,s\n")
    try:
        # 用 csv_path 构造实例,触发 _load_csv 加载 csv1 为 transactions 表
        mgr = DuckDBManager(csv_path=csv1, user_id="test_reload_csv")
        assert mgr.table_name == "transactions"
        mgr.get_enhanced_schema_text()  # populate cache
        assert "transactions" in getattr(mgr, "_profile_cache", {})
        cached1 = mgr._profile_cache["transactions"]
        assert {"a", "b"} == {c["name"] for c in cached1["columns"]}

        # reload_csv 加载 csv2(不同列 x/y)到同名 transactions 表
        ok = mgr.reload_csv(csv2)
        assert ok is True
        # reload_csv 必须清掉旧画像缓存,否则下次 schema 文本会返回 stale 列(a/b)
        assert "transactions" not in getattr(mgr, "_profile_cache", {})
        # 重新生成应反映新列结构(x/y)
        mgr.get_enhanced_schema_text()
        cached2 = mgr._profile_cache["transactions"]
        assert {"x", "y"} == {c["name"] for c in cached2["columns"]}
    finally:
        for p in (csv1, csv2):
            if os.path.exists(p):
                os.unlink(p)


