"""POST /api/analysis 端点测试：成功 JSON / 异常 500 / 校验 400 / 会话 404。

_get_memory_service / _get_planner_agent 换桩（conftest.swap_srv_seam），
_sanitize_result / _normalize_paths 走真实现（序列化契约值得覆盖）。
"""
from tests.test_chat_sse_api import _FakeMemoryService


class _FakeAnalyst:
    def __init__(self, result=None, exc=None):
        self.result = result or {}
        self.exc = exc
        self.calls: list[dict] = []

    def run(self, payload: dict) -> dict:
        self.calls.append(payload)
        if self.exc:
            raise self.exc
        return self.result


def test_analysis_success(client, auth_headers, swap_srv_seam):
    mem = _FakeMemoryService(session_id="sess_a1")
    analyst = _FakeAnalyst(result={
        "report": {"markdown": "# 分析报告\n\n销量整体呈上升趋势。"},
        "title": "销量分析",
    })
    swap_srv_seam("_get_memory_service", lambda *a, **k: mem)
    swap_srv_seam("_get_planner_agent", lambda uid: analyst)

    r = client.post("/api/analysis", json={"query": "分析销量"}, headers=auth_headers)

    assert r.status_code == 200
    body = r.json()
    assert body["report"]["markdown"] == "# 分析报告\n\n销量整体呈上升趋势。"
    assert body["title"] == "销量分析"
    # planner 收到 query + user_id
    assert analyst.calls == [{"query": "分析销量", "user_id": analyst.calls[0]["user_id"]}]
    # 分析摘要入记忆，带 [分析结果] 前缀且截断至 500 字
    assert len(mem.end_calls) == 1
    assert mem.end_calls[0]["response"].startswith("[分析结果] # 分析报告")


def test_analysis_planner_failure_returns_500(client, auth_headers, swap_srv_seam):
    mem = _FakeMemoryService()
    analyst = _FakeAnalyst(exc=RuntimeError("boom"))
    swap_srv_seam("_get_memory_service", lambda *a, **k: mem)
    swap_srv_seam("_get_planner_agent", lambda uid: analyst)

    r = client.post("/api/analysis", json={"query": "分析"}, headers=auth_headers)

    assert r.status_code == 500
    assert r.json() == {"success": False, "errors": ["boom"]}
    # 失败路径不写记忆
    assert mem.end_calls == []


def test_analysis_empty_query_returns_400(client, auth_headers):
    r = client.post("/api/analysis", json={"query": ""}, headers=auth_headers)
    assert r.status_code == 400
    assert r.json()["error"] == "query is required"


def test_analysis_unknown_session_returns_404(client, auth_headers, swap_srv_seam):
    mem = _FakeMemoryService(fail_begin=True)
    swap_srv_seam("_get_memory_service", lambda *a, **k: mem)

    r = client.post("/api/analysis", json={"query": "分析"}, headers=auth_headers)
    assert r.status_code == 404
