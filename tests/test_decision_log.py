"""decision_log 契约测试（架构评审 R2 候选4：观测埋点收口）。

钉住三块契约：make_decision 自动字段与截断、JSONL 分片落盘（按 日期_用户 分文件、
文件名消毒、失败静默）、发布器注入（SSE 解耦后路由语义）。
日志目录经 monkeypatch 替换模块内 get_abs_path 指向 tmp，不污染仓库 logs/。
"""
import json

import pytest

import utils.decision_log as dl


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    """全新发布器状态 + 日志根指向 tmp（真实布局为 <root>/logs/decisions）。"""
    monkeypatch.setattr(dl, "_publisher", None, raising=False)
    monkeypatch.setattr(dl, "get_abs_path", lambda _p: str(tmp_path / "logs"))
    monkeypatch.setattr(dl, "get_user_id", lambda: "u_test")
    monkeypatch.setattr(dl, "get_session_id", lambda: "sess_1")
    return tmp_path


# ── make_decision ──

def test_make_decision_fills_context_fields():
    d = dl.make_decision(source="tool_call", tool_selected="run_sql")
    assert d.user_id == "u_test"
    assert d.session_id == "sess_1"
    assert d.timestamp            # ISO8601 自动填充


def test_make_decision_truncates_long_text():
    d = dl.make_decision(reasoning="r" * 900, result_summary="s" * 400)
    assert len(d.reasoning) == 500
    assert len(d.result_summary) == 200


# ── JSONL 落盘 ──

def test_log_decision_writes_jsonl_line(_isolate):
    dl.log_decision(dl.make_decision(source="planner", tool_selected="plan"))
    files = list((_isolate / "logs" / "decisions").glob("*.jsonl"))
    assert len(files) == 1
    assert "2026-" in files[0].name and files[0].name.endswith("_u_test.jsonl")
    rec = json.loads(files[0].read_text(encoding="utf-8"))
    assert rec["source"] == "planner" and rec["tool_selected"] == "plan"


def test_log_decision_shards_by_user(_isolate):
    dl.log_decision(dl.make_decision(source="tool_call"))
    other = dl.make_decision(source="tool_call")
    other.user_id = "u_other"
    dl.log_decision(other)

    names = {f.name for f in (_isolate / "logs" / "decisions").glob("*.jsonl")}
    assert any(n.endswith("_u_test.jsonl") for n in names)
    assert any(n.endswith("_u_other.jsonl") for n in names)   # 多用户不混写


def test_safe_sanitizes_unsafe_filename_chars():
    assert dl._safe('a"b/c\\d:e') == "a_b_c_d_e"
    assert dl._safe("") == "default"
    assert len(dl._safe("x" * 100)) == 40


def test_log_decision_failure_is_silent(monkeypatch):
    monkeypatch.setattr(dl, "get_abs_path",
                        lambda _p: str(__import__("pathlib").Path("Z:/definitely/not/writable/here")))
    monkeypatch.setattr(dl, "_write_lock", __import__("threading").Lock())
    dl.log_decision(dl.make_decision(source="tool_call"))     # 不抛即通过


# ── 发布器注入（SSE 解耦）──

def test_emit_decision_routes_via_publisher():
    seen = []
    dl.set_decision_publisher(seen.append)
    payload = {"source": "tool_call", "tool": "run_sql"}
    dl.emit_decision(payload)
    assert seen == [payload]


def test_emit_decision_silent_without_publisher():
    dl.emit_decision({"source": "tool_call"})                 # 未接线：静默丢弃
    assert True


def test_emit_decision_swallows_publisher_errors():
    def boom(_):
        raise RuntimeError("queue down")
    dl.set_decision_publisher(boom)
    dl.emit_decision({"source": "tool_call"})                 # 推送失败不影响业务
