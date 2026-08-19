from agents.document_report_agent import DocumentReportAgent, _load_text


class _FakeLLM:
    def _call_llm(self, messages):
        return "# 摘要\n本文档讲X。\n\n## 关键要点\n- 点1\n- 点2\n\n## 问答\n问:A\n答:B"

def test_run_returns_markdown_with_sections(tmp_path):
    txt = tmp_path / "note.txt"
    txt.write_text("示例内容" * 200, encoding="utf-8")
    agent = DocumentReportAgent()
    agent._call_llm = _FakeLLM()._call_llm  # mock LLM
    out = agent.run(str(txt), question="要点是什么？")
    assert "摘要" in out["markdown"]
    assert "关键要点" in out["markdown"]
    assert "问答" in out["markdown"]
    assert out["file"] == str(txt)

def test_run_without_question(tmp_path):
    txt = tmp_path / "note.txt"
    txt.write_text("内容" * 100, encoding="utf-8")
    agent = DocumentReportAgent()
    agent._call_llM = _FakeLLM()._call_llm
    agent._call_llm = _FakeLLM()._call_llm
    out = agent.run(str(txt))
    assert "摘要" in out["markdown"]

def test_unsupported_file_returns_error(tmp_path):
    bad = tmp_path / "data.xyz"
    bad.write_text("nope", encoding="utf-8")
    agent = DocumentReportAgent()
    agent._call_llm = _FakeLLM()._call_llm
    out = agent.run(str(bad))
    assert "无法解析" in out["markdown"]

def test_load_text_truncates(tmp_path):
    txt = tmp_path / "big.txt"
    txt.write_text("A" * 20000, encoding="utf-8")
    text = _load_text(str(txt))
    assert len(text) <= 8000
