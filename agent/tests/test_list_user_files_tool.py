import os, sys, json
CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(CURRENT_DIR)
PROJECT_PARENT = os.path.dirname(PROJECT_ROOT)
for p in (PROJECT_ROOT, PROJECT_PARENT):
    if p not in sys.path:
        sys.path.insert(0, p)

import agent.tools.agent_tools as tools

def test_list_user_files_returns_json(monkeypatch):
    monkeypatch.setattr(tools, "_current_user_id", lambda: "u1")
    monkeypatch.setattr(tools, "_list_text_files", lambda u: [{"name": "a.pdf", "status": "已完成"}])
    monkeypatch.setattr(tools, "_list_table_files", lambda u: [{"name": "sales.csv", "table_name": "sales"}])
    out = tools.list_user_files.invoke({})
    data = json.loads(out)
    assert any(f["name"] == "a.pdf" and f["type"] == "text" for f in data)
    assert any(f["name"] == "sales.csv" and f["type"] == "table" for f in data)

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
