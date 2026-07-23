"""
Chart Generator: 使用 Plotly 生成交互式图表，支持多种图表类型。
"""

import os
import sys
from datetime import datetime
from typing import Any

import pandas as pd

CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(CURRENT_DIR)
for path in (PROJECT_ROOT, os.path.dirname(PROJECT_ROOT)):
    if path not in sys.path:
        sys.path.insert(0, path)

from utils.logger_handler import logger
from utils.path_tool import get_abs_path

# 延迟导入 Plotly，处理未安装情况
_plotly_available = True
try:
    import plotly.graph_objects as go
    import plotly.express as px
    import plotly.io as pio
except ImportError:
    _plotly_available = False
    logger.warning("Plotly not installed. Charts will be generated as placeholder text.")


CHART_OUTPUT_DIR = "reports/charts"

# Cobalt 系配色，与 app sci-tech 主题一致：主色 cobalt，辅以 cyan/violet/green/amber/rose
PALETTE = ["#3b82f6", "#22d3ee", "#a78bfa", "#34d399", "#fbbf24", "#fb7185"]


def _ensure_output_dir() -> str:
    """确保图表输出目录存在。"""
    output_path = get_abs_path(CHART_OUTPUT_DIR)
    os.makedirs(output_path, exist_ok=True)
    return output_path


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
    def pie_chart(df: pd.DataFrame, names_col: str, values_col: str,
                  title: str = "占比图", **kwargs) -> str:
        """生成饼图（类别占比分析）。返回保存路径。"""
        if not _plotly_available:
            return _placeholder("pie_chart", title)

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

        return func(df, **normalized)

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
    """保存图表为 HTML 文件，返回文件路径。"""
    output_dir = _ensure_output_dir()
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"{base_name}_{timestamp}.html"
    filepath = os.path.join(output_dir, filename)
    try:
        fig.write_html(filepath)
        logger.info(f"Chart saved: {filepath}")
        return filepath
    except Exception as e:
        logger.error(f"Failed to save chart: {e}")
        return f"[Chart generation failed: {e}]"


def _safe_name(name: str) -> str:
    """将标题转换为安全的文件名。"""
    import re
    name = re.sub(r"[^\w\s-]", "", name)
    return re.sub(r"[-\s]+", "_", name.strip().lower())[:50]


def _placeholder(chart_type: str, title: str) -> str:
    """当 Plotly 不可用时返回占位文本。"""
    return f"[PLACEHOLDER: {chart_type} - {title}. Install plotly to generate charts.]"
