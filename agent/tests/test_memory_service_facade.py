import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from memory.service import MemoryService


class FacadeTestCase(unittest.TestCase):
    """外观单元测试：LongTermMemory / Recall 用 mock 注入，避免真实 DB/Chroma。"""

    def setUp(self):
        self._patches = []
        self.ltm_mock = self._start("memory.service.LongTermMemory")
        self.recall_mock = self._start("memory.service.get_memory_recall")
        self.svc = MemoryService(llm_callable=lambda messages: "")
        # __init__ 已用上面的 mock 构造 _ltm / _recall
        self.ltm = self.ltm_mock.return_value
        self.recall = self.recall_mock.return_value

    def _start(self, target):
        p = patch(target)
        self._patches.append(p)
        return p.start()

    def tearDown(self):
        for p in self._patches:
            p.stop()

    # ── 读方法 ──

    def test_list_sessions_delegates(self):
        self.ltm.get_user_sessions.return_value = [{"session_id": "s1"}]
        self.assertEqual(self.svc.list_sessions("u1"), [{"session_id": "s1"}])
        self.ltm.get_user_sessions.assert_called_once_with("u1")

    def test_get_conversation_history_delegates_with_limit(self):
        self.ltm.get_last_n_turns.return_value = [{"role": "user"}]
        self.assertEqual(
            self.svc.get_conversation_history("u1", limit=5), [{"role": "user"}]
        )
        self.ltm.get_last_n_turns.assert_called_once_with("u1", n=5)

    def test_get_session_owner_returns_conversation(self):
        self.ltm.get_session_owner.return_value = "u1"
        self.ltm.get_session_conversation.return_value = [{"role": "user"}]
        self.assertEqual(self.svc.get_session("u1", "s1"), [{"role": "user"}])
        self.ltm.get_session_conversation.assert_called_once_with("s1")

    def test_get_session_non_owner_raises(self):
        self.ltm.get_session_owner.return_value = "other"
        with self.assertRaises(PermissionError):
            self.svc.get_session("u1", "s1")
        self.ltm.get_session_conversation.assert_not_called()

    def test_get_session_missing_raises(self):
        self.ltm.get_session_owner.return_value = None
        with self.assertRaises(PermissionError):
            self.svc.get_session("u1", "ghost")

    # ── 改名 ──

    def test_rename_session_owner_delegates(self):
        self.ltm.get_session_owner.return_value = "u1"
        self.svc.rename_session("u1", "s1", "新标题")
        self.ltm.update_session_title.assert_called_once_with("s1", "新标题")

    def test_rename_session_non_owner_raises(self):
        self.ltm.get_session_owner.return_value = "other"
        with self.assertRaises(PermissionError):
            self.svc.rename_session("u1", "s1", "x")
        self.ltm.update_session_title.assert_not_called()


class DeleteSessionTests(unittest.TestCase):
    """delete_session：IDOR 先于任何删除；属主时三层都被调用。"""

    def setUp(self):
        self._patches = []
        self._start("memory.service.LongTermMemory")
        self._start("memory.service.get_memory_recall")
        # clear_session 在 service.py 内作为模块名查找，patch 其所在模块名
        self.clear_mock = self._start("memory.service.clear_session")
        self.svc = MemoryService(llm_callable=lambda messages: "")
        self.ltm = self.svc._ltm
        self.recall = self.svc._recall

    def _start(self, target):
        p = patch(target)
        self._patches.append(p)
        return p.start()

    def tearDown(self):
        for p in self._patches:
            p.stop()

    def test_non_owner_raises_and_touches_nothing(self):
        """关键不变量：非属主删除绝不触发任何层。"""
        self.ltm.get_session_owner.return_value = "other"
        with self.assertRaises(PermissionError):
            self.svc.delete_session("u1", "s1")
        self.ltm.delete_session.assert_not_called()
        self.clear_mock.assert_not_called()
        self.recall.delete_session_memory.assert_not_called()

    def test_missing_session_raises_and_touches_nothing(self):
        self.ltm.get_session_owner.return_value = None
        with self.assertRaises(PermissionError):
            self.svc.delete_session("u1", "ghost")
        self.ltm.delete_session.assert_not_called()
        self.clear_mock.assert_not_called()
        self.recall.delete_session_memory.assert_not_called()

    def test_owner_deletes_all_three_tiers(self):
        """属主删除：LTM 删除 + short_term 清池 + recall embedding 清理。"""
        self.ltm.get_session_owner.return_value = "u1"
        self.svc.delete_session("u1", "s1")
        self.ltm.delete_session.assert_called_once_with("s1")
        self.clear_mock.assert_called_once_with("s1")
        self.recall.delete_session_memory.assert_called_once_with("s1", "u1")

    def test_owner_deletes_even_if_recall_embedding_fails(self):
        """recall 清理失败不应回滚 LTM 删除（与现状一致：仅记日志）。"""
        self.ltm.get_session_owner.return_value = "u1"
        self.recall.delete_session_memory.side_effect = RuntimeError("boom")
        self.svc.delete_session("u1", "s1")  # 不应抛
        self.ltm.delete_session.assert_called_once_with("s1")
        self.clear_mock.assert_called_once_with("s1")


if __name__ == "__main__":
    unittest.main()
