"""SQL Safety — 查询通道的只读安全接缝。

集中 DuckDB 查询通道的所有安全校验：
- 标识符安全：``safe_ident`` 转义、``validate_table_name`` 表名校验
- 路径安全：``validate_csv_path`` 路径穿越 / read_csv_auto 注入防护
- 只读沙箱：``assert_read_only`` 用 sqlglot AST 白名单拦截写 / DDL / 文件 / 网络语句

这些函数与 DuckDB 连接状态零耦合（``assert_read_only`` 只对 SQL 字符串做 AST 校验），
故独立成模块。管理通道（``_load_csv`` / ``load_csv_dataset`` 等建表路径）不经此校验，
直接调 ``conn.execute`` —— 「管理通道 vs 查询通道」边界由调用方选择的方法决定。
"""

import os
import re

import sqlglot
from sqlglot import exp


class SecurityError(Exception):
    """SQL 语句未通过只读沙箱白名单校验时抛出。"""


# --------------------------------------------------------------------------- #
# 标识符安全
# --------------------------------------------------------------------------- #

# 合法表名：字母/下划线开头，仅含字母数字下划线。
_TABLE_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def safe_ident(name: str) -> str:
    """转义 DuckDB 标识符，防止 SQL 注入。"""
    return '"' + name.replace('"', '""') + '"'


def validate_table_name(name: str) -> str:
    """校验表名合法（防 SQL 注入）：仅允许标识符字符。"""
    if not name or not _TABLE_NAME_RE.match(name):
        raise SecurityError(f"非法表名: {name!r}（仅允许字母/下划线开头、字母数字下划线）")
    return name


# --------------------------------------------------------------------------- #
# 路径安全
# --------------------------------------------------------------------------- #

def validate_csv_path(path: str) -> str:
    """校验数据文件路径安全：必须在 data 目录下且不含单引号（防 read_csv_auto 注入与路径穿越）。"""
    if not path:
        raise SecurityError("空数据文件路径")
    if "'" in path:
        raise SecurityError(f"数据文件路径含非法字符: {path!r}")
    # 路径穿越防护：realpath 必须在 data 目录下
    try:
        from utils.path_tool import get_abs_path
    except ModuleNotFoundError:
        from agent.utils.path_tool import get_abs_path
    allowed_root = os.path.realpath(get_abs_path("data"))
    real = os.path.realpath(path)
    if not real.startswith(allowed_root + os.sep) and real != allowed_root:
        raise SecurityError(f"数据文件路径越界: {path!r}")
    return path


# --------------------------------------------------------------------------- #
# 只读沙箱（SQL AST 校验）
# --------------------------------------------------------------------------- #

# 查询通道允许的 SQL 语句类型（AST 节点 key）。仅允许只读 SELECT 派生类型，
# 以及只读 schema 探查语句（SHOW/DESCRIBE/SUMMARIZE）。
# 注意：EXPLAIN/LOAD/CALL/VACUUM 在 DuckDB 方言下会回退为 'command' 类型，
# 无法可靠校验内部，故 'command' 不在白名单——这些语句会被拒绝。
# 管理通道（_load_csv/reload_csv）不经此校验。
_READ_ONLY_STMT_TYPES = {
    "select", "union", "intersect", "except", "subquery",
    "show", "describe", "summarize",
}
# 显式拒绝的语句类型 key（DDL/DML/副作用类），双保险：即便上层放行也会拦下。
_FORBIDDEN_STMT_TYPES = {
    "create", "insert", "update", "delete", "drop", "alter", "truncate",
    "copy", "attach", "detach", "call", "set", "pragma", "vacuum",
    "merge", "replace", "install", "load",
}
# 禁用的函数名（规范化：去下划线大写）：任意文件读 / 网络访问 / 文件写入。
# 这些函数即使在 SELECT 内也会泄露文件内容或触发 SSRF/写盘，故全量禁止。
# 同时覆盖 sqlglot 的两种解析形态：Anonymous（read_csv_auto）与内置类（ReadCSV）。
_FORBIDDEN_FUNCTIONS = {
    # 文件读取
    "READCSVAUTO", "READCSV", "READJSON", "READJSONAUTO",
    "READPARQUET", "READBLOB", "READTEXT", "READTEXTAUTO",
    "READFWF", "READFWFAUTO",
    # 网络/远程
    "HTTPFS", "GLOB", "GLOBRECURSIVE",
    # DuckDB 扩展加载（不应在查询通道出现）
    "INSTALL", "LOAD",
    # 写文件（COPY/EXPORT 已在 stmt 层拦截，这里再防函数形态）
    "EXPORTDATABASE", "EXPORTPARQUET", "EXPORTCSV",
    # 执行外部
    "SYSTEM", "SHELL",
}


def _normalize_func_name(name: str) -> str:
    """规范化函数名：大写并去掉下划线，使 read_csv_auto / READCSV / ReadParquet
    等不同 sqlglot 解析形态（Anonymous vs 内置类）归一到同一可比较串。"""
    return (name or "").upper().replace("_", "")


def _collect_func_names(stmt: exp.Expression) -> set[str]:
    """收集 SQL AST 中出现的所有函数名（含匿名函数如 read_csv_auto），

    返回规范化后的集合（去掉下划线的大写名），如 {'READCSVAUTO', 'COUNT', 'SUM'}。
    sqlglot 对 read_csv_auto/read_json 解析为 exp.Anonymous（.name 带 _），
    对 read_csv/read_parquet 解析为内置类 ReadCSV/ReadParquet（类名无 _），
    故统一去下划线后比较。
    """
    names: set[str] = set()
    for f in stmt.find_all(exp.Func):
        if isinstance(f, exp.Anonymous):
            names.add(_normalize_func_name(f.name))
        else:
            names.add(_normalize_func_name(type(f).__name__))
    return names


def assert_read_only(sql: str) -> None:
    """查询通道 AST 校验：仅允许只读 SELECT 派生语句，拦截写/DDL/文件/网络函数与多语句。

    使用 sqlglot 将 SQL 解析为 DuckDB 方言 AST，而非字符串关键词扫描，
    杜绝注释/换行/字符串拼接/函数构造等绕过手段。管理通道（_load_csv 的
    CREATE TABLE、reload_csv 的 DROP TABLE）通过 self.conn.execute 直调，
    不经此方法，保持「管理通道 vs 查询通道」边界。
    """
    if not sql or not sql.strip():
        raise SecurityError("空 SQL 语句")

    try:
        stmts = sqlglot.parse(sql, read="duckdb")
    except Exception as e:  # ParseError 等
        raise SecurityError(f"SQL 解析失败，拒绝执行: {type(e).__name__}: {e}")

    # 过滤掉纯空语句（仅注释等），保留真实语句
    real_stmts = [s for s in stmts if s is not None]
    if not real_stmts:
        raise SecurityError("SQL 无有效语句")
    if len(real_stmts) > 1:
        # 多语句（如 `SELECT 1; DROP TABLE x`）会解析为多条 AST，一律拒绝，防分号注入
        raise SecurityError(
            f"只读沙箱禁止多语句执行（检测到 {len(real_stmts)} 条语句）"
        )

    stmt = real_stmts[0]
    stmt_key = stmt.key.lower()

    # 1) 语句类型白名单
    if stmt_key in _FORBIDDEN_STMT_TYPES:
        raise SecurityError(f"只读沙箱禁止执行 '{stmt_key.upper()}' 语句")
    if stmt_key not in _READ_ONLY_STMT_TYPES:
        raise SecurityError(
            f"只读沙箱禁止以 '{stmt_key.upper()}' 开头的语句（仅允许只读 SELECT 派生）"
        )

    # 2) 函数级黑名单：即便语句是 SELECT，也禁止任何文件/网络/写盘函数
    func_names = _collect_func_names(stmt)
    bad_funcs = func_names & _FORBIDDEN_FUNCTIONS
    if bad_funcs:
        raise SecurityError(
            f"只读沙箱禁止调用文件/网络/写盘函数: {sorted(bad_funcs)}"
        )
