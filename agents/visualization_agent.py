"""
Visualization Agent: 接收分析结果，自动选择合适的图表类型并生成图表。
"""

import json

import pandas as pd

from agents.base import BaseAgent, parse_json_list
from visualization.charts import ChartGenerator, chart_png_path
from utils.logger_handler import logger
from rag.chart_knowledge import chart_knowledge


CHART_DECISION_PROMPT = """你是一个数据可视化专家。根据数据分析结果，决定应该生成哪些图表。

## 数据概况
{data_json}

## 任务描述
{task}

## 可用的图表类型
- line: 折线图（趋势、时间序列）
- bar: 柱状图（排名、对比）
- pie: 饼图（占比、分布）
- scatter: 散点图（相关性）
- heatmap: 热力图（矩阵、区域分析）

## 要求
输出 JSON 格式的图表列表：
[
  {{"chart_type": "line", "title": "趋势变化", "x_col": "Month", "y_col": "total_revenue", "x_label": "月份", "y_label": "度量值", "reason": "展示随时间的变化趋势"}},
  ...
]

最多生成 4 张最重要的图表。每张图表指定：
- chart_type, title
- x_col, y_col（或 names_col/values_col）：必须用上面"数据概况"里出现的实际列名
- x_label, y_label：人类可读的中文轴标签，依列语义命名（如 total_revenue -> "总收入"、population -> "人口数"、Month -> "月份"、region -> "地区"）
- reason
轴的范围与刻度格式由系统按数据自动处理，你只需给出语义化标签。
只输出 JSON 数组，不要有其他文字。
"""


class VisualizationAgent(BaseAgent):
    """可视化 Agent：根据分析数据自动生成多个图表。"""

    name = "visualization_agent"

    def __init__(self, model=None):
        super().__init__(model=model)
        self.chart_generator = ChartGenerator()

    def run(self, input_data: dict) -> dict:
        """
        input_data = {
            "dataframe_json": str,       # 主数据集
            "task": str,                 # 原始任务描述
            "extra_data": dict | None,   # 其他分析数据（如 trend_summary, product_summary）
            "chart_specs": list | None,  # 可选：手动指定图表规格，跳过 LLM 决策
        }
        returns: {"charts": [{"path": str, "title": str, "type": str}], "error": str | None}
        """
        df_json = input_data.get("dataframe_json", "[]")
        task = input_data.get("task", "数据分析")
        chart_specs = input_data.get("chart_specs", None)
        extra_data = input_data.get("extra_data", {})

        try:
            from io import StringIO
            df = pd.read_json(StringIO(df_json), orient="records")
        except Exception as e:
            logger.error(f"Failed to parse DataFrame JSON: {e}")
            return {"charts": [], "error": f"Failed to parse data: {e}"}

        if df.empty:
            return {"charts": [], "error": "Empty dataset"}

        # 如果没有预定义图表规格，使用 LLM 决定
        if chart_specs is None:
            try:
                chart_specs = self._decide_charts(df, task, extra_data)
            except Exception as e:
                logger.warning(f"LLM chart decision failed, using auto-detection: {e}")
                chart_specs = self._auto_charts(df, task)

        # 生成图表
        # 启动单一 kaleido sync server 渲染本批所有 PNG（复用 chromium scope，
        # 避免 fig.write_image 逐次新建 scope 在第 2 次挂起）。server 进程内常驻，
        # 不在此 stop（见 charts.stop_png_batch 注释）。
        charts = []
        from visualization.charts import start_png_batch
        start_png_batch()
        for spec in chart_specs:
            try:
                chart_path = self._generate_chart(df, spec, extra_data)
                chart_entry = {
                    "path": chart_path,
                    "title": spec.get("title", "Chart"),
                    "type": spec.get("chart_type", "bar"),
                    # 同名 PNG（kaleido 渲染），供报告导出嵌入栅格图；无则为 None
                    # 注意：chart_png_path 是 charts.py 的模块级函数，不是
                    # ChartGenerator 的静态方法（误调会 AttributeError 使
                    # 已成功渲染的图表被误标为失败）。
                    "png_path": chart_png_path(chart_path),
                }
                charts.append(chart_entry)

                # 存入图表知识库（RAG 内部资料）
                try:
                    chart_knowledge.save_chart({
                        "chart_type": spec.get("chart_type", "bar"),
                        "title": spec.get("title", "图表"),
                        "x_col": spec.get("x_col", ""),
                        "y_col": spec.get("y_col", ""),
                        "data_summary": self._summarize_data(df, spec),
                        "analysis_text": spec.get("reason", ""),
                        "chart_path": chart_path,
                        "task_context": task,
                    })
                except Exception as e:
                    logger.warning(f"Failed to save chart to knowledge base: {e}")

            except Exception as e:
                logger.error(f"Chart generation failed for {spec.get('title')}: {e}")
                charts.append({
                    "path": f"[Error: {e}]",
                    "title": spec.get("title", "Chart"),
                    "type": spec.get("chart_type", "bar"),
                    "png_path": None,
                })

        return {"charts": charts, "error": None}

    @staticmethod
    def _resolve_column(df: pd.DataFrame, col_name, prefer: str = "number") -> str | None:
        """验证并修正列名：如果 col_name 不存在，在 df 中找最接近的匹配列。
        prefer: "number" 优先数值列, "object" 优先文本列, "any" 任意列。

        col_name 为 list/tuple（LLM 偶尔给多列，如多系列柱状图）时逐项解析，
        返回第一个有效列——下游 charts.py 的 y_col/x_col 均为单字符串契约
        （labels 字典键、dropna(subset=[...])），直接透传 list 会以
        "unhashable type: 'list'" 使整张图失败。
        """
        if isinstance(col_name, (list, tuple)):
            # 第一遍：任一item精确命中即用（优先精确列名，避免被其他
            # item 的"默认列回退"抢先，画错列）。
            for item in col_name:
                if isinstance(item, str) and item in df.columns:
                    return item
            # 第二遍：逐项走模糊匹配/默认列回退
            for item in col_name:
                resolved = VisualizationAgent._resolve_column(df, item, prefer=prefer)
                if resolved:
                    return resolved
            return None
        if not col_name:
            return None
        if col_name in df.columns:
            return col_name
        # 模糊匹配：检查实际列名是否包含请求的关键词
        cols = df.columns.tolist()
        for c in cols:
            if col_name.lower() in c.lower() or c.lower() in col_name.lower():
                return c
        # 无匹配时按 prefer 返回默认列
        if prefer == "number":
            num_cols = df.select_dtypes(include=["number"]).columns.tolist()
            return num_cols[0] if num_cols else (cols[0] if cols else None)
        elif prefer == "object":
            obj_cols = df.select_dtypes(include=["object", "category"]).columns.tolist()
            return obj_cols[0] if obj_cols else (cols[0] if cols else None)
        return cols[0] if cols else None

    def _generate_chart(self, df: pd.DataFrame, spec: dict, extra_data: dict) -> str:
        """根据规格生成单张图表。"""
        chart_type = spec.get("chart_type", "bar")
        title = spec.get("title", "图表")
        x_label = spec.get("x_label", "")
        y_label = spec.get("y_label", "")

        # 如果有 extra_data 中的专用数据（如趋势摘要），优先使用
        if chart_type == "line" and "trend_summary" in extra_data:
            trend_data = extra_data.get("trend_summary", {})
            monthly = trend_data.get("monthly_data", [])
            if monthly:
                df_chart = pd.DataFrame(monthly)
                y_col = self._resolve_column(df_chart, spec.get("y_col", ""), prefer="number")
                x_col = self._resolve_column(df_chart, spec.get("x_col", ""), prefer="object")
                return ChartGenerator.line_chart(df_chart, x_col=x_col, y_col=y_col, title=title,
                                                 x_label=x_label, y_label=y_label)

        if chart_type == "bar" and "product_summary" in extra_data:
            top = extra_data.get("product_summary", {}).get("top_products", [])
            if top:
                df_chart = pd.DataFrame(top)
                x_col = self._resolve_column(df_chart, spec.get("x_col", ""), prefer="object")
                y_col = self._resolve_column(df_chart, spec.get("y_col", ""), prefer="number")
                return ChartGenerator.bar_chart(df_chart, x_col=x_col, y_col=y_col, title=title,
                                                x_label=x_label, y_label=y_label)

        if chart_type == "pie" and "product_summary" in extra_data:
            cat = extra_data.get("product_summary", {}).get("category_summary", [])
            if cat:
                df_chart = pd.DataFrame(cat)
                names_col = self._resolve_column(df_chart, spec.get("names_col", ""), prefer="object")
                values_col = self._resolve_column(df_chart, spec.get("values_col", ""), prefer="number")
                return ChartGenerator.pie_chart(
                    df_chart, names_col=names_col, values_col=values_col, title=title,
                )

        # 通用情况：清理并验证 spec 中的列名，语义化标签原样透传
        # 列表列名（多序列 y_col 等）由 _resolve_column 的 list 分支统一解析
        validated = {}
        for k, v in spec.items():
            if k in ("chart_type", "title", "reason"):
                continue
            if k in ("x_col", "names_col", "labels_col"):
                validated[k] = self._resolve_column(df, v, prefer="object")
            elif k in ("y_col", "values_col"):
                validated[k] = self._resolve_column(df, v, prefer="number")
            else:
                validated[k] = v
        return ChartGenerator.auto_chart(df, chart_type, title=title, **validated)

    def _decide_charts(self, df: pd.DataFrame, task: str, extra_data: dict) -> list[dict]:
        """使用 LLM 决定生成哪些图表。喂入含统计量的数据概况，便于 LLM 选列与命名轴标签。"""
        summary = self._data_summary(df)
        prompt = CHART_DECISION_PROMPT.format(
            data_json=json.dumps(summary, ensure_ascii=False, indent=2, default=str),
            task=task,
        )
        messages = [{"role": "user", "content": prompt}]
        response = self._call_llm(messages)

        # 解析 JSON 数组：qwen3.7 等模型会加 ```json 围栏，parse_json_list 统一容忍
        specs = [s for s in parse_json_list(response) if isinstance(s, dict)]
        if specs:
            return specs
        # 兼容对象型输出（单图规格）：包一层返回
        result = self._parse_json(response)
        if isinstance(result, dict) and "raw" not in result:
            return [result]
        return []

    def _data_summary(self, df: pd.DataFrame) -> dict:
        """构建喂给 LLM 的数据概况：列/dtype + 数值列统计 + 类别列高频值。"""
        cols_info = []
        for c in df.columns:
            info = {"col": c, "dtype": str(df[c].dtype)}
            if pd.api.types.is_numeric_dtype(df[c]):
                s = df[c].dropna()
                if not s.empty:
                    info.update({
                        "min": float(s.min()),
                        "max": float(s.max()),
                        "mean": round(float(s.mean()), 2),
                        "nunique": int(s.nunique()),
                    })
            else:
                nunique = int(df[c].nunique(dropna=True))
                info["nunique"] = nunique
                if nunique <= 12:
                    info["top_values"] = [str(x) for x in df[c].dropna().value_counts().head(8).index.tolist()]
            cols_info.append(info)
        return {
            "columns": cols_info,
            "sample": df.head(3).to_dict(orient="records"),
            "shape": list(df.shape),
        }

    def _summarize_data(self, df: pd.DataFrame, spec: dict) -> str:
        """生成数据摘要文本，用于存入知识库。"""
        try:
            x_col = spec.get("x_col", "")
            y_col = spec.get("y_col", "")
            parts = [f"行数: {len(df)}, 列: {', '.join(df.columns[:6])}"]
            if y_col and y_col in df.columns:
                col_data = df[y_col]
                if pd.api.types.is_numeric_dtype(col_data):
                    parts.append(
                        f"{y_col}: 合计={col_data.sum():,.1f}, "
                        f"均值={col_data.mean():,.1f}, "
                        f"最大={col_data.max():,.1f}"
                    )
            if x_col and x_col in df.columns:
                parts.append(f"{x_col}: {df[x_col].nunique()} 个唯一值")
            return "; ".join(parts)
        except Exception:
            return f"数据集: {len(df)} 行, {len(df.columns)} 列"

    def _auto_charts(self, df: pd.DataFrame, task: str) -> list[dict]:
        """当 LLM 不可用时，自动推断图表。"""
        charts = []
        numeric_cols = df.select_dtypes(include=["number"]).columns.tolist()
        cat_cols = df.select_dtypes(include=["object", "category"]).columns.tolist()

        # 如果有时序列，生成趋势图
        time_cols = [c for c in df.columns if any(w in c.lower() for w in ["month", "date", "time", "月份", "日期"])]
        if time_cols and len(numeric_cols) >= 1:
            charts.append({
                "chart_type": "line",
                "title": "月度趋势分析",
                "x_col": time_cols[0],
                "y_col": numeric_cols[-1],
            })

        # 如果有类别列，生成柱状图
        cat_cols_filtered = [c for c in cat_cols if c not in time_cols]
        if cat_cols_filtered and len(numeric_cols) >= 1:
            charts.append({
                "chart_type": "bar",
                "title": "类别对比分析",
                "x_col": cat_cols_filtered[0],
                "y_col": numeric_cols[0],
            })

        # 至少生成一张图
        if not charts and len(numeric_cols) >= 1:
            charts.append({
                "chart_type": "bar",
                "title": "数据分析图表",
                "x_col": df.columns[0],
                "y_col": numeric_cols[0],
            })

        return charts[:4]
