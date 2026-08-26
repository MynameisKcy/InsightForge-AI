"""user_id → 模型解析 传播回归测试。

锁定的 bug 类别（docs/IMPROVEMENT_DIRECTIONS.md 方向#2）：
1. Agent/服务构造时不传 user_id → 钉死 .env 默认模型（配了网页模型的用户 403）；
2. 进程级共享"当前用户"状态（旧 _memory_llm_user 字典）在跨用户交错下用错模型；
3. summarizer 懒加载缓存首个实例 → 同样钉死单用户。

修复形态：summarizer 经 llm_factory(user_id) 按用户解析，无共享可变状态。
"""
from unittest.mock import patch

import memory.short_term as short_term_mod
from agent.tools import agent_tools
from memory.recall import MemoryRecallService
from memory.service import MemoryService
from memory.short_term import ConversationMemory, get_summarizer, set_summarizer_factory
from utils.request_context import reset_request_context, set_request_context


class _FakeSummarizer:
    def summarize(self, turns, previous_summary=""):
        return "摘要文本"


class _RecordingFactory:
    """记录每次请求的 user_id，返回假 summarizer（模拟 llm_factory(user_id)）。"""

    def __init__(self):
        self.uids: list[str] = []

    def __call__(self, uid):
        self.uids.append(uid)
        return _FakeSummarizer()


class _FakeLtm:
    """避开真实 memory.db 的 LongTermMemory 桩。"""

    def get_session_memory_meta(self, sid):
        return ("旧摘要", -1)

    def get_turns_after(self, sid, watermark):
        return [{"role": "user", "content": "q1"},
                {"role": "assistant", "content": "a1"}]

    def get_session_max_turn_index(self, sid):
        return 1

    def get_session_title(self, sid):
        return "t"

    def save_session_memory_meta(self, *a):
        pass

    def save_summary(self, *a, **k):
        pass

    def mark_session_finalized(self, *a):
        pass


class _FakeMemoryStore:
    def add_session_memory(self, *a, **k):
        pass


class TestPerUserSummarizer:
    """pytest 风格（模块级状态需精细恢复，统一 setup/teardown 语义手写）。"""

    def setup_method(self):
        self._orig_factory = short_term_mod._summarizer_factory
        self.factory = _RecordingFactory()
        set_summarizer_factory(self.factory)

    def teardown_method(self):
        short_term_mod._summarizer_factory = self._orig_factory

    def test_get_summarizer_cross_user_interleave(self):
        # 跨用户交错：每次解析都拿到调用方自己的 user_id，
        # 无共享"当前用户"状态可被后来的请求翻转
        get_summarizer("alice")
        get_summarizer("bob")
        get_summarizer("alice")
        assert self.factory.uids == ["alice", "bob", "alice"]

    def test_compress_uses_session_owner_uid(self):
        # 会话压缩：summarizer 用该会话属主的 user_id 解析
        with patch.object(short_term_mod, "_get_ltm", _FakeLtm()):
            mem = ConversationMemory(user_id="alice", session_id="s1")
            mem._context_window = 1000
            mem.last_measured_input_tokens = 950  # >= 90% 窗口，触发压缩
            for i in range(6):
                mem.turns.append({"role": "user", "content": f"q{i}"})
            mem._maybe_compress()
        assert self.factory.uids == ["alice"]

    def test_finalize_session_resolves_per_user_summarizer(self):
        # 闲置 finalize 线程路径：显式 user_id 参数解析，与并发请求线程无关
        recall = MemoryRecallService(ltm=_FakeLtm(), memory_store=_FakeMemoryStore())
        recall.finalize_session("s1", "bob")
        assert self.factory.uids == ["bob"]

    def test_memory_service_wires_llm_factory(self):
        # MemoryService 构造 → set_summarizer_factory(lambda uid: ...)：
        # get_summarizer(uid) 经 llm_factory(uid) 解析模型
        llm_calls: list[str] = []
        with patch("memory.service.LongTermMemory"), \
                patch("memory.service.get_memory_recall"):
            MemoryService(llm_factory=lambda uid: llm_calls.append(uid) or (lambda m: ""))
        get_summarizer("carol")
        assert llm_calls == ["carol"]


# ── 工具/端点构造透传 user_id ──

def test_document_report_tool_passes_ctx_user():
    captured = {}

    class _FakeDocAgent:
        def __init__(self, user_id=None, model=None):
            captured["user_id"] = user_id

        def run(self, path, question=None):
            return {"markdown": "# md"}

    token = set_request_context(user_id="u_doc")
    try:
        with patch("agents.document_report_agent.DocumentReportAgent", _FakeDocAgent):
            out = agent_tools.document_report.invoke(
                {"file_path": "x.pdf", "question": ""})
    finally:
        reset_request_context(token)
    assert "报告生成失败" not in out
    assert captured["user_id"] == "u_doc"


def test_quick_data_insight_passes_ctx_user_to_agents():
    sql_seen, trend_seen = {}, {}

    class _FakeSQLAgent:
        def __init__(self, user_id=None, **k):
            sql_seen["user_id"] = user_id

        def run(self, payload):
            return {"dataframe_json": '[{"m": 1}, {"m": 2}]', "row_count": 2}

    class _FakeAnalysisAgent:
        def __init__(self, analyzer, user_id=None, model=None):
            trend_seen["user_id"] = user_id

        def run(self, payload):
            return {"insight": "整体上升"}

    token = set_request_context(user_id="u_qdi")
    try:
        with patch("agents.sql_agent.SQLAgent", _FakeSQLAgent), \
                patch("agents.analysis_agent.AnalysisAgent", _FakeAnalysisAgent):
            out = agent_tools.quick_data_insight.invoke({"query": "趋势如何"})
    finally:
        reset_request_context(token)
    assert "整体上升" in out
    assert sql_seen["user_id"] == "u_qdi"
    assert trend_seen["user_id"] == "u_qdi"


def test_export_endpoint_passes_user_id(client, auth, tmp_path):
    captured = {}

    class _FakeExportAgent:
        def __init__(self, user_id=None, model=None):
            captured["user_id"] = user_id

        def run(self, payload):
            f = tmp_path / "report.md"
            f.write_text(payload["markdown"], encoding="utf-8")
            return {"files": [{"path": str(f), "format": "md"}], "errors": []}

    with patch("agents.export_agent.ExportAgent", _FakeExportAgent):
        r = client.post(
            "/api/report/export",
            json={"markdown": "# 报告", "title": "t", "format": "md"},
            headers=auth["headers"],
        )
    assert r.status_code == 200
    assert captured["user_id"] == auth["user_id"]
