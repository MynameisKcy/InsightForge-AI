"""TaskStore（#1）：JSON 文件持久化的 CRUD / owner 隔离 / 路径安全 / 进度更新。

离线测试：set_tasks_root(tmp) 覆写存储根，不触达生产 data/tasks。
"""
import os
import tempfile
import unittest

from memory.task_store import (
    TaskPathError,
    TaskRecord,
    get_task,
    list_tasks,
    new_task_id,
    save_task,
    set_tasks_root,
    update_progress,
)


class TaskStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="task_store_test_")
        set_tasks_root(self.tmp)
        self.addCleanup(lambda: set_tasks_root(None))

    def _rec(self, owner="u1", **kw):
        base = dict(id=new_task_id(), owner=owner, query="分析趋势",
                    title="趋势报告", status="running")
        base.update(kw)
        return TaskRecord(**base)

    def test_save_and_get_roundtrip(self):
        rec = self._rec(plan=[{"step": 1, "agent": "sql_query", "depends_on": []}])
        save_task(rec)
        got = get_task("u1", rec.id)
        self.assertIsNotNone(got)
        self.assertEqual(got.query, "分析趋势")
        self.assertEqual(got.plan[0]["agent"], "sql_query")
        self.assertTrue(got.created_at)
        self.assertTrue(got.updated_at)

    def test_owner_isolation(self):
        rec = save_task(self._rec(owner="u1"))
        # u2 读不到 u1 的任务（返回 None，不抛）
        self.assertIsNone(get_task("u2", rec.id))
        self.assertEqual(list_tasks("u2"), [])

    def test_get_missing_returns_none(self):
        self.assertIsNone(get_task("u1", "task_nonexistent"))

    def test_path_traversal_rejected(self):
        # 读路径：非法 id 防御式返回 None（不抛、不越界）
        self.assertIsNone(get_task("u1", "../evil"))
        # 写路径：非法 owner/id 直接拒绝
        with self.assertRaises(TaskPathError):
            save_task(TaskRecord(id="../../etc", owner="u1", query="x", title="x"))
        with self.assertRaises(TaskPathError):
            save_task(TaskRecord(id=new_task_id(), owner="../../etc", query="x",
                                 title="x"))

    def test_list_sorted_desc_and_limited(self):
        for i in range(3):
            save_task(self._rec(title=f"t{i}"))
        recs = list_tasks("u1", limit=2)
        self.assertEqual(len(recs), 2)
        # created_at 降序：后建的在前（同秒时间戳，用 updated_at 倒序等价验证）
        self.assertEqual([r.title for r in recs], ["t2", "t1"])

    def test_update_progress(self):
        rec = save_task(self._rec())
        got = update_progress("u1", rec.id,
                              completed_steps=[1],
                              stage_results={"sql_query": {"ok": True}},
                              dataframe_json="[]",
                              status="completed")
        self.assertEqual(got.status, "completed")
        self.assertEqual(got.completed_steps, [1])
        self.assertEqual(got.stage_results["sql_query"], {"ok": True})
        self.assertEqual(got.dataframe_json, "[]")

    def test_update_progress_invalid_status_rejected(self):
        rec = save_task(self._rec())
        with self.assertRaises(ValueError):
            update_progress("u1", rec.id, completed_steps=[], status="half_done")

    def test_update_progress_missing_task_returns_none(self):
        self.assertIsNone(update_progress("u1", "task_ghost", completed_steps=[]))

    def test_atomic_write_no_tmp_leftover(self):
        rec = save_task(self._rec())
        task_dir = os.path.join(self.tmp, "u1")
        self.assertEqual([f for f in os.listdir(task_dir)], [f"{rec.id}.json"])

    def test_unknown_fields_dropped_on_load(self):
        rec = self._rec()
        rec_dict = rec.to_dict()
        rec_dict["hacker_field"] = "x"
        import json as _json

        path = os.path.join(self.tmp, "u1", f"{rec.id}.json")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            _json.dump(rec_dict, f, ensure_ascii=False)
        got = get_task("u1", rec.id)
        self.assertNotIn("hacker_field", got.to_dict())


if __name__ == "__main__":
    unittest.main()
