"""DuckDB 实例池 LRU 上限测试（duckdb-instance-pool spec，config/agent.yml
`duckdb.instance_pool_cap`）。

覆盖：命中复用（不重复建/重灌）、超上限驱逐最久未访问用户（close 被调）、
访问刷新新近度、被驱逐用户再访问透明重建（数据集重灌再次触发）、
显式 close 后再访问重建、配置读取与缺失回退默认值。

用桩 DuckDBManager 隔离真实 :memory: 连接与数据集重灌副作用
（对齐 test_datasets_api.py 的 duck_mod 桩模式）。
"""
import unittest
from unittest.mock import patch

import database.duckdb_manager as ddm
import utils.config_handler


class FakeManager:
    """桩实例：记录构造与 close，避免真实连接/数据集重灌副作用。"""

    def __init__(self, csv_path=None, user_id="default"):
        self.csv_path = csv_path
        self.user_id = user_id
        self.last_loaded_csv = csv_path
        self.closed = False

    def close(self):
        self.closed = True

    def reload_csv(self, csv_path):
        self.last_loaded_csv = csv_path

    def register_external_databases(self):
        pass


class DuckDBInstancePoolLRUTests(unittest.TestCase):
    def setUp(self):
        # 保存并清空全局池，避免与其他用例/模块级状态串扰
        self._saved = list(ddm._duckdb_instances.items())
        ddm._duckdb_instances.clear()
        self.reload_calls: list[str] = []

        def spy_reload(inst):
            self.reload_calls.append(inst.user_id)

        self.patchers = [
            patch.object(ddm, "DuckDBManager", FakeManager),
            patch.object(ddm, "_reload_datasets_into_instance", spy_reload),
        ]
        for p in self.patchers:
            p.start()

    def tearDown(self):
        for p in self.patchers:
            p.stop()
        ddm._duckdb_instances.clear()
        ddm._duckdb_instances.update(self._saved)

    def _cap(self, n: int):
        return patch.object(ddm, "_instance_pool_cap", lambda: n)

    def test_same_user_reuses_instance_without_rebuild(self):
        with self._cap(5):
            a = ddm.init_duckdb(user_id="u1")
            b = ddm.init_duckdb(user_id="u1")
        self.assertIs(a, b)
        self.assertEqual(len(ddm._duckdb_instances), 1)
        # 新建触发一次数据集重灌，复用不重灌
        self.assertEqual(self.reload_calls, ["u1"])

    def test_evicts_lru_when_over_cap(self):
        with self._cap(2):
            u1 = ddm.init_duckdb(user_id="u1")
            u2 = ddm.init_duckdb(user_id="u2")
            u3 = ddm.init_duckdb(user_id="u3")  # 池满：驱逐最久未用的 u1
        self.assertTrue(u1.closed)
        self.assertFalse(u2.closed)
        self.assertFalse(u3.closed)
        self.assertEqual(set(ddm._duckdb_instances.keys()), {"u2", "u3"})

    def test_access_refreshes_recency(self):
        with self._cap(2):
            u1 = ddm.init_duckdb(user_id="u1")
            u2 = ddm.init_duckdb(user_id="u2")
            ddm.init_duckdb(user_id="u1")          # u1 刷新新近度
            u3 = ddm.init_duckdb(user_id="u3")     # 驱逐的应是 u2
        self.assertFalse(u1.closed)
        self.assertTrue(u2.closed)
        self.assertFalse(u3.closed)

    def test_evicted_user_rebuilds_transparently(self):
        with self._cap(1):
            ddm.init_duckdb(user_id="u1")
            ddm.init_duckdb(user_id="u2")          # u1 被驱逐
            self.assertEqual(self.reload_calls, ["u1", "u2"])
            again = ddm.init_duckdb(user_id="u1")  # 透明重建：再触发重灌
            self.assertEqual(self.reload_calls.count("u1"), 2)
            self.assertEqual(set(ddm._duckdb_instances.keys()), {"u1"})
            self.assertIsNotNone(again)

    def test_explicit_close_then_rebuild(self):
        with self._cap(5):
            inst = ddm.init_duckdb(user_id="u1")
            ddm.close_duckdb("u1")
            self.assertTrue(inst.closed)
            self.assertNotIn("u1", ddm._duckdb_instances)
            new = ddm.init_duckdb(user_id="u1")
            self.assertIsNot(inst, new)

    def test_cap_read_from_config(self):
        with patch.object(utils.config_handler, "agent_conf",
                          {"duckdb": {"instance_pool_cap": 3}}):
            self.assertEqual(ddm._instance_pool_cap(), 3)

    def test_default_cap_when_config_missing(self):
        with patch.object(utils.config_handler, "agent_conf", {}):
            self.assertEqual(ddm._instance_pool_cap(), 50)
        # 非法配置同样回退默认，不抛异常
        with patch.object(utils.config_handler, "agent_conf",
                          {"duckdb": {"instance_pool_cap": "not-a-number"}}):
            self.assertEqual(ddm._instance_pool_cap(), 50)


if __name__ == "__main__":
    unittest.main()
