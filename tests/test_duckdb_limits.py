"""DuckDB 查询通道资源上限测试（config/agent.yml `duckdb` 节）。

覆盖：连接级 memory_limit/threads、结果行数上限（报错喂 _fix_sql）、
查询超时 watchdog 中断、沙箱白名单不受影响（SET/PRAGMA 依旧被拒）、
无配置时的防御式默认值。直构 DuckDBManager（不走全局实例缓存，
对齐 test_duckdb_multi_source 模式）。
"""
import time
import unittest
from unittest.mock import patch

import utils.config_handler
from database.duckdb_manager import DuckDBManager
from database.safety import SecurityError


class DuckDBLimitTests(unittest.TestCase):
    def setUp(self):
        self.db = DuckDBManager(user_id="test_limits")

    def tearDown(self):
        self.db.close()

    def _setting(self, key: str) -> str:
        # current_setting 经 SELECT 白名单合法，且不在函数黑名单内
        return str(self.db.query_df(
            f"SELECT current_setting('{key}') AS v")["v"][0])

    def _mem_bytes(self, db=None) -> float:
        """memory_limit 归一为字节（DuckDB 把 1GB 显示成 953.6 MiB）。"""
        db = db or self.db
        v = str(db.query_df(
            "SELECT current_setting('memory_limit') AS v")["v"][0]).upper()
        num, unit = v.split()
        mult = {"B": 1, "KIB": 1024, "MIB": 1024 ** 2, "GIB": 1024 ** 3,
                "KB": 1000, "MB": 1000 ** 2, "GB": 1000 ** 3}
        return float(num) * mult[unit]

    def test_memory_and_threads_limits_applied(self):
        # agent.yml: memory_limit=1GB（=1e9 字节，DuckDB 显示 953.6 MiB）
        self.assertAlmostEqual(self._mem_bytes(), 1e9, delta=1e9 * 0.05)
        self.assertEqual(int(self._setting("threads")), 2)

    def test_max_result_rows_raises_with_fix_sql_hint(self):
        # 管理通道建大表（直连 conn.execute，绕过白名单——测试建表路径）
        self.db.conn.execute(
            "CREATE TABLE big AS SELECT range AS i FROM range(20000)")
        with self.assertRaises(ValueError) as ctx:
            self.db.query_df("SELECT i FROM big")
        self.assertIn("LIMIT", str(ctx.exception))
        # 上限内的查询不受影响
        small = self.db.query_df("SELECT i FROM big LIMIT 100")
        self.assertEqual(len(small), 100)

    def test_query_timeout_interrupts_expensive_query(self):
        self.db._query_timeout = 0.5
        start = time.monotonic()
        with self.assertRaises(TimeoutError) as ctx:
            # 巨大 cross join：无中断需数十秒，watchdog 0.5s 内打断
            self.db.execute(
                "SELECT count(*) FROM range(5000000) a, range(1000) b")
        elapsed = time.monotonic() - start
        self.assertIn("超时", str(ctx.exception))
        self.assertLess(elapsed, 10)

    def test_timeout_disabled_when_zero(self):
        self.db._query_timeout = 0
        # 直通路径（无 watchdog）仍正常返回
        cnt = self.db.query_df("SELECT 42 AS v")["v"][0]
        self.assertEqual(cnt, 42)

    def test_sandbox_still_blocks_set_and_pragma(self):
        # 资源上限走连接配置/Python 侧，沙箱白名单零放宽
        with self.assertRaises(SecurityError):
            self.db.execute("SET threads=4")
        with self.assertRaises(SecurityError):
            self.db.execute("PRAGMA memory_limit='2GB'")

    def test_defaults_when_config_missing(self):
        with patch.object(utils.config_handler, "agent_conf", {}):
            db = DuckDBManager(user_id="test_limits_defaults")
        try:
            self.assertEqual(db._max_result_rows, 10000)
            self.assertEqual(db._query_timeout, 30.0)
            self.assertAlmostEqual(self._mem_bytes(db), 1e9, delta=1e9 * 0.05)
            self.assertEqual(int(str(db.query_df(
                "SELECT current_setting('threads') AS v")["v"][0])), 2)
        finally:
            db.close()


if __name__ == "__main__":
    unittest.main()
