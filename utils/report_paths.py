"""reports/ 静态树位置约定的单一属主（架构评审 R2 候选5）。

图表/报告文件在三个表示之间流转：FS 绝对路径（生成侧落盘）、web URL
（`/reports/...`，fastapi_server 静态挂载，进 SSE [CHART] 帧与报告 markdown）、
PNG 兄弟文件（kaleido 在 html 旁产出的栅格图，导出嵌入用）。此前换算散在
serialization / report_agent / export_agent / charts 四处且口径分叉
（serialization 认整个 reports 树、report_agent 只认 charts 子树），本模块
按语义显式命名收敛为一份：

- fs_to_web_url      reports 树内任意 FS 路径 → web URL（serialization/chat_stream）
- chart_web_url      仅 charts 子树；非图表产物返回 None（report_agent 图表引用）
- web_url_to_fs      `/reports/` 前缀 URL → FS 绝对路径（export_agent 解析图片引用）
- png_sibling_path   html → 同名 png 兄弟推导（charts 生成侧 + export 消费侧）

约定：目录常量一律 repo 相对 POSIX 形式，经 `get_abs_path` 落地；
web 前缀与 fastapi_server 的 `app.mount("/reports", ...)` 锁步。
"""
import os

from utils.path_tool import get_abs_path

REPORTS_DIR = "reports"
CHARTS_SUBDIR = "charts"
CHARTS_DIR = f"{REPORTS_DIR}/{CHARTS_SUBDIR}"

WEB_REPORTS_PREFIX = "/reports"
WEB_CHARTS_PREFIX = f"{WEB_REPORTS_PREFIX}/{CHARTS_SUBDIR}"


def fs_to_web_url(path: str) -> str:
    """reports 树内任意 FS 路径 → web URL；树外路径标准化分隔符后原样返回。

    取路径中首个 `/reports/` 段及其后缀（Windows 反斜杠先标准化）。
    """
    normalized = (path or "").replace("\\", "/")
    idx = normalized.find(WEB_REPORTS_PREFIX + "/")
    if idx >= 0:
        return normalized[idx:]
    return normalized


def chart_web_url(path: str | None) -> str | None:
    """图表产物的 web URL；仅认 charts 子树——占位符文本/报告等非图表产物返回 None。"""
    if not path or not isinstance(path, str):
        return None
    normalized = path.replace("\\", "/")
    prefix = WEB_CHARTS_PREFIX + "/"
    if normalized.startswith(prefix):
        return normalized
    idx = normalized.find(prefix)
    if idx >= 0:
        return normalized[idx:]
    return None


def web_url_to_fs(url: str) -> str:
    """`/reports/...` web URL → FS 绝对路径；其余输入原样返回（存在性检查归调用方）。"""
    if url and url.startswith(WEB_REPORTS_PREFIX + "/"):
        return get_abs_path(url.lstrip("/"))
    return url


def png_sibling_path(html_path: str) -> str:
    """html 路径 → 同名 .png 兄弟路径（仅字符串推导，不保证存在）。"""
    if not html_path:
        return ""
    return os.path.splitext(html_path)[0] + ".png"
