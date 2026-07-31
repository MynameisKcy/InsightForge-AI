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
