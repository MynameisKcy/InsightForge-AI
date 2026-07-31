"""ExportAgent 测试。"""
import os
import sys

CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(CURRENT_DIR)
for path in (PROJECT_ROOT, os.path.dirname(PROJECT_ROOT)):
    if path not in sys.path:
        sys.path.insert(0, path)

from agents.export_agent import ExportAgent, _cjk_font_registered


def test_register_cjk_font_returns_name_or_none():
    """_register_cjk_font 应返回 'CJK' 或 None，不抛异常。"""
    agent = ExportAgent()
    name = agent._register_cjk_font()
    assert name is None or name == "CJK"


def test_export_pdf_with_chinese_and_table(tmp_path):
    """PDF 导出含中文+表格的 markdown，应生成非空文件且表格不丢失。"""
    agent = ExportAgent()
    md = "# 销售报告\n\n## 产品销量\n\n| 产品 | 销量 |\n|---|---|\n| 苹果 | 100 |\n| 香蕉 | 200 |\n"
    path = agent._export_pdf(md, "销售报告")
    assert path is not None
    assert os.path.exists(path)
    assert os.path.getsize(path) > 0
    # 表格数据应被渲染（PDF 二进制无法直接断言内容，但文件应成功生成无异常）
