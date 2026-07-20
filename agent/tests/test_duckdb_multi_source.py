import os
import sys
import unittest
import tempfile
import csv

CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(CURRENT_DIR)
PROJECT_PARENT = os.path.dirname(PROJECT_ROOT)
for p in (PROJECT_ROOT, PROJECT_PARENT):
    if p not in sys.path:
        sys.path.insert(0, p)


class TestDuckDBMultiSource(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp()

    def _make_csv(self, filename, rows):
        path = os.path.join(self.tmp_dir, filename)
        with open(path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
            writer.writeheader()
            writer.writerows(rows)
        return path

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
        from database.duckdb_manager import safe_ident
        self.assertEqual(safe_ident("normal"), '"normal"')
        self.assertEqual(safe_ident('has"quote'), '"has""quote"')


if __name__ == "__main__":
    unittest.main()
