import json
import agent.tools.agent_tools as tools

# list_user_files 的格式 + 隔离契约见 test_user_isolation_files.py
# 这里只守 document_report 工具的契约

def test_document_report_calls_agent(monkeypatch, tmp_path):
    txt = tmp_path / "note.txt"
    txt.write_text("内容" * 100, encoding="utf-8")
    # 桩 DocumentReportAgent.run
    class _StubAgent:
        def run(self, file_path, question=None):
            return {"markdown": "# 摘要\nstub", "file": file_path}
    monkeypatch.setattr(tools, "_new_document_report_agent", lambda: _StubAgent())
    out = tools.document_report.invoke({"file_path": str(txt), "question": ""})
    assert "stub" in out

def test_document_report_error_is_friendly(monkeypatch):
    """document_report 内部异常时应返回友好错误，而非抛栈。"""
    def _boom():
        raise RuntimeError("boom")
    monkeypatch.setattr(tools, "_new_document_report_agent", _boom)
    out = tools.document_report.invoke({"file_path": "x.pdf", "question": ""})
    assert "报告生成失败" in out
