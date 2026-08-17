"""POST /api/chat SSE 端点测试：token 契约 + 认证/校验 + 记忆编排。

全部离线：_get_memory_service / _get_react_agent 经 conftest.swap_srv_seam 换桩，
fake ReactAgent 的 execute_stream 为同步生成器（_stream_with_heartbeat 在后台
线程消费，与真实 ReactAgent 一致）。

[KEEPALIVE] 心跳因 interval=15s 硬编码不做专测（需 >15s 长测，性价比低）。
"""
import json
import os

import api.fastapi_server as srv
from memory.service import MemoryTurnContext


class _FakeMemoryService:
    """MemoryService 形状桩：begin_turn/end_turn，记录 end_turn 入参。"""

    def __init__(self, session_id="sess_42", is_new=False, fail_begin=False):
        self.session_id = session_id
        self.is_new = is_new
        self.fail_begin = fail_begin
        self.end_calls: list[dict] = []

    def begin_turn(self, user_id, session_id="", query=""):
        if self.fail_begin:
            raise PermissionError("会话不存在")
        return MemoryTurnContext(
            session_id=self.session_id,
            mem_context=[],
            is_new_session=self.is_new,
        )

    def end_turn(self, user_id, session_id, query, response, input_tokens=None):
        self.end_calls.append(
            {"user_id": user_id, "session_id": session_id,
             "query": query, "response": response}
        )


class _FakeReactAgent:
    """execute_stream 同步生成器；on_start 可用于发步骤事件/模拟图表落盘。"""

    def __init__(self, chunks=None, exc=None, on_start=None):
        self.chunks = chunks or []
        self.exc = exc
        self.on_start = on_start
        self._last_input_tokens = 10

    def execute_stream(self, query, history=None, user_id=None,
                       session_id=None, progress_emitter=None, cancel_token=None):
        if self.on_start:
            self.on_start(progress_emitter)
        for chunk in self.chunks:
            yield chunk
        if self.exc:
            raise self.exc


def _step_payloads(text: str) -> list[dict]:
    """从 SSE 文本中解析全部 [STEP:{json}] 载荷。"""
    payloads = []
    for line in text.splitlines():
        if line.startswith("data: [STEP:") and line.endswith("]"):
            payloads.append(json.loads(line[len("data: [STEP:"):-1]))
    return payloads


def test_chat_stream_happy_path(client, auth_headers, swap_srv_seam):
    mem = _FakeMemoryService(session_id="sess_42")
    agent = _FakeReactAgent(chunks=["[THINKING]解析问题中", "分析完成。", "结论如下。"])
    swap_srv_seam("_get_memory_service", lambda *a, **k: mem)
    swap_srv_seam("_get_react_agent", lambda uid: agent)

    r = client.post("/api/chat", json={"query": "分析销量"},
                    headers=auth_headers)

    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/event-stream")
    # 会话 token 开头、DONE 结尾
    assert "data: [SESSION]sess_42\n\n" in r.text
    assert r.text.endswith("data: [DONE]\n\n")
    # 思考状态原样透传，正文按句下发
    assert "data: [THINKING]解析问题中\n\n" in r.text
    assert "data: 分析完成。\n\n" in r.text
    assert "data: 结论如下。\n\n" in r.text
    # 入记忆的是纯正文（[THINKING] 不落库），且带请求元数据
    assert len(mem.end_calls) == 1
    call = mem.end_calls[0]
    assert call["response"] == "分析完成。结论如下。"
    assert call["session_id"] == "sess_42"
    assert call["query"] == "分析销量"


def test_chat_new_session_sends_sessions_reload(client, auth_headers, swap_srv_seam):
    mem = _FakeMemoryService(session_id="sess_new", is_new=True)
    agent = _FakeReactAgent(chunks=["好的。"])
    swap_srv_seam("_get_memory_service", lambda *a, **k: mem)
    swap_srv_seam("_get_react_agent", lambda uid: agent)

    r = client.post("/api/chat", json={"query": "你好"}, headers=auth_headers)

    assert r.status_code == 200
    assert "data: [SESSIONS_RELOAD]\n\n" in r.text
    assert "data: [SESSION]sess_new\n\n" in r.text


def test_chat_step_progress_event(client, auth_headers, swap_srv_seam):
    mem = _FakeMemoryService()

    def _emit_steps(emitter):
        # 模拟 PlannerAgent 在后台线程内直注进度事件
        emitter.emit("step", {"index": 1, "name": "sql_query"})
        emitter.emit("step", {"index": 2, "name": "trend_analysis"})

    agent = _FakeReactAgent(chunks=["开始。"], on_start=_emit_steps)
    swap_srv_seam("_get_memory_service", lambda *a, **k: mem)
    swap_srv_seam("_get_react_agent", lambda uid: agent)

    r = client.post("/api/chat", json={"query": "全链路分析"}, headers=auth_headers)

    assert r.status_code == 200
    steps = _step_payloads(r.text)
    assert steps == [
        {"type": "step", "index": 1, "name": "sql_query"},
        {"type": "step", "index": 2, "name": "trend_analysis"},
    ]


def test_chat_emits_new_chart_url(client, auth_headers, swap_srv_seam, monkeypatch, tmp_path):
    charts_dir = tmp_path / "reports" / "charts"
    charts_dir.mkdir(parents=True)
    # 路由经模块级 get_abs_path("reports/charts") 定位图表目录 → 指到 tmp
    monkeypatch.setattr(srv, "get_abs_path", lambda p: str(tmp_path / p))

    def _write_chart(_emitter):
        # 模拟 VisualizationAgent 在流执行期间落盘新图表（预扫描之后写入才算新图）
        (charts_dir / "trend_20260817.html").write_text(
            "<html>chart</html>", encoding="utf-8")

    mem = _FakeMemoryService()
    agent = _FakeReactAgent(chunks=["图已生成。"], on_start=_write_chart)
    swap_srv_seam("_get_memory_service", lambda *a, **k: mem)
    swap_srv_seam("_get_react_agent", lambda uid: agent)

    r = client.post("/api/chat", json={"query": "画个图"}, headers=auth_headers)

    assert r.status_code == 200
    web_url = "/reports/charts/trend_20260817.html"
    assert f"data: [CHART:{web_url}]\n\n" in r.text
    # 图表 URL 嵌入记忆，历史会话加载时可恢复图表
    assert web_url in mem.end_calls[0]["response"]
    assert os.path.isfile(str(charts_dir / "trend_20260817.html"))


def test_chat_error_token_on_agent_failure(client, auth_headers, swap_srv_seam):
    mem = _FakeMemoryService()
    agent = _FakeReactAgent(chunks=["部分输出。"], exc=RuntimeError("boom"))
    swap_srv_seam("_get_memory_service", lambda *a, **k: mem)
    swap_srv_seam("_get_react_agent", lambda uid: agent)

    r = client.post("/api/chat", json={"query": "出错的请求"}, headers=auth_headers)

    assert r.status_code == 200  # SSE 已开流，错误以 token 下发而非状态码
    assert "data: [ERROR] boom\n\n" in r.text
    assert "[DONE]" not in r.text
    # 异常路径不写记忆
    assert mem.end_calls == []


def test_chat_empty_query_returns_400(client, auth_headers):
    r = client.post("/api/chat", json={"query": "   "}, headers=auth_headers)
    assert r.status_code == 400
    assert r.json()["error"] == "query is required"


def test_chat_without_auth_returns_401(client):
    r = client.post("/api/chat", json={"query": "你好"})
    assert r.status_code == 401


def test_chat_unknown_session_returns_404(client, auth_headers, swap_srv_seam):
    mem = _FakeMemoryService(fail_begin=True)
    swap_srv_seam("_get_memory_service", lambda *a, **k: mem)

    r = client.post("/api/chat",
                    json={"query": "你好", "session_id": "ghost"},
                    headers=auth_headers)
    assert r.status_code == 404
