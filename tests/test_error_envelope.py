"""统一错误信封契约测试（架构评审 R2 候选7）。

历史形态：{"error"}（analysis/chat/datasets/knowledge/sessions）、
{"success","error"}（users/datasets）、{"ok","error"}（sessions/users/settings
的 JSON 体校验）、{"detail"}（auth 401 与 FastAPI 默认）四种并存，前端只能
按端点各写一套解析。规范信封为其超集：

    {"success": false, "error": "<人类可读消息>"}

读 .error 的解析器与判 .success 的解析器都兼容（纯增量键）；成功体形态
（ok:true / success:true / 裸载荷）不属错误信封范畴，不动。
"""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.errors import error_response, register_exception_handlers


class TestErrorResponse:
    def test_canonical_shape(self):
        resp = error_response("boom", 400)
        import json
        assert resp.status_code == 400
        assert json.loads(resp.body) == {"success": False, "error": "boom"}

    def test_extra_keys_passthrough(self):
        import json
        resp = error_response("bad", 422, detail="field x")
        assert json.loads(resp.body) == {"success": False, "error": "bad", "detail": "field x"}


@pytest.fixture
def envelope_app():
    """最小应用：注册三类异常处理器 + 各触发端点。"""
    app = FastAPI()
    register_exception_handlers(app)

    @app.get("/boom")
    async def boom():
        raise RuntimeError("kaboom")

    @app.get("/teapot")
    async def teapot():
        from fastapi import HTTPException
        raise HTTPException(status_code=418, detail="茶壶")

    @app.post("/validated")
    async def validated(item: dict):
        return {"ok": True}

    return app


class TestExceptionHandlers:
    def test_http_exception_detail_becomes_error(self, envelope_app):
        client = TestClient(envelope_app)
        r = client.get("/teapot")
        assert r.status_code == 418
        assert r.json() == {"success": False, "error": "茶壶"}

    def test_validation_error_summarized_as_string(self, envelope_app):
        client = TestClient(envelope_app)
        r = client.post("/validated", json="not-a-dict")
        assert r.status_code == 422
        body = r.json()
        assert body["success"] is False
        # 错误是可读字符串而非 pydantic 的 detail 数组
        assert isinstance(body["error"], str) and body["error"]

    def test_unhandled_exception_is_generic_500(self, envelope_app):
        client = TestClient(envelope_app, raise_server_exceptions=False)
        r = client.get("/boom")
        assert r.status_code == 500
        # 不泄漏内部异常信息
        assert r.json() == {"success": False, "error": "Internal Server Error"}


class TestRealAppEndpoints:
    """真实 app 上的信封抽查：auth 401 与 422 走同一信封。"""

    def test_unauthorized_body_is_canonical(self, client):
        r = client.get("/api/datasets")
        assert r.status_code == 401
        body = r.json()
        assert body["success"] is False
        assert body["error"]  # 原为 {"detail": ...}，前端读不到消息
