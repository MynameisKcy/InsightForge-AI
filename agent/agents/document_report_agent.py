"""
DocumentReportAgent：针对文本类文件（PDF/Word/TXT/MD）生成
结构化报告（摘要 + 关键要点 + 可选问答），输出 Markdown。
"""
import os
import sys

CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(CURRENT_DIR)
for path in (PROJECT_ROOT, os.path.dirname(PROJECT_ROOT)):
    if path not in sys.path:
        sys.path.insert(0, path)

from agents.base import BaseAgent

try:
    from agent.utils.file_handler import pdf_loader, docx_loader, text_loader, markdown_loader
    from agent.utils.prompt_loader import load_document_report_prompts
except ModuleNotFoundError:
    from utils.file_handler import pdf_loader, docx_loader, text_loader, markdown_loader
    from utils.prompt_loader import load_document_report_prompts


def _load_text(file_path: str) -> str:
    """按扩展名加载文本，截断到 8000 字避免 prompt 过长。"""
    lower = file_path.lower()
    docs = None
    if lower.endswith(".txt"):
        docs = text_loader(file_path)
    elif lower.endswith(".md"):
        docs = markdown_loader(file_path)
    elif lower.endswith(".pdf"):
        docs = pdf_loader(file_path)
    elif lower.endswith(".docx"):
        docs = docx_loader(file_path)
    if not docs:
        return ""
    return "\n".join(getattr(d, "page_content", str(d)) for d in docs)[:8000]


class DocumentReportAgent(BaseAgent):
    name = "document_report"

    def run(self, file_path: str, question: str | None = None) -> dict:
        content = _load_text(file_path)
        if not content:
            return {"markdown": f"无法解析文件：{file_path}", "file": file_path}
        sys_prompt = load_document_report_prompts()
        user_msg = f"文档内容：\n{content}\n\n"
        if question:
            user_msg += f"用户问题：{question}"
        else:
            user_msg += "未提供具体问题，请按模板输出。"
        md = self._call_llm([
            {"role": "system", "content": sys_prompt},
            {"role": "user", "content": user_msg},
        ])
        return {"markdown": md, "file": file_path}
