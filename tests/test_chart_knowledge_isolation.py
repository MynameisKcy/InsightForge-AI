"""图表知识库 owner 隔离回归测试（chart-knowledge-isolation spec）。

用临时 SQLite 文件验证：
- A 保存的图表知识 B 经四条检索路径（关键词/类型/最近/RAG 上下文）均不可见；
- 公共 system 行对所有用户可见；
- 无命中时"暂无历史图表参考数据"占位降级；
- 旧库（无 owner 列）打开即迁移为 system 公共，且幂等。
组织方式对齐 test_vector_store_isolation.py。
"""
import os
import sqlite3
import tempfile
import unittest

from rag.chart_knowledge import PUBLIC_OWNER, ChartKnowledgeBase


def _make_kb() -> ChartKnowledgeBase:
    """每用例独立临时 SQLite 文件，避免用例间数据串扰。"""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    return ChartKnowledgeBase(db_path=path)


def _chart(title: str, **kw) -> dict:
    info = {
        "chart_type": "bar",
        "title": title,
        "x_col": "Month",
        "y_col": "revenue",
        "data_summary": f"{title} 摘要",
        "analysis_text": f"{title} 结论",
        "chart_path": f"/reports/{title}.html",
        "task_context": f"{title} 任务",
    }
    info.update(kw)
    return info


class ChartKnowledgeOwnerIsolationTests(unittest.TestCase):
    def test_retrieve_isolated_by_owner(self):
        kb = _make_kb()
        kb.save_chart(_chart("alice 销售分析"), user_id="alice")
        kb.save_chart(_chart("bob 库存分析"), user_id="bob")

        # 关键词检索隔离：关键词"分析"两行都命中，但 owner 过滤后各见各的
        alice_hits = kb.search_by_keywords("分析", user_id="alice")
        self.assertEqual({c["title"] for c in alice_hits}, {"alice 销售分析"})
        bob_hits = kb.search_by_keywords("分析", user_id="bob")
        self.assertEqual({c["title"] for c in bob_hits}, {"bob 库存分析"})

        # 按类型检索隔离
        alice_by_type = kb.search_by_type("bar", user_id="alice")
        self.assertEqual({c["title"] for c in alice_by_type}, {"alice 销售分析"})

        # 最近图表隔离
        bob_recent = kb.get_recent_charts(user_id="bob")
        self.assertEqual({c["title"] for c in bob_recent}, {"bob 库存分析"})

        # RAG 图表上下文隔离：bob 的上下文不得出现 alice 的任何字样
        ctx = kb.get_chart_context_for_rag("销售 分析", user_id="bob")
        self.assertNotIn("alice", ctx)
        self.assertIn("bob", ctx)

    def test_public_owner_visible_to_all_users(self):
        kb = _make_kb()
        # 直接插公共行（模拟迁移产物 / 公共知识）
        with kb._get_conn() as conn:
            conn.execute(
                "INSERT INTO chart_archive (chart_type, title, owner_user_id, created_at) "
                "VALUES ('line', '公共知识', ?, '2026-01-01T00:00:00')",
                (PUBLIC_OWNER,),
            )
            conn.commit()

        for uid in ("alice", "bob"):
            hits = kb.search_by_keywords("公共", user_id=uid)
            self.assertEqual({c["title"] for c in hits}, {"公共知识"})
            recent = kb.get_recent_charts(user_id=uid)
            self.assertEqual({c["title"] for c in recent}, {"公共知识"})

    def test_no_result_placeholder_kept(self):
        kb = _make_kb()
        kb.save_chart(_chart("alice 私密洞察"), user_id="alice")

        # bob 无命中且无公共行 → 占位降级，不返回他人记录
        ctx = kb.get_chart_context_for_rag("随便什么问题", user_id="bob")
        self.assertIn("暂无", ctx)
        self.assertNotIn("alice", ctx)

    def test_save_chart_records_explicit_owner(self):
        kb = _make_kb()
        kb.save_chart(_chart("x"), user_id="alice")
        with kb._get_conn() as conn:
            rows = conn.execute(
                "SELECT owner_user_id FROM chart_archive").fetchall()
        self.assertEqual([r["owner_user_id"] for r in rows], ["alice"])

    def test_save_chart_falls_back_to_default_outside_request(self):
        kb = _make_kb()
        # 测试进程无请求上下文 → contextvars 默认值兜底为 default
        kb.save_chart(_chart("x"))
        with kb._get_conn() as conn:
            rows = conn.execute(
                "SELECT owner_user_id FROM chart_archive").fetchall()
        self.assertEqual([r["owner_user_id"] for r in rows], ["default"])

    def test_clear_old_data_scoped_to_owner_and_public(self):
        kb = _make_kb()
        # alice 旧行、bob 旧行、system 旧行（created_at 均早于 90 天前）
        kb.save_chart(_chart("alice 旧", chart_type="pie"), user_id="alice")
        kb.save_chart(_chart("bob 旧", chart_type="pie"), user_id="bob")
        with kb._get_conn() as conn:
            conn.execute(
                "UPDATE chart_archive SET created_at = '2020-01-01T00:00:00'")
            conn.commit()

        kb.clear_old_data(days=1, user_id="alice")

        with kb._get_conn() as conn:
            left = {r["owner_user_id"] for r in conn.execute(
                "SELECT owner_user_id FROM chart_archive").fetchall()}
        # alice 的过期行被清，bob 的不动（即便同样过期）
        self.assertEqual(left, {"bob"})


class ChartKnowledgeLegacyMigrationTests(unittest.TestCase):
    def _legacy_db(self) -> str:
        """手工构造旧 schema（无 owner 列）库并写入存量行。"""
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        conn = sqlite3.connect(path)
        conn.execute("""
            CREATE TABLE chart_archive (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chart_type TEXT NOT NULL,
                title TEXT NOT NULL,
                x_col TEXT, y_col TEXT,
                data_summary TEXT, analysis_text TEXT,
                chart_path TEXT, task_context TEXT,
                created_at TEXT NOT NULL
            )
        """)
        conn.execute(
            "INSERT INTO chart_archive (chart_type, title, created_at) "
            "VALUES ('bar', '旧图表', '2025-01-01T00:00:00')")
        conn.commit()
        conn.close()
        return path

    def test_legacy_rows_migrated_to_public_and_idempotent(self):
        path = self._legacy_db()

        kb = ChartKnowledgeBase(db_path=path)      # 打开即迁移
        kb2 = ChartKnowledgeBase(db_path=path)     # 再开一次：幂等

        # 旧行归 system，所有用户均可检索到
        for uid in ("alice", "bob"):
            hits = kb.search_by_keywords("旧图表", user_id=uid)
            self.assertEqual(len(hits), 1)
            self.assertEqual(hits[0]["owner_user_id"], PUBLIC_OWNER)

        # 幂等：两次打开后记录数不变、无重复行
        with kb._get_conn() as conn:
            n = conn.execute("SELECT COUNT(*) AS n FROM chart_archive").fetchone()["n"]
        self.assertEqual(n, 1)
        # kb2 与 kb 指向同一文件，行为一致
        self.assertEqual(len(kb2.get_recent_charts(user_id="alice")), 1)


if __name__ == "__main__":
    unittest.main()
