# agent/tests/test_data_resolver.py
"""DataResolver 测试：验证用户自然语言输入能定位到具体数据集。

重点：display_name（原始中文文件名）参与匹配——用户输入"山东"
应命中 display_name 含「山东省」的数据集，而非落到 dynamic_all（返回所有）。
"""
import os
import tempfile

import unittest


def _make_resolver_with_db():
    """构造一个用临时 datasources_db 的 DataResolver 测试环境。

    DataResolver 直接用模块级单例 datasources_db；测试通过 monkeypatch
    让 _load_dynamic_datasets 返回我们构造的数据集，避免动单例。
    """
    from database import data_resolver
    return data_resolver


class TestDataResolverDisplayMatch(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmp_dir, "test_resolver.db")
        from database.datasources_db import DatasourcesDB
        self.db = DatasourcesDB(db_path=self.db_path)
        # 造两个数据集：一个中文 display_name，一个英文
        self.db.add_dataset(
            "ds_202507242126", "csv", "/tmp/shandong.csv", "ds_202507242126", "[]", 17,
            display_name="山东省经济、农业、人口普查公报信息202507242126",
            owner_user_id="userA",
        )
        self.db.add_dataset(
            "sales_2024", "csv", "/tmp/sales.csv", "sales_2024", "[]", 100,
            display_name="2024年销售数据",
            owner_user_id="userA",
        )

    def tearDown(self):
        if os.path.exists(self.db_path):
            os.remove(self.db_path)

    def _resolve(self, query):
        """monkeypatch _load_dynamic_datasets 返回本测试的 db 数据集，再调 resolve。"""
        data_resolver = _make_resolver_with_db()
        original = data_resolver.DataResolver._load_dynamic_datasets
        data_resolver.DataResolver._load_dynamic_datasets = staticmethod(
            lambda user_id=None: self.db.list_datasets(owner_user_id=user_id)
        )
        try:
            return data_resolver.DataResolver.resolve(query, user_id="userA")
        finally:
            data_resolver.DataResolver._load_dynamic_datasets = original

    def test_query_matches_display_name_chinese(self):
        """用户输入"山东"应命中 display_name 含「山东省」的数据集（关键词匹配）。"""
        r = self._resolve("分析山东的人口数据")
        self.assertEqual(r["name"], "ds_202507242126")
        self.assertEqual(r["matched_by"], "dynamic_keyword_match")

    def test_query_matches_sales_display(self):
        """用户输入"销售"应命中 display_name 含「销售」的数据集。"""
        r = self._resolve("看看2024销售数据")
        self.assertEqual(r["name"], "sales_2024")
        self.assertEqual(r["matched_by"], "dynamic_keyword_match")

    def test_no_match_falls_back_to_all(self):
        """输入不含任何数据集关键词时落到 dynamic_all（返回所有，取第一个）。"""
        r = self._resolve("随便看看")
        self.assertEqual(r["matched_by"], "dynamic_all")


if __name__ == "__main__":
    unittest.main()
