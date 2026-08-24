"""ExportAgent 图表图片嵌入测试。

回归：Word 导出对 HTML 图表路径崩溃；MD/PDF/HTML 导出无图。
根因是图表为 Plotly 交互式 HTML，导出需栅格 PNG。本测试直测 ExportAgent
（不经 FastAPI/鉴权/LLM），覆盖：
- _resolve_chart_image 解析（Web URL / .html 同名 PNG / 缺失 / 占位符）
- Word 嵌入真 PNG 且遇不可解析引用不崩溃
- PDF 含图（体积增长）
- HTML / Markdown 内联 base64 data URI（自包含可离线）
"""
import os
import uuid
import zipfile

import pytest
from PIL import Image

from agents.export_agent import ExportAgent
from utils.path_tool import get_abs_path

# 测试期在 reports/charts 下创建的文件，测后清理
_created_files: list[str] = []


def _make_noise_png(path: str, w: int = 200, h: int = 150) -> None:
    """生成一张带伪噪声的 PNG（不可压缩，保证体积，用于 PDF 体积断言）。"""
    img = Image.new("RGB", (w, h))
    px = img.load()
    for y in range(h):
        for x in range(w):
            px[x, y] = ((x * 7 + y * 13) % 256, (x * 3 + y * 5) % 256, (x * y) % 256)
    img.save(path, "PNG")


def _make_png(name: str | None = None) -> str:
    """在 reports/charts 下创建测试 PNG，返回其 Web URL。"""
    name = name or f"test_{uuid.uuid4().hex[:8]}"
    charts_dir = get_abs_path("reports/charts")
    os.makedirs(charts_dir, exist_ok=True)
    fpath = os.path.join(charts_dir, f"{name}.png")
    _make_noise_png(fpath)
    _created_files.append(fpath)
    return f"/reports/charts/{name}.png"


def _make_html_with_sibling_png(name: str | None = None) -> str:
    """创建 foo.html + foo.png，返回 foo.html 的 Web URL（验证 .html 引用解析到同名 .png）。"""
    name = name or f"test_{uuid.uuid4().hex[:8]}"
    charts_dir = get_abs_path("reports/charts")
    os.makedirs(charts_dir, exist_ok=True)
    html_path = os.path.join(charts_dir, f"{name}.html")
    png_path = os.path.join(charts_dir, f"{name}.png")
    with open(html_path, "w", encoding="utf-8") as f:
        f.write("<html></html>")
    _make_noise_png(png_path)
    _created_files.extend([html_path, png_path])
    return f"/reports/charts/{name}.html"


@pytest.fixture(autouse=True)
def _cleanup():
    yield
    for p in _created_files:
        try:
            os.remove(p)
        except OSError:
            pass
    _created_files.clear()


def _agent():
    return ExportAgent()


# ── _resolve_chart_image 解析逻辑 ──

def test_resolve_web_url_png():
    png = _agent()._resolve_chart_image(_make_png())
    assert png is not None and png.lower().endswith(".png") and os.path.exists(png)


def test_resolve_html_sibling_png():
    png = _agent()._resolve_chart_image(_make_html_with_sibling_png())
    assert png is not None and png.lower().endswith(".png") and os.path.exists(png)


def test_resolve_missing_returns_none():
    assert _agent()._resolve_chart_image("/reports/charts/nope_xyz.png") is None


def test_resolve_placeholder_returns_none():
    a = _agent()
    assert a._resolve_chart_image("[Error: boom]") is None
    assert a._resolve_chart_image("") is None


# ── Word：嵌图 + 不崩溃 ──

def test_docx_with_png_embeds_image():
    url = _make_png()
    md = f"# 报告\n\n![趋势图]({url})\n\n正文。\n"
    path = _agent()._export_docx(md, "测试报告")
    assert path and os.path.exists(path)
    names = zipfile.ZipFile(path).namelist()
    assert any(n.startswith("word/media/") for n in names), f"docx 未嵌入图片媒体: {names}"


def test_docx_unresolvable_image_does_not_crash():
    # 回归：旧实现对 .html 路径 add_picture 抛 UnrecognizedImageError，整条 Word 导出失败
    md = "# 报告\n\n![趋势图](/reports/charts/missing_xyz.html)\n\n正文。\n"
    path = _agent()._export_docx(md, "测试报告")
    assert path and os.path.exists(path)  # 不抛异常即通过


# ── PDF：嵌图（体积增长）──

def test_pdf_with_png_grows():
    url = _make_png()
    a = _agent()
    base = os.path.getsize(a._export_pdf("# 报告\n\n正文。\n", "测试报告"))
    with_img = os.path.getsize(a._export_pdf(f"# 报告\n\n![趋势图]({url})\n", "测试报告"))
    assert with_img > base + 1000, f"PDF 未嵌入图片: base={base} with_img={with_img}"


# ── HTML：base64 自包含 ──

def test_html_with_png_inlines_base64():
    url = _make_png()
    path = _agent()._export_html(f"# 报告\n\n![趋势图]({url})\n", "测试报告")
    content = open(path, encoding="utf-8").read()
    assert "data:image/png;base64" in content


# ── Markdown：data URI 自包含 ──

def test_md_with_png_inlines_data_uri():
    url = _make_png()
    path = _agent()._export_markdown(f"# 报告\n\n![趋势图]({url})\n", "测试报告")
    content = open(path, encoding="utf-8").read()
    assert "data:image/png;base64" in content


def test_md_unresolvable_image_kept_as_is():
    md = "# 报告\n\n![趋势图](/reports/charts/missing_xyz.html)\n"
    path = _agent()._export_markdown(md, "测试报告")
    content = open(path, encoding="utf-8").read()
    # 无法解析的引用原样保留（不丢内容）
    assert "/reports/charts/missing_xyz.html" in content
