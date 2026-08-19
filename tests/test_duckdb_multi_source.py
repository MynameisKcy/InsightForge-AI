import csv
import os
import unittest


class TestDuckDBMultiSource(unittest.TestCase):
    def setUp(self):
        # 使用 data/datasets 目录，满足 validate_csv_path 路径穿越防护
        from utils.path_tool import get_abs_path
        self.tmp_dir = get_abs_path("data/datasets")
        os.makedirs(self.tmp_dir, exist_ok=True)
        self._created_files = []

    def _make_csv(self, filename, rows):
        path = os.path.join(self.tmp_dir, filename)
        with open(path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
            writer.writeheader()
            writer.writerows(rows)
        self._created_files.append(path)
        return path

    def tearDown(self):
        for f in self._created_files:
            try:
                os.remove(f)
            except OSError:
                pass

    def test_load_csv_dataset(self):
        from database.duckdb_manager import DuckDBManager
        csv_path = self._make_csv("test.csv", [
            {"name": "Alice", "score": 90},
            {"name": "Bob", "score": 85},
        ])
        db = DuckDBManager(user_id="test_multi")
        result = db.load_csv_dataset(csv_path, "test_table")
        self.assertTrue(result["success"])
        self.assertEqual(result["row_count"], 2)
        tables = db.get_table_names()
        self.assertIn("test_table", tables)
        df = db.query_df("SELECT * FROM test_table")
        self.assertEqual(len(df), 2)
        db.close()

    def test_load_multiple_datasets(self):
        from database.duckdb_manager import DuckDBManager
        csv1 = self._make_csv("sales.csv", [{"product": "A", "amount": 100}])
        csv2 = self._make_csv("inventory.csv", [{"product": "A", "qty": 50}])
        db = DuckDBManager(user_id="test_multi2")
        r1 = db.load_csv_dataset(csv1, "sales")
        r2 = db.load_csv_dataset(csv2, "inventory")
        self.assertTrue(r1["success"])
        self.assertTrue(r2["success"])
        df = db.query_df(
            "SELECT s.product, s.amount, i.qty FROM sales s JOIN inventory i ON s.product = i.product"
        )
        self.assertEqual(len(df), 1)
        self.assertEqual(df.iloc[0]["amount"], 100)
        self.assertEqual(df.iloc[0]["qty"], 50)
        db.close()

    def test_drop_table(self):
        from database.duckdb_manager import DuckDBManager
        csv_path = self._make_csv("tmp.csv", [{"x": 1}])
        db = DuckDBManager(user_id="test_drop")
        db.load_csv_dataset(csv_path, "tmp_table")
        self.assertIn("tmp_table", db.get_table_names())
        result = db.drop_table("tmp_table")
        self.assertTrue(result)
        self.assertNotIn("tmp_table", db.get_table_names())
        db.close()

    def test_safe_ident(self):
        from database.safety import safe_ident
        self.assertEqual(safe_ident("normal"), '"normal"')
        self.assertEqual(safe_ident('has"quote'), '"has""quote"')

    def test_customer_isolation_by_user(self):
        """客户数据按 user_id 隔离：A 上传的客户对 B 不可见。"""
        import database.customer_profiles as cmod
        from database.customer_profiles import get_customer_count, get_customer_overview
        from database.duckdb_manager import DuckDBManager

        # 用临时 customers.db，避免污染真实库
        orig_path = cmod._CUSTOMER_DB_PATH
        tmp_cust = os.path.join(self.tmp_dir, "test_customers.db")
        cmod._CUSTOMER_DB_PATH = tmp_cust
        try:
            csv_a = self._make_csv("cust_a.csv", [
                {"customer_id": "C1", "customer_name": "Alice", "city": "BJ"},
                {"customer_id": "C2", "customer_name": "Bob", "city": "SH"},
            ])
            csv_b = self._make_csv("cust_b.csv", [
                {"customer_id": "C1", "customer_name": "Carol", "city": "GZ"},
            ])
            # 通过构造函数触发 _load_csv → persist_customer_profiles
            db_a = DuckDBManager(csv_path=csv_a, table_name="cust_table_a", user_id="userA")
            db_a.close()

            db_b = DuckDBManager(csv_path=csv_b, table_name="cust_table_b", user_id="userB")
            db_b.close()

            # A 用户只看到自己的 2 个客户（含 Alice），看不到 B 的 Carol
            a_overview = get_customer_overview("userA", top_n=10)
            a_names = {c["customer_name"] for c in a_overview}
            self.assertEqual(a_names, {"Alice", "Bob"})

            # B 用户只看到自己的 Carol（C1 在 B 这里是 Carol，不是 Alice）
            b_overview = get_customer_overview("userB", top_n=10)
            b_names = {c["customer_name"] for c in b_overview}
            self.assertEqual(b_names, {"Carol"})

            # 复合主键生效：A 的 C1(Alice) 与 B 的 C1(Carol) 互不覆盖
            a_c1 = [c for c in a_overview if c["customer_id"] == "C1"][0]
            self.assertEqual(a_c1["customer_name"], "Alice")

            # 统计也按用户隔离
            self.assertEqual(get_customer_count("userA")["total_customers"], 2)
            self.assertEqual(get_customer_count("userB")["total_customers"], 1)
            # 不存在的用户返回 0
            self.assertEqual(get_customer_count("userX")["total_customers"], 0)
        finally:
            cmod._CUSTOMER_DB_PATH = orig_path
            for f in (tmp_cust, tmp_cust + "-wal", tmp_cust + "-shm"):
                try:
                    os.remove(f)
                except OSError:
                    pass

    def test_upload_csv_populates_profiles(self):
        """上传路径(load_csv_dataset)也应触发客户画像持久化——回归 1/4 路径 bug。"""
        import database.customer_profiles as cmod
        from database.customer_profiles import get_customer_count
        from database.duckdb_manager import DuckDBManager

        orig_path = cmod._CUSTOMER_DB_PATH
        tmp_cust = os.path.join(self.tmp_dir, "test_customers_upload.db")
        cmod._CUSTOMER_DB_PATH = tmp_cust
        try:
            csv_path = self._make_csv("cust_upload.csv", [
                {"customer_id": "U1", "customer_name": "Zoe", "city": "BJ"},
                {"customer_id": "U2", "customer_name": "Ian", "city": "SH"},
                {"customer_id": "U1", "customer_name": "Zoe", "city": "BJ"},
            ])
            db = DuckDBManager(user_id="userUpload")
            try:
                result = db.load_csv_dataset(csv_path, "cust_upload_table")
                self.assertTrue(result["success"])
                # 上传路径必须落库(修复前这里为 0)
                self.assertEqual(get_customer_count("userUpload")["total_customers"], 2)
            finally:
                db.close()
        finally:
            cmod._CUSTOMER_DB_PATH = orig_path
            for f in (tmp_cust, tmp_cust + "-wal", tmp_cust + "-shm"):
                try:
                    os.remove(f)
                except OSError:
                    pass


if __name__ == "__main__":
    unittest.main()
