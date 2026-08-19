"""
Chart Generator: 使用 Plotly 生成交互式图表，支持多种图表类型。
"""

import inspect
import os
import re
from datetime import datetime

import pandas as pd

from utils.logger_handler import logger
from utils.path_tool import get_abs_path

# 延迟导入 Plotly，处理未安装情况
_plotly_available = True
try:
    import plotly.express as px
    import plotly.graph_objects as go  # noqa: F401  # 可用性探测导入
    import plotly.io as pio  # noqa: F401  # 可用性探测导入
except ImportError:
    _plotly_available = False
    logger.warning("Plotly not installed. Charts will be generated as placeholder text.")


CHART_OUTPUT_DIR = "reports/charts"

# 静态 PNG 导出参数（供报告 Word/PDF/MD/HTML 嵌入栅格图；kaleido 渲染）
# 宽度略大于交互图高度配套，scale=2 提升导出清晰度
CHART_PNG_WIDTH = 900
CHART_PNG_HEIGHT = 500
CHART_PNG_SCALE = 2

# 延迟导入 kaleido（Plotly 静态图导出引擎，图表 -> PNG 供报告嵌入）。
# 重要：不能用 Plotly 的 fig.write_image() —— 它每次调用都新建 kaleido scope，
# 同进程第 2 次调用会挂起（被 watchdog 强杀）。改用持久 sync server 复用单一
# chromium scope。server 一旦启动即随进程生命周期常驻、不再 stop：kaleido 的
# stop_sync_server 在解释器退出时触发 GIL 致命错误，故常驻更稳，且后续图表复用
# 热 scope（首张约 3s 冷启，其后约 0.1s/张）。
_kaleido_available = True
try:
    import kaleido
except ImportError:
    _kaleido_available = False
    logger.warning("kaleido not installed. Chart PNG export disabled (reports will lack embedded charts).")

_png_server_started = False


def _ensure_png_server() -> None:
    """启动 kaleido sync server（幂等）。保持单个 chromium scope 热复用，
    避免 fig.write_image 逐次新建 scope 在第 2 次调用挂起。"""
    global _png_server_started
    if not _kaleido_available or _png_server_started:
        return
    try:
        kaleido.start_sync_server(silence_warnings=True)
        _png_server_started = True
    except Exception as e:
        logger.warning(f"kaleido sync server start failed: {e}")


def start_png_batch() -> None:
    """显式启动 PNG 批次（幂等）。VisualizationAgent.run() 图表循环前调用，
    使首批图表共享热 scope（首张约 3s 冷启，其后约 0.1s/张）。"""
    _ensure_png_server()


def stop_png_batch() -> None:
    """No-op：不主动 stop kaleido server。

    stop_sync_server 在解释器退出时触发 GIL 致命错误，故 server 随进程常驻、
    由 OS 在进程结束时回收 chromium。调用安全但无效果，保留以稳定接口。
    """
    return

# Cobalt 系配色，与 app sci-tech 主题一致：主色 cobalt，辅以 cyan/violet/green/amber/rose
PALETTE = ["#3b82f6", "#22d3ee", "#a78bfa", "#34d399", "#fbbf24", "#fb7185"]


def _ensure_output_dir() -> str:
    """确保图表输出目录存在。"""
    output_path = get_abs_path(CHART_OUTPUT_DIR)
    os.makedirs(output_path, exist_ok=True)
    return output_path


def _filter_kwargs(func, kwargs: dict) -> dict:
    """按目标函数签名过滤 kwargs，只保留它接受的参数。

    auto_chart 会向 normalized 注入 names_col/values_col 等列键做兜底推断，
    但不同图表签名不同（bar_chart 只认 x_col/y_col，pie_chart 只认
    names_col/values_col，heatmap 只认 title）。不过滤直接 splat 会导致
    TypeError: got an unexpected keyword argument。若 func 含 **kwargs
    （pie/heatmap），则全部保留。
    """
    try:
        sig = inspect.signature(func)
    except (TypeError, ValueError):
        return dict(kwargs)
    params = sig.parameters
    if any(p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values()):
        return dict(kwargs)
    return {k: v for k, v in kwargs.items() if k in params}


class ChartGenerator:
    """图表生成器：根据数据类型自动选择图表类型并生成 Plotly 图表。"""

    @staticmethod
    def _is_month_like(df: pd.DataFrame, col: str) -> bool:
        """检测列是否为月类型（整数 1-12 或 年+月 格式，用于格式化 x 轴标签）。"""
        if col not in df.columns:
            return False
        vals = df[col].dropna()
        if len(vals) == 0:
            return False
        # 检查是否全为整数且在 1-12 范围内（月份）或在一个较小整数范围内
        try:
            numeric = pd.to_numeric(vals, errors="coerce")
            if numeric.isna().any():
                return False
            # 全部是整数
            if not (numeric == numeric.astype(int)).all():
                return False
            unique_count = numeric.nunique()
            # 常见月份范围：1-12, 或连续的月份编号
            if unique_count >= 3 and unique_count <= 100:
                return True
        except Exception:
            pass
        return False

    @staticmethod
    def _series_stats(series):
        """数值列统计：min/max/max_abs/has_neg/is_int_like。非数值或空返回 None。"""
        s = pd.to_numeric(series, errors="coerce").dropna()
        if s.empty:
            return None
        vmin, vmax = float(s.min()), float(s.max())
        return {
            "min": vmin, "max": vmax,
            "max_abs": float(max(abs(vmin), abs(vmax))),
            "has_neg": vmin < 0,
            "is_int_like": bool((s == s.astype(int)).all()),
        }

    @staticmethod
    def _tick_format(max_abs):
        """按量级选 Plotly 刻度格式：>=1000 用 SI(k/M/G)，否则千分位/小数。"""
        if max_abs >= 1000:
            return ".2~s"   # 1.2M / 12k / 3.4G
        if max_abs >= 1:
            return ",.0f"   # 1,234
        return ",.2f"

    @staticmethod
    def _y_range(series, start_zero=False):
        """y 轴范围，带 5% padding；start_zero（柱状图、全非负）时下限为 0。"""
        st = ChartGenerator._series_stats(series)
        if st is None:
            return None
        lo, hi = st["min"], st["max"]
        if start_zero and not st["has_neg"]:
            lo = 0
        if lo == hi:
            return [lo - 1, hi + 1]
        pad = (hi - lo) * 0.05
        return [lo - pad, hi + pad]

    @staticmethod
    def _style_numeric_axis(fig, axis, series, start_zero=False):
        """给数值轴设置刻度格式与范围。axis: 'x' 或 'y'。"""
        st = ChartGenerator._series_stats(series)
        if st is None:
            return
        kw = {"tickformat": ChartGenerator._tick_format(st["max_abs"])}
        rng = ChartGenerator._y_range(series, start_zero=start_zero)
        if rng is not None:
            kw["range"] = rng
        if axis == "y":
            fig.update_yaxes(**kw)
        else:
            fig.update_xaxes(**kw)

    @staticmethod
    def _x_should_be_category(df, col):
        """x 列是否应作为分类轴（月型 / 非数值 / 低基数数值），避免科学计数法与乱序。"""
        if col not in df.columns:
            return False
        if ChartGenerator._is_month_like(df, col):
            return True
        s = df[col]
        if not pd.api.types.is_numeric_dtype(s):
            return True
        return 0 < s.nunique(dropna=True) <= 24

    @staticmethod
    def line_chart(df: pd.DataFrame, x_col: str, y_col: str, title: str = "趋势图",
                   x_label: str = "", y_label: str = "") -> str:
        """生成时间序列折线图（趋势图）。返回保存路径。"""
        if not _plotly_available:
            return _placeholder("line_chart", title)

        # 处理月份类型 x 轴：转为字符串避免科学计数法显示
        work_df = df.copy()
        # 剔除 y_col 缺失行，避免空点/断线
        work_df = work_df.dropna(subset=[y_col])
        if work_df.empty:
            return _empty_data_placeholder("line_chart", y_col)
        month_like = ChartGenerator._is_month_like(work_df, x_col)
        use_x = "_x_display" if month_like else x_col
        if month_like:
            work_df["_x_display"] = work_df[x_col].astype(int).astype(str)
        fig = px.line(work_df, x=use_x, y=y_col, title=title, markers=True,
                      labels={use_x: x_label or x_col, y_col: y_label or y_col})
        fig.update_traces(line_color=PALETTE[0])
        fig.update_layout(template="plotly_white", hovermode="x unified",
                          height=500, colorway=PALETTE)
        # 分类轴（月型/低基数/文本）避免科学计数法与乱序
        if ChartGenerator._x_should_be_category(work_df, x_col):
            fig.update_xaxes(type="category")
        # y 轴：按数据量级格式化刻度 + 带留白的范围
        ChartGenerator._style_numeric_axis(fig, "y", work_df[y_col], start_zero=False)
        return _save_chart(fig, f"trend_{_safe_name(title)}")

    @staticmethod
    def bar_chart(df: pd.DataFrame, x_col: str, y_col: str, title: str = "柱状图",
                  top_n: int = 10, horizontal: bool = False,
                  x_label: str = "", y_label: str = "") -> str:
        """生成柱状图（TOP 排名分析）。返回保存路径。"""
        if not _plotly_available:
            return _placeholder("bar_chart", title)

        # 剔除 y_col 缺失行，避免画出高度为 0 的空柱
        df = df.dropna(subset=[y_col])
        if df.empty:
            return _empty_data_placeholder("bar_chart", y_col)
        # 剔除汇总行（如「全省」混入各市排名），避免汇总占大头挤扁其余柱
        df = ChartGenerator._drop_summary_rows(df, cat_col=x_col, values_col=y_col)
        if df.empty:
            return _empty_data_placeholder("bar_chart", y_col)
        data = df.head(top_n) if len(df) > top_n else df

        # 处理月份类型 x 轴：转为字符串避免科学计数法显示
        month_like = (not horizontal) and ChartGenerator._is_month_like(data, x_col)
        if month_like:
            data = data.copy()
            data["_x_display"] = data[x_col].astype(int).astype(str)
            use_x = "_x_display"
        else:
            use_x = x_col

        labels = {y_col: y_label or y_col}
        if horizontal:
            labels[x_col] = x_label or x_col
        else:
            labels[use_x] = x_label or x_col
        if horizontal:
            fig = px.bar(data, y=x_col, x=y_col, title=title, orientation="h",
                         text=y_col, labels=labels)
        else:
            fig = px.bar(data, x=use_x, y=y_col, title=title, text=y_col, labels=labels)
        fig.update_traces(texttemplate="%{text:.2s}", textposition="outside",
                          marker_color=PALETTE[0])
        fig.update_layout(template="plotly_white", height=500, colorway=PALETTE)
        if not horizontal:
            if month_like or ChartGenerator._x_should_be_category(data, x_col):
                fig.update_xaxes(type="category")
            # 柱状图 y 轴从 0 起（全非负时），刻度按量级格式化
            ChartGenerator._style_numeric_axis(fig, "y", data[y_col], start_zero=True)
        else:
            ChartGenerator._style_numeric_axis(fig, "x", data[y_col], start_zero=True)
        return _save_chart(fig, f"bar_{_safe_name(title)}")

    @staticmethod
    def _drop_summary_rows(df: pd.DataFrame, cat_col: str, values_col: str) -> pd.DataFrame:
        """剔除汇总行，避免对比图把汇总当一个类别（如全省/总计混入各市饼图）。

        两条判据（命中任一即剔）：
        1. 类别名命中汇总关键词：全省/总计/合计/汇总/全部/小计/共计/总和
        2. 某行数值 ≈ 其余正数值之和（误差 2%，且该行是最大值）——「全省=各市之和」模式。

        仅在类别列存在时生效；无类别列或全无匹配则原样返回（不误伤正常数据）。
        """
        if df is None or df.empty or cat_col not in df.columns:
            return df if df is not None else pd.DataFrame()
        SUMMARY_KEYWORDS = re.compile(r"(?:全省|总计|合计|汇总|全部|小计|共计|总和)")
        # 判据1：关键词命中
        cat_str = df[cat_col].astype(str)
        kw_mask = cat_str.str.contains(SUMMARY_KEYWORDS, na=False)
        if kw_mask.any():
            return df[~kw_mask].reset_index(drop=True)
        # 判据2：数值≈其余正数之和（汇总行特征）
        if values_col in df.columns:
            vals = df[values_col]
            numeric_vals = pd.to_numeric(vals, errors="coerce")
            positive = numeric_vals[numeric_vals > 0]
            if len(positive) >= 3:
                total = positive.sum()
                # 找最大值行，若它 ≈ (总和 - 它自己) 则视为汇总行
                max_idx = positive.idxmax()
                max_val = positive.loc[max_idx]
                rest_sum = total - max_val
                if rest_sum > 0 and abs(max_val - rest_sum) / rest_sum <= 0.02:
                    return df.drop(index=max_idx).reset_index(drop=True)
        return df.reset_index(drop=True)

    @staticmethod
    def pie_chart(df: pd.DataFrame, names_col: str, values_col: str,
                  title: str = "占比图", **kwargs) -> str:
        """生成饼图（类别占比分析）。返回保存路径。"""
        if not _plotly_available:
            return _placeholder("pie_chart", title)

        # 剔除 values_col 缺失行，避免占比失真
        df = df.dropna(subset=[values_col])
        if df.empty:
            return _empty_data_placeholder("pie_chart", values_col)
        # 剔除汇总行（如「全省」混入各市占比），避免汇总占大头挤扁其余 slice
        df = ChartGenerator._drop_summary_rows(df, cat_col=names_col, values_col=values_col)
        if df.empty:
            return _empty_data_placeholder("pie_chart", values_col)
        fig = px.pie(df, names=names_col, values=values_col, title=title,
                     color_discrete_sequence=PALETTE)
        fig.update_traces(textposition="inside", textinfo="percent+label")
        fig.update_layout(template="plotly_white", height=500)
        return _save_chart(fig, f"pie_{_safe_name(title)}")

    @staticmethod
    def heatmap(df: pd.DataFrame, title: str = "热力图", **kwargs) -> str:
        """生成热力图（区域-月份矩阵）。需要 pivot 格式数据。"""
        if not _plotly_available:
            return _placeholder("heatmap", title)

        n_rows = len(df.index) if hasattr(df, "index") else 1
        n_cols = len(df.columns) if hasattr(df, "columns") else 1
        height = max(400, min(700, 60 * max(n_rows, n_cols)))
        fig = px.imshow(
            df.values if hasattr(df, "values") else df,
            x=df.columns.tolist(),
            y=df.index.tolist(),
            title=title,
            color_continuous_scale="RdBu_r",
            aspect="auto",
        )
        fig.update_layout(template="plotly_white", height=height)
        return _save_chart(fig, f"heatmap_{_safe_name(title)}")

    @staticmethod
    def scatter_chart(df: pd.DataFrame, x_col: str, y_col: str,
                      color_col: str | None = None, title: str = "散点图",
                      x_label: str = "", y_label: str = "") -> str:
        """生成散点图（利润关系分析）。"""
        if not _plotly_available:
            return _placeholder("scatter", title)

        # 剔除 x_col/y_col 缺失行，避免空点
        df = df.dropna(subset=[x_col, y_col])
        if df.empty:
            return _empty_data_placeholder("scatter_chart", f"{x_col}/{y_col}")
        labels = {x_col: x_label or x_col, y_col: y_label or y_col}
        if color_col and color_col in df.columns:
            fig = px.scatter(df, x=x_col, y=y_col, color=color_col, title=title,
                             hover_data=df.columns.tolist(), labels=labels)
        else:
            fig = px.scatter(df, x=x_col, y=y_col, title=title,
                             hover_data=df.columns.tolist(), labels=labels)
        fig.update_layout(template="plotly_white", height=500, colorway=PALETTE)
        ChartGenerator._style_numeric_axis(fig, "x", df[x_col], start_zero=False)
        ChartGenerator._style_numeric_axis(fig, "y", df[y_col], start_zero=False)
        return _save_chart(fig, f"scatter_{_safe_name(title)}")

    @staticmethod
    def auto_chart(df: pd.DataFrame, chart_type: str, **kwargs) -> str:
        """根据 chart_type 字符串自动选择图表类型。"""
        chart_type = chart_type.lower().strip()
        mapper = {
            "line": ChartGenerator.line_chart,
            "trend": ChartGenerator.line_chart,
            "折线图": ChartGenerator.line_chart,
            "趋势图": ChartGenerator.line_chart,
            "bar": ChartGenerator.bar_chart,
            "柱状图": ChartGenerator.bar_chart,
            "top": ChartGenerator.bar_chart,
            "pie": ChartGenerator.pie_chart,
            "饼图": ChartGenerator.pie_chart,
            "heatmap": ChartGenerator.heatmap,
            "热力图": ChartGenerator.heatmap,
            "scatter": ChartGenerator.scatter_chart,
            "散点图": ChartGenerator.scatter_chart,
        }
        func = mapper.get(chart_type, ChartGenerator.bar_chart)

        # 规范化参数名: 接受 x/x_col, y/y_col, names/names_col, values/values_col
        normalized = {}
        for k, v in kwargs.items():
            if k == "x":
                normalized["x_col"] = v
            elif k == "y":
                normalized["y_col"] = v
            elif k == "names":
                normalized["names_col"] = v
            elif k == "values":
                normalized["values_col"] = v
            else:
                normalized[k] = v

        # 推断缺失的列
        numeric_cols = df.select_dtypes(include=["number"]).columns.tolist()
        if "x_col" not in normalized and len(df.columns) >= 2:
            normalized["x_col"] = df.columns[0]
        if "y_col" not in normalized and len(numeric_cols) >= 1:
            normalized["y_col"] = numeric_cols[-1] if len(numeric_cols) >= 1 else df.columns[-1]
        if "names_col" not in normalized and len(df.columns) >= 2:
            normalized["names_col"] = df.columns[0]
        if "values_col" not in normalized and len(numeric_cols) >= 1:
            normalized["values_col"] = numeric_cols[-1] if len(numeric_cols) >= 1 else df.columns[-1]

        return func(df, **_filter_kwargs(func, normalized))

    @staticmethod
    def detect_chart_type(data_description: str) -> str:
        """根据数据描述自动判断最佳图表类型。"""
        desc = data_description.lower()
        if any(w in desc for w in ["趋势", "时间", "月度", "每月", "趋势", "trend", "timeline", "growth"]):
            return "line"
        if any(w in desc for w in ["top", "排名", "排行", "ranking", "best", "worst"]):
            return "bar"
        if any(w in desc for w in ["占比", "比例", "分布", "percentage", "share", "ratio"]):
            return "pie"
        if any(w in desc for w in ["热力", "矩阵", "区域", "heatmap", "matrix", "correlation"]):
            return "heatmap"
        if any(w in desc for w in ["关系", "相关", "散点", "scatter", "correlation"]):
            return "scatter"
        return "bar"


def _save_chart(fig, base_name: str) -> str:
    """保存图表为 HTML 文件，返回文件路径。

    同时 best-effort 写一份同名 PNG（供报告导出嵌入栅格图，kaleido 渲染）。
    kaleido 缺失/失败时仅写 HTML，不影响交互式图表与主流程。
    """
    output_dir = _ensure_output_dir()
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"{base_name}_{timestamp}.html"
    filepath = os.path.join(output_dir, filename)
    try:
        fig.write_html(filepath)
        logger.info(f"Chart saved: {filepath}")
    except Exception as e:
        logger.error(f"Failed to save chart: {e}")
        return f"[Chart generation failed: {e}]"

    # best-effort 写 PNG（导出报告用）。失败仅告警，不阻断。
    # 用 sync server 复用 scope，避免 fig.write_image 第 2 次挂起。
    if _kaleido_available:
        try:
            _ensure_png_server()
            png_path = _chart_png_path(filepath)
            opts = {"width": CHART_PNG_WIDTH, "height": CHART_PNG_HEIGHT,
                    "scale": CHART_PNG_SCALE, "format": "png"}
            kaleido.write_fig_sync(fig, png_path, opts)
            logger.info(f"Chart PNG saved: {png_path}")
        except Exception as e:
            # kaleido 渲染失败：导出层优雅降级为文字占位，不崩 Word
            logger.warning(f"Chart PNG export skipped (kaleido?): {e}")
    return filepath


def _chart_png_path(html_path: str) -> str:
    """由 HTML 图表路径推导同名 PNG 路径（仅推导字符串，不保证存在）。"""
    if not html_path:
        return ""
    return os.path.splitext(html_path)[0] + ".png"


def chart_png_path(html_path: str) -> str | None:
    """返回 HTML 图表对应的 PNG 文件路径；PNG 不存在则 None。

    用于报告导出层判断是否可嵌入栅格图。占位符文本（非 .html 路径）直接返回 None。
    """
    if not html_path or not html_path.lower().endswith(".html"):
        return None
    png = _chart_png_path(html_path)
    return png if os.path.exists(png) else None


def _safe_name(name: str) -> str:
    """将标题转换为安全的文件名。"""
    import re
    name = re.sub(r"[^\w\s-]", "", name)
    return re.sub(r"[-\s]+", "_", name.strip().lower())[:50]


def _placeholder(chart_type: str, title: str) -> str:
    """当 Plotly 不可用时返回占位文本。"""
    return f"[PLACEHOLDER: {chart_type} - {title}. Install plotly to generate charts.]"


def _empty_data_placeholder(chart_type: str, col: str) -> str:
    """dropna 后无有效数据时返回占位文本，不抛异常。"""
    return f"[{chart_type}: 列 {col!r} 无有效数值数据（全部缺失）]"
