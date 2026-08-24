"""ExportAgent 测试。"""
import os

import pytest

from agents.export_agent import ExportAgent


def test_register_cjk_font_returns_name_or_none():
    """_register_cjk_font 应返回 'CJK' 或 None，不抛异常。"""
    agent = ExportAgent()
    name = agent._register_cjk_font()
    assert name is None or name == "CJK"


def test_export_pdf_with_chinese_and_table(tmp_path):
    """PDF 导出含中文+表格的 markdown，应生成非空文件且表格不丢失。"""
    pytest.importorskip("reportlab")
    agent = ExportAgent()
    md = "# 销售报告\n\n## 产品销量\n\n| 产品 | 销量 |\n|---|---|\n| 苹果 | 100 |\n| 香蕉 | 200 |\n"
    path = agent._export_pdf(md, "销售报告")
    assert path is not None
    assert os.path.exists(path)
    assert os.path.getsize(path) > 0
    # 表格数据应被渲染（PDF 二进制无法直接断言内容，但文件应成功生成无异常）


def test_export_markdown(tmp_path):
    agent = ExportAgent()
    path = agent._export_markdown("# 标题\n\n正文内容", "测试报告")
    assert path and os.path.exists(path)
    with open(path, encoding="utf-8") as f:
        assert "正文内容" in f.read()


def test_export_docx(tmp_path):
    pytest.importorskip("docx")
    agent = ExportAgent()
    md = "# 标题\n\n- 项目一\n- 项目二\n\n| A | B |\n|---|---|\n| 1 | 2 |\n"
    path = agent._export_docx(md, "测试报告")
    assert path and os.path.exists(path)
    assert os.path.getsize(path) > 0


def test_export_html(tmp_path):
    agent = ExportAgent()
    md = "# 标题\n\n**加粗**\n"
    path = agent._export_html(md, "测试报告")
    assert path and os.path.exists(path)
    with open(path, encoding="utf-8") as f:
        content = f.read()
    assert "<h1>标题</h1>" in content
    assert "<strong>加粗</strong>" in content


def test_export_unknown_format_returns_none():
    agent = ExportAgent()
    assert agent._export("内容", "标题", "xlsx") is None


def test_docx_hr_not_rendered_as_literal_dashes(tmp_path):
    """分隔线 --- 在 Word 中不应落成字面 "---" 段落（历史漂移修正）。"""
    pytest.importorskip("docx")
    from docx import Document

    agent = ExportAgent()
    md = "# 标题\n\n上节\n\n---\n\n下节"
    path = agent._export_docx(md, "测试报告")
    doc = Document(path)
    texts = [p.text for p in doc.paragraphs]
    assert "---" not in texts


def test_html_unresolvable_image_falls_back_to_placeholder(tmp_path):
    """图片引用解析失败时 HTML 与其他格式一致输出占位文字，而非破图 img 标签（历史漂移修正）。"""
    agent = ExportAgent()
    md = "![销量图](/reports/charts/__no_such_chart__.html)"
    path = agent._export_html(md, "测试报告")
    with open(path, encoding="utf-8") as f:
        content = f.read()
    assert "（图表：销量图）" in content
    assert "__no_such_chart__" not in content
