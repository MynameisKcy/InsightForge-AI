"""
Report export helpers for Streamlit UI.
"""
import datetime
import re


REPORT_TITLE = "# 用户使用情况报告"
REPORT_SECTIONS = (
    "## 基本信息",
    "## 使用概况",
    "## 效率表现",
    "## 发现的问题",
    "## 建议",
    "## 结论",
)


def is_report_content(content: str) -> bool:
    if not content:
        return False

    normalized = content.strip()
    matched_sections = sum(1 for section in REPORT_SECTIONS if section in normalized)
    return REPORT_TITLE in normalized and matched_sections >= 3


def build_report_filename(content: str, created_at: datetime.datetime | None = None) -> str:
    created_at = created_at or datetime.datetime.now()
    user_id = _match_first(content, r"用户\s*ID[:：\s]*([0-9A-Za-z_-]+)") or "unknown"
    month = _match_first(content, r"月份[:：\s]*([0-9]{1,2})") or "unknown"
    timestamp = created_at.strftime("%Y%m%d_%H%M%S")
    return f"user_report_{user_id}_{month}_{timestamp}.md"


def to_markdown_bytes(content: str) -> bytes:
    return content.strip().encode("utf-8")


def _match_first(content: str, pattern: str) -> str | None:
    matched = re.search(pattern, content)
    if not matched:
        return None
    return matched.group(1)
