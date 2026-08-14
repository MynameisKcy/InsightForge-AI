# tests/test_datasources_db.py
import os
import unittest
import tempfile


class TestDatasourcesDB(unittest.TestCase):
    def setUp(self):
        """每个测试用临时数据库文件。"""
        self.tmp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmp_dir, "test_datasources.db")
        from database.datasources_db import DatasourcesDB
        self.db = DatasourcesDB(db_path=self.db_path)

    def tearDown(self):
        if os.path.exists(self.db_path):
            os.remove(self.db_path)

    def test_add_and_get_dataset(self):
        result = self.db.add_dataset(
            name="sales_2024",
            source_type="csv",
            file_path="/tmp/sales_2024.csv",
            table_name="sales_2024",
            schema_json='[{"name":"id","type":"INTEGER"}]',
            row_count=100,
        )
        self.assertTrue(result["success"])
        self.assertIsNotNone(result["id"])

        ds = self.db.get_dataset("sales_2024")
        self.assertIsNotNone(ds)
        self.assertEqual(ds["source_type"], "csv")
        self.assertEqual(ds["row_count"], 100)

    def test_add_duplicate_name_fails(self):
        self.db.add_dataset(
            name="test_ds", source_type="csv", file_path="/tmp/test.csv",
            table_name="test_ds", schema_json="[]", row_count=0,
        )
        result = self.db.add_dataset(
            name="test_ds", source_type="csv", file_path="/tmp/test2.csv",
            table_name="test_ds_2", schema_json="[]", row_count=0,
        )
        self.assertFalse(result["success"])
        self.assertIn("已存在", result["error"])

    def test_list_datasets(self):
        self.db.add_dataset("a", "csv", "/a.csv", "a", "[]", 10)
        self.db.add_dataset("b", "excel", "/b.xlsx", "b", "[]", 20)
        datasets = self.db.list_datasets()
        self.assertEqual(len(datasets), 2)

    def test_delete_dataset(self):
        self.db.add_dataset("del_me", "csv", "/d.csv", "del_me", "[]", 0)
        result = self.db.delete_dataset("del_me")
        self.assertTrue(result["success"])
        self.assertIsNone(self.db.get_dataset("del_me"))

    def test_delete_nonexistent_fails(self):
        result = self.db.delete_dataset("nope")
        self.assertFalse(result["success"])

    def test_update_dataset(self):
        self.db.add_dataset("up", "csv", "/u.csv", "up", "[]", 0)
        result = self.db.update_dataset("up", row_count=500, description="updated")
        self.assertTrue(result["success"])
        ds = self.db.get_dataset("up")
        self.assertEqual(ds["row_count"], 500)
        self.assertEqual(ds["description"], "updated")

    def test_add_dataset_with_display_name(self):
        """add_dataset 带 display_name 时能读回（原始中文文件名）。"""
        self.db.add_dataset(
            "ds_202507242126", "csv", "/tmp/x.csv", "ds_202507242126", "[]", 17,
            display_name="山东省经济、农业、人口普查公报信息202507242126",
        )
        ds = self.db.get_dataset("ds_202507242126")
        self.assertIsNotNone(ds)
        self.assertEqual(ds["display_name"], "山东省经济、农业、人口普查公报信息202507242126")

    def test_display_name_defaults_empty(self):
        """不传 display_name 时默认空字符串（兼容旧调用）。"""
        self.db.add_dataset("no_disp", "csv", "/n.csv", "no_disp", "[]", 0)
        ds = self.db.get_dataset("no_disp")
        self.assertEqual(ds["display_name"], "")

    def test_list_datasets_includes_display_name(self):
        """list_datasets 返回含 display_name 字段。"""
        self.db.add_dataset("a", "csv", "/a.csv", "a", "[]", 10,
                            display_name="销售数据")
        datasets = self.db.list_datasets()
        self.assertEqual(datasets[0]["display_name"], "销售数据")

    def test_update_display_name(self):
        """update_dataset 能更新 display_name。"""
        self.db.add_dataset("updn", "csv", "/u.csv", "updn", "[]", 0)
        r = self.db.update_dataset("updn", display_name="新名称")
        self.assertTrue(r["success"])
        self.assertEqual(self.db.get_dataset("updn")["display_name"], "新名称")

    def test_get_all_table_names(self):
        self.db.add_dataset("t1", "csv", "/t1.csv", "table_one", "[]", 0)
        self.db.add_dataset("t2", "mysql", "db:erp", "erp_orders", "[]", 0)
        names = self.db.get_all_table_names()
        self.assertIn("table_one", names)
        self.assertIn("erp_orders", names)

    # ── 多用户隔离：A 用户的数据集对 B 用户不可见 ──
    def test_isolation_list_by_owner(self):
        self.db.add_dataset("a_ds", "csv", "/a.csv", "a_table", "[]", 10,
                            owner_user_id="userA")
        self.db.add_dataset("b_ds", "csv", "/b.csv", "b_table", "[]", 20,
                            owner_user_id="userB")
        a_list = self.db.list_datasets(owner_user_id="userA")
        b_list = self.db.list_datasets(owner_user_id="userB")
        a_names = {d["name"] for d in a_list}
        b_names = {d["name"] for d in b_list}
        self.assertEqual(a_names, {"a_ds"})
        self.assertEqual(b_names, {"b_ds"})

    def test_isolation_get_by_owner(self):
        self.db.add_dataset("shared", "csv", "/a.csv", "a_table", "[]", 10,
                            owner_user_id="userA")
        # B 用户查不到 A 的数据集
        self.assertIsNone(self.db.get_dataset("shared", owner_user_id="userB"))
        # A 用户能查到
        ds = self.db.get_dataset("shared", owner_user_id="userA")
        self.assertIsNotNone(ds)
        self.assertEqual(ds["owner_user_id"], "userA")

    def test_isolation_delete_by_owner(self):
        self.db.add_dataset("shared", "csv", "/a.csv", "a_table", "[]", 10,
                            owner_user_id="userA")
        # B 用户尝试删除 A 的数据集 —— 应失败（防越权）
        result_b = self.db.delete_dataset("shared", owner_user_id="userB")
        self.assertFalse(result_b["success"])
        # 数据集仍存在（A 还能查到）
        self.assertIsNotNone(self.db.get_dataset("shared", owner_user_id="userA"))
        # A 用户删除自己的数据集 —— 成功
        result_a = self.db.delete_dataset("shared", owner_user_id="userA")
        self.assertTrue(result_a["success"])


if __name__ == "__main__":
    unittest.main()
