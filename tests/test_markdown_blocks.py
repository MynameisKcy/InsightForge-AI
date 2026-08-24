"""markdown_blocks 纯解析器测试：报告 Markdown 方言 → Block 序列。

方言规则与 ExportAgent 三个渲染器的历史行为对齐（见 agents/markdown_blocks.py 模块注释），
本文件是解析 interface 的唯一行为钉子——渲染器重构不应改动这里。
"""
from agents.markdown_blocks import (
    Blank,
    Heading,
    HorizontalRule,
    Image,
    ListItem,
    Paragraph,
    Table,
    parse_markdown_blocks,
)


def test_headings_level_1_to_3():
    md = "# 一级\n## 二级\n### 三级"
    assert parse_markdown_blocks(md) == [
        Heading(level=1, text="一级"),
        Heading(level=2, text="二级"),
        Heading(level=3, text="三级"),
    ]


def test_four_hashes_is_paragraph_not_heading():
    """#### 及更深不视为标题（历史行为：落入正文段落）。"""
    assert parse_markdown_blocks("#### 四级") == [Paragraph(text="#### 四级")]


def test_hash_without_space_is_paragraph():
    assert parse_markdown_blocks("#没有空格") == [Paragraph(text="#没有空格")]


def test_paragraphs_and_blank_lines_preserved():
    """连续空行逐行保留为 Blank（历史行为：每个空行各产出一个元素）。"""
    md = "第一段\n\n\n第二段"
    assert parse_markdown_blocks(md) == [
        Paragraph(text="第一段"),
        Blank(),
        Blank(),
        Paragraph(text="第二段"),
    ]


def test_inline_marks_kept_raw_in_text():
    """行内强调标记原样保留——转换是各渲染器自己的事。"""
    assert parse_markdown_blocks("**粗** *斜* `码`") == [
        Paragraph(text="**粗** *斜* `码`")
    ]


def test_bullet_and_ordered_list_items():
    md = "- 项目一\n12. 项目二"
    blocks = parse_markdown_blocks(md)
    assert blocks == [
        ListItem(ordered=False, marker="-", text="项目一"),
        ListItem(ordered=True, marker="12.", text="项目二"),
    ]


def test_horizontal_rule_exact_match_only():
    """仅整行 '---' 是分隔线；带后缀的不是。"""
    assert parse_markdown_blocks("---") == [HorizontalRule()]
    assert parse_markdown_blocks("---备注") == [Paragraph(text="---备注")]


def test_image_reference_parsed():
    md = "![销量图](/reports/charts/sales.html)"
    assert parse_markdown_blocks(md) == [
        Image(alt="销量图", ref="/reports/charts/sales.html")
    ]


def test_unclosed_image_falls_back_to_paragraph():
    assert parse_markdown_blocks("![残缺") == [Paragraph(text="![残缺")]


def test_table_with_separator_row():
    md = "| 产品 | 销量 |\n|---|---|\n| 苹果 | 100 |\n| 香蕉 | 200 |"
    assert parse_markdown_blocks(md) == [
        Table(
            header=["产品", "销量"],
            rows=[["苹果", "100"], ["香蕉", "200"]],
            has_separator=True,
        )
    ]


def test_table_without_separator_first_row_is_header():
    md = "| A | B |\n| 1 | 2 |"
    assert parse_markdown_blocks(md) == [
        Table(header=["A", "B"], rows=[["1", "2"]], has_separator=False)
    ]


def test_table_accumulation_stops_at_non_pipe_line():
    md = "| A | B |\n|---|---|\n| 1 | 2 |\n后续段落"
    assert parse_markdown_blocks(md) == [
        Table(header=["A", "B"], rows=[["1", "2"]], has_separator=True),
        Paragraph(text="后续段落"),
    ]


def test_table_separator_requires_all_cells_loose_dashes():
    """分隔行判定为逐格含 '--'；某格不含的不算分隔行。"""
    md = "| A | B |\n| --- | x |\n| 1 | 2 |"
    assert parse_markdown_blocks(md) == [
        Table(header=["A", "B"], rows=[["---", "x"], ["1", "2"]], has_separator=False)
    ]


def test_empty_cells_dropped_historic_semantics():
    """按历史语义，切分后为空的格子被丢弃（列会左移）——显式钉住。"""
    tables = [b for b in parse_markdown_blocks("| A |  |\n|---|---|\n| 1 |  |") if isinstance(b, Table)]
    assert tables[0].header == ["A"]


def test_mixed_document_ordering():
    md = (
        "# 报告\n"
        "\n"
        "概述 **重点**。\n"
        "\n"
        "| A | B |\n"
        "|---|---|\n"
        "| 1 | 2 |\n"
        "\n"
        "![图](/reports/charts/c.png)\n"
        "---\n"
        "- 结论一"
    )
    assert parse_markdown_blocks(md) == [
        Heading(level=1, text="报告"),
        Blank(),
        Paragraph(text="概述 **重点**。"),
        Blank(),
        Table(header=["A", "B"], rows=[["1", "2"]], has_separator=True),
        Blank(),
        Image(alt="图", ref="/reports/charts/c.png"),
        HorizontalRule(),
        ListItem(ordered=False, marker="-", text="结论一"),
    ]
