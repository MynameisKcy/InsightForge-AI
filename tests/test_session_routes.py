import secrets as _secrets
import time as _time
import unittest
from unittest.mock import patch

import api.deps as deps
import api.fastapi_server as srv
import database.user_db as user_db_mod
from memory.service import MemoryService


def _uniq_account(label: str = "u") -> str:
    """每次生成唯一账号，避免跨测试运行的用户名冲突/密码残留。"""
    return f"{label}_facade_{_secrets.token_hex(4)}_{int(_time.time()) % 100000}"


def _register_login(label: str) -> str:
    acct = _uniq_account(label)
    user_db_mod.user_db.register(acct, "Test1234!")
    return user_db_mod.user_db.login(acct, "Test1234!")["token"]


def _fake_service() -> MemoryService:
    """_ltm/_recall 被 mock 的 MemoryService，避免真实模型/DB。"""
    with patch("memory.service.LongTermMemory"), patch(
        "memory.service.get_memory_recall"
    ):
        svc = MemoryService(llm_factory=lambda uid: (lambda m: ""))
    svc._ltm.get_user_sessions.return_value = [{"session_id": "s1", "title": "t"}]
    svc._ltm.get_last_n_turns.return_value = [{"role": "user", "content": "hi"}]
    # 不存在的会话 → get_session_owner 返回 None → _assert_owner 抛 PermissionError → 404
    svc._ltm.get_session_owner.return_value = None
    return svc


class SessionRoutesTests(unittest.TestCase):
    def setUp(self):
        from fastapi.testclient import TestClient
        self.client = TestClient(srv.app)
        self._svc = _fake_service()
        # 路由处理器经 deps._get_memory_service()（请求期解析），替换 deps 模块属性
        self._orig = deps._get_memory_service
        deps._get_memory_service = lambda *a, **k: self._svc

    def tearDown(self):
        deps._get_memory_service = self._orig

    def test_list_sessions_returns_200(self):
        tok = _register_login("list")
        r = self.client.get("/api/sessions", headers={"Authorization": f"Bearer {tok}"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["sessions"], [{"session_id": "s1", "title": "t"}])

    def test_history_returns_200(self):
        tok = _register_login("hist")
        r = self.client.get(
            "/api/conversation/history", headers={"Authorization": f"Bearer {tok}"}
        )
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["turns"], [{"role": "user", "content": "hi"}])

    def test_get_missing_session_returns_404(self):
        tok = _register_login("get")
        r = self.client.get(
            "/api/sessions/ghost", headers={"Authorization": f"Bearer {tok}"}
        )
        self.assertEqual(r.status_code, 404)

    def test_delete_missing_session_returns_404(self):
        tok = _register_login("del")
        r = self.client.delete(
            "/api/sessions/ghost", headers={"Authorization": f"Bearer {tok}"}
        )
        self.assertEqual(r.status_code, 404)

    def test_rename_missing_session_returns_404(self):
        tok = _register_login("ren")
        r = self.client.patch(
            "/api/sessions/ghost",
            json={"title": "x"},
            headers={"Authorization": f"Bearer {tok}"},
        )
        self.assertEqual(r.status_code, 404)


if __name__ == "__main__":
    unittest.main()
