"""报告 Markdown 方言的块解析器——ExportAgent 四格式导出的唯一解析实现。

方言（与 ReportAgent 产出对齐）：ATX 标题（#..###）、竖线表格（可选 --- 分隔行）、
图表图片引用 ![alt](ref)、- / N. 列表项、整行 --- 分隔线、行内 **粗**/*斜*/`码` 标记。
纯函数、零文件系统访问：Image 只携带 alt/ref，图表 PNG 解析留在渲染侧（ExportAgent）。

分类规则（按序匹配，行尾 rstrip 后判定，保留历史行为——行首空白会使标题/图片/列表失配）：
1. 空行 → Blank
2. 竖线开头（strip 后）→ Table（连续竖线行累积；分隔行须逐格含 "--"）
3. #{1,3}+空格 → Heading；#### 或无空格 # 落入正文
4. 整行 "---" → HorizontalRule
5. "!(" 开头且完整图片语法 → Image；残缺回退正文
6. "- " → 无序列表项；"N. " → 有序列表项
7. 其余 → Paragraph（行内标记原样保留，转换归各渲染器）

历史上三个渲染器各带一份遍历器且规则有漂移，统一后的行为差异（有意修正）：
- Word 对 "---" 渲染分隔线而非字面输出横杠段落
- 表格分隔行判定统一为逐格含 "--"（原 Word/PDF 为整行子串匹配）
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_HEADING_RE = re.compile(r"(#{1,3})\s+(.*)")
_IMAGE_RE = re.compile(r"!\[(.*?)\]\((.*?)\)")
_ORDERED_RE = re.compile(r"^(\d+)\.\s")


@dataclass(frozen=True)
class Blank:
    """空行。各格式自行决定留白方式（空段落 / Spacer / 空串）。"""


@dataclass(frozen=True)
class Heading:
    level: int  # 1..3
    text: str


@dataclass(frozen=True)
class Paragraph:
    text: str  # 原始行文本，行内标记未转换


@dataclass(frozen=True)
class ListItem:
    ordered: bool
    marker: str  # "-" 或 "N."
    text: str


@dataclass(frozen=True)
class HorizontalRule:
    pass


@dataclass(frozen=True)
class Image:
    alt: str
    ref: str  # web URL / FS 路径 / 占位符，解析归渲染侧


@dataclass(frozen=True)
class Table:
    header: list[str]
    rows: list[list[str]]
    has_separator: bool


def _split_cells(line: str) -> list[str]:
    """按竖线切分单元格。沿用历史语义：切分后为空的格子丢弃。"""
    return [c.strip() for c in line.split("|") if c.strip()]


def parse_markdown_blocks(markdown: str) -> list[Blank | Heading | ListItem | HorizontalRule | Image | Paragraph | Table]:
    blocks: list = []
    lines = markdown.split("\n")
    i = 0
    while i < len(lines):
        raw = lines[i]
        line = raw.rstrip()

        if not line.strip():
            blocks.append(Blank())
            i += 1
            continue

        # 表格：连续竖线行一次吞掉
        if line.strip().startswith("|"):
            table_lines = []
            while i < len(lines) and lines[i].strip().startswith("|"):
                table_lines.append(lines[i].strip())
                i += 1
            header = _split_cells(table_lines[0]) if table_lines else []
            has_sep = len(table_lines) > 1 and bool(table_lines[1]) and all(
                "--" in c for c in _split_cells(table_lines[1])
            )
            data_start = 2 if has_sep else 1
            rows = []
            for tl in table_lines[data_start:]:
                cells = _split_cells(tl)
                if cells:
                    rows.append(cells)
            blocks.append(Table(header=header, rows=rows, has_separator=has_sep))
            continue

        m = _HEADING_RE.match(line)
        if m:
            blocks.append(Heading(level=len(m.group(1)), text=m.group(2)))
            i += 1
            continue

        if line == "---":
            blocks.append(HorizontalRule())
            i += 1
            continue

        if line.startswith("!["):
            m = _IMAGE_RE.match(line.strip())
            if m:
                blocks.append(Image(alt=m.group(1), ref=m.group(2)))
                i += 1
                continue

        if line.startswith("- "):
            blocks.append(ListItem(ordered=False, marker="-", text=line[2:]))
            i += 1
            continue

        m = _ORDERED_RE.match(line)
        if m:
            blocks.append(ListItem(ordered=True, marker=m.group(1) + ".", text=line[m.end():]))
            i += 1
            continue

        blocks.append(Paragraph(text=line))
        i += 1

    return blocks
