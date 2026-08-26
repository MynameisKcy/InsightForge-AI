"""Schema — DuckDB 表语义画像与 schema 文本生成。

纯函数，接收 DuckDB 连接作参（不 import duckdb）。
- ``detect_wide_table`` 检测宽表（≥5 个 4 位年份列名）
- ``compute_table_profile`` 计算单列语义统计（nunique / 取值 / 数值 min-max）+ 宽表标记
- ``get_schema_text`` 基础 schema 文本（折叠自原 ``schema_loader.py`` 壳）

深度（语义画像计算）集中在此模块，可脱离 DuckDBManager 独立测试。
"""

import re

from database.safety import safe_ident, validate_table_name


def detect_wide_table(col_names: list[str]) -> tuple[bool, str | None]:
    """检测是否为宽表(≥5 个 4 位年份列名)。返回 (is_wide, 'YYYY-YYYY'|None)。"""
    year_cols = []
    for c in col_names:
        s = str(c).strip()
        if re.fullmatch(r"(19|20)\d{2}", s):
            year_cols.append(int(s))
    if len(year_cols) >= 5:
        year_cols.sort()
        return True, f"{year_cols[0]}-{year_cols[-1]}"
    return False, None


def compute_table_profile(conn, table_name: str) -> dict:
    """计算单表语义画像：每列 nunique/取值/数值统计 + 宽表标记。供 schema 文本与缓存使用。"""
    validate_table_name(table_name)
    qname = safe_ident(table_name)
    cols = conn.execute(f"DESCRIBE {qname}").fetchall()
    col_names = [c[0] for c in cols]
    total = conn.execute(f"SELECT COUNT(*) FROM {qname}").fetchone()[0]
    is_wide, wide_range = detect_wide_table(col_names)

    col_profiles = []
    for col_name, col_type, *_ in cols:
        qcol = safe_ident(col_name)
        is_numeric = col_type.upper() in (
            "DOUBLE", "FLOAT", "INTEGER", "BIGINT", "SMALLINT", "TINYINT",
            "DECIMAL", "REAL", "HUGEINT", "UBIGINT", "UINTEGER", "USMALLINT",
            "UTINYINT", "UHUGEINT", "INT128",
        )
        # nunique + 非空数
        agg = conn.execute(
            f'SELECT COUNT(DISTINCT {qcol}), COUNT({qcol}) FROM {qname}'
        ).fetchone()
        nunique, non_null = int(agg[0]), int(agg[1])
        entry = {"name": col_name, "dtype": col_type, "nunique": nunique,
                 "non_null": non_null, "total": total}
        if is_numeric:
            if non_null > 0:
                mm = conn.execute(
                    f'SELECT MIN({qcol}), MAX({qcol}) FROM {qname} WHERE {qcol} IS NOT NULL'
                ).fetchone()
                entry["min"] = float(mm[0]) if mm[0] is not None else None
                entry["max"] = float(mm[1]) if mm[1] is not None else None
        else:
            # 低基数分类列:列取值(最多 8 个)
            if 0 < nunique <= 15:
                vals = conn.execute(
                    f'SELECT DISTINCT {qcol} FROM {qname} WHERE {qcol} IS NOT NULL LIMIT 8'
                ).fetchall()
                entry["values"] = [str(v[0]) for v in vals]
        col_profiles.append(entry)
    return {"columns": col_profiles, "is_wide_table": is_wide,
            "wide_table_range": wide_range, "row_count": total}


def get_schema_text(conn) -> str:
    """基础 schema 文本（原 SchemaLoader.get_schema_text，折叠自 schema_loader.py）。"""
    tables = conn.execute("SHOW TABLES").fetchall()
    if not tables:
        return "No tables found."
    parts = []
    for (table_name,) in tables:
        validate_table_name(table_name)
        cols = conn.execute(f"DESCRIBE {safe_ident(table_name)}").fetchall()
        col_lines = [f"  - {col_name} ({col_type})" for col_name, col_type, *_ in cols]
        parts.append(f"Table: {table_name}\n" + "\n".join(col_lines))
    return "\n\n".join(parts)
