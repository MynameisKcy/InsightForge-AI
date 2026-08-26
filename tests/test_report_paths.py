"""reports/ 静态树位置约定契约测试（架构评审 R2 候选5）。

utils/report_paths.py 是 FS↔web↔PNG 位置约定的单一属主：
- fs_to_web_url      —— reports 树内任意文件（serialization/chat_stream 用）
- chart_web_url      —— 仅 charts 子树，图表语义（report_agent 用）
- web_url_to_fs      —— /reports/ 前缀 URL 回 FS（export_agent 用）
- png_sibling_path   —— html→同名 png 兄弟推导（charts 生成侧 + export 消费侧共用）

迁移前这些约定散在 serialization/report_agent/export_agent/charts 四处且口径
已分叉；本测试钉住各自语义，防止再次漂移。
"""
import os

from utils.path_tool import get_abs_path
from utils.report_paths import (
    CHARTS_DIR,
    REPORTS_DIR,
    WEB_CHARTS_PREFIX,
    WEB_REPORTS_PREFIX,
    chart_web_url,
    fs_to_web_url,
    png_sibling_path,
    web_url_to_fs,
)


class TestConstants:
    def test_dir_constants_are_repo_relative_posix(self):
        assert REPORTS_DIR == "reports"
        assert CHARTS_DIR == "reports/charts"

    def test_web_prefixes(self):
        assert WEB_REPORTS_PREFIX == "/reports"
        assert WEB_CHARTS_PREFIX == "/reports/charts"


class TestFsToWebUrl:
    """reports 树内任意 FS 路径 → web URL（原 serialization._to_web_path 契约）。"""

    def test_windows_abs_path_under_charts(self):
        assert fs_to_web_url(r"D:\proj\reports\charts\foo.html") == "/reports/charts/foo.html"

    def test_deeper_subtree(self):
        assert fs_to_web_url(r"C:\x\reports\markdown\r_20260825.md") == "/reports/markdown/r_20260825.md"

    def test_already_web_url_passthrough(self):
        assert fs_to_web_url("/reports/charts/foo.html") == "/reports/charts/foo.html"

    def test_outside_reports_tree_returns_normalized(self):
        # 不在树内的路径原样返回（分隔符已标准化）——不猜测、不改名
        assert fs_to_web_url(r"D:\data\out\foo.html") == "D:/data/out/foo.html"


class TestChartWebUrl:
    """仅 charts 子树的图表语义换算（原 report_agent._chart_web_url 契约）。"""

    def test_fs_chart_path(self):
        assert chart_web_url(r"D:\proj\reports\charts\trend_x.png") == "/reports/charts/trend_x.png"

    def test_already_web_chart_url_passthrough(self):
        assert chart_web_url("/reports/charts/trend_x.png") == "/reports/charts/trend_x.png"

    def test_reports_but_not_charts_is_none(self):
        # 口径分叉的钉子：报告/导出等非 charts 文件不是图表，返回 None
        assert chart_web_url(r"C:\x\reports\markdown\r.md") is None
        assert chart_web_url("/reports/report_a.md") is None

    def test_placeholder_text_is_none(self):
        assert chart_web_url("[Error: kaleido boom]") is None
        assert chart_web_url("[PLACEHOLDER: line - t]") is None

    def test_empty_and_none_are_none(self):
        assert chart_web_url("") is None
        assert chart_web_url(None) is None


class TestWebUrlToFs:
    """/reports/ 前缀 URL → FS 绝对路径（export_agent._resolve_chart_image 契约）。"""

    def test_web_url_maps_under_repo_root(self):
        assert web_url_to_fs("/reports/charts/a.png") == get_abs_path("reports/charts/a.png")

    def test_non_web_ref_passthrough(self):
        # FS 绝对路径 / 占位符文本原样返回——存在性检查留在调用方
        assert web_url_to_fs(r"C:\elsewhere\a.png") == r"C:\elsewhere\a.png"
        assert web_url_to_fs("[Error: boom]") == "[Error: boom]"

    def test_roundtrip_with_fs_to_web_url(self):
        fs = get_abs_path("reports/charts/roundtrip.html")
        assert os.path.normcase(os.path.normpath(web_url_to_fs(fs_to_web_url(fs)))) == \
            os.path.normcase(os.path.normpath(fs))


class TestPngSiblingPath:
    """html→同名 png 兄弟推导（原 charts._chart_png_path 与 export 内联 splitext 的合一）。"""

    def test_html_gets_png_sibling(self):
        assert png_sibling_path(r"D:\x\reports\charts\trend_a.html") == r"D:\x\reports\charts\trend_a.png"

    def test_empty_is_empty(self):
        assert png_sibling_path("") == ""
