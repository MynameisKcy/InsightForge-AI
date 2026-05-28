"""
Report Agent: 整合所有分析结果 → 生成 Markdown 报告。
"""

import json
import os
import sys
from datetime import datetime
from typing import Any

CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(CURRENT_DIR)
for path in (PROJECT_ROOT, os.path.dirname(PROJECT_ROOT)):
    if path not in sys.path:
        sys.path.insert(0, path)

from agents.base import BaseAgent
from utils.logger_handler import logger
from utils.path_tool import get_abs_path

try:
    from jinja2 import Template
    _jinja2_available = True
except ImportError:
    _jinja2_available = False
    logger.warning("Jinja2 not installed. Reports will use basic string formatting.")


REPORT_TEMPLATE_PATH = get_abs_path("templates/report_template.md")


EXECUTIVE_SUMMARY_PROMPT = """你是一个高级商业分析师。请根据以下所有分析结果生成一段 3-5 句的执行摘要。

## 趋势分析
{trend_summary}

## 产品分析
{product_summary}

## 风险分析
{risk_summary}

## 要求
用简洁的中文总结核心发现，突出最重要的数据和洞察，适合给管理层阅读。
只输出执行摘要文本，不要 JSON。
"""


CONCLUSION_PROMPT = """你是一个高级商业分析师。请根据以下分析结果生成 2-3 句的结论。

## 分析数据
{data_summary}

## 要求
简洁有力，总结整体情况并给出一个方向性建议。
只输出结论文本，不要 JSON。
"""


class ReportAgent(BaseAgent):
    """报告生成 Agent：整合所有分析结果生成 Markdown 报告。"""

    name = "report_agent"

    def __init__(self):
        super().__init__()
        self._template = None
        if _jinja2_available and os.path.exists(REPORT_TEMPLATE_PATH):
            with open(REPORT_TEMPLATE_PATH, "r", encoding="utf-8") as f:
                self._template = Template(f.read())

    def run(self, input_data: dict) -> dict:
        """
        input_data = {
            "task": str,                          # 原始任务
            "sql_result": dict,                    # SQL Agent 结果
            "trend_result": dict,                  # Trend Agent 结果
            "product_result": dict,                # Product Agent 结果
            "risk_result": dict,                   # Risk Agent 结果
            "charts": list,                        # Visualization Agent 结果
            "title": str (optional),
        }
        returns: {"markdown": str, "title": str, "path": str | None}
        """
        task = input_data.get("task", "数据分析报告")
        title = input_data.get("title", "数据分析报告")
        generated_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        trend_result = input_data.get("trend_result", {})
        product_result = input_data.get("product_result", {})
        risk_result = input_data.get("risk_result", {})
        charts = input_data.get("charts", [])

        # 生成执行摘要
        executive_summary = self._generate_executive_summary(trend_result, product_result, risk_result)

        # 生成结论
        conclusion = self._generate_conclusion(trend_result, product_result, risk_result)

        # 构建报告数据
        report_data = self._build_report_data(
            title=title,
            generated_at=generated_at,
            task=task,
            executive_summary=executive_summary,
            trend_result=trend_result,
            product_result=product_result,
            risk_result=risk_result,
            charts=charts,
            conclusion=conclusion,
        )

        # 渲染 Markdown
        markdown = self._render_report(report_data)

        # 保存报告
        report_path = self._save_report(markdown, title)

        return {
            "markdown": markdown,
            "title": title,
            "path": report_path,
        }

    def _build_report_data(self, **kwargs) -> dict:
        """将各模块结果组装为模板数据。"""
        trend = kwargs.get("trend_result", {})
        product = kwargs.get("product_result", {})
        risk = kwargs.get("risk_result", {})
        charts = kwargs.get("charts", [])

        # 解析图表路径
        trend_chart = ""
        product_chart = ""
        risk_chart = ""
        for c in charts:
            ctype = c.get("type", "")
            if ctype in ("line", "trend") and not trend_chart:
                trend_chart = c.get("path", "")
            elif ctype in ("bar", "pie") and not product_chart:
                product_chart = c.get("path", "")

        # 异常月份详情
        anomaly_months = trend.get("anomaly_months", [])
        anomaly_months_detail = ""
        if anomaly_months:
            items = []
            for a in anomaly_months:
                if isinstance(a, dict):
                    items.append(f"- 月份 {a.get('index', a.get('month', '?'))}: 值 {a.get('value', '?')}")
                else:
                    items.append(f"- {a}")
            anomaly_months_detail = "\n".join(items)

        # 安全获取嵌套数据
        def safe_list(data, key, default=None):
            if default is None:
                default = []
            val = data.get(key, default)
            return val if val is not None else default

        return {
            "title": kwargs.get("title", "数据分析报告"),
            "generated_at": kwargs.get("generated_at", datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
            "executive_summary": kwargs.get("executive_summary", ""),
            "direction": trend.get("direction", "N/A"),
            "overall_growth_pct": trend.get("overall_growth_pct", "N/A"),
            "start_value": trend.get("start_value", "N/A"),
            "end_value": trend.get("end_value", "N/A"),
            "anomaly_months": bool(anomaly_months),
            "anomaly_months_detail": anomaly_months_detail,
            "trend_insight": trend.get("insight", trend.get("trend_summary", "")),
            "trend_chart": trend_chart,
            "product_insight": product.get("insight", ""),
            "top_products": safe_list(product, "top_products")[:10],
            "category_summary": safe_list(product, "category_summary")[:10],
            "product_chart": product_chart,
            "risk_level": risk.get("risk_level", "N/A"),
            "risk_assessment": risk.get("risk_assessment", ""),
            "key_risks": safe_list(risk, "key_risks"),
            "revenue_anomalies": risk.get("revenue_anomalies"),
            "recommendations": self._format_recommendations(trend, product, risk, kwargs.get("task", "")),
            "conclusion": kwargs.get("conclusion", ""),
        }

    def _format_recommendations(self, trend: dict, product: dict, risk: dict, task: str) -> str:
        """整合来自各模块的建议。"""
        parts = []
        recs = trend.get("recommendations", [])
        if isinstance(recs, str) and recs:
            parts.append(f"趋势建议: {recs}")
        elif isinstance(recs, list):
            for r in recs:
                parts.append(f"- {r}")

        recs = product.get("recommendations", [])
        if isinstance(recs, str) and recs:
            parts.append(f"产品建议: {recs}")
        elif isinstance(recs, list):
            for r in recs:
                parts.append(f"- {r}")

        mitigations = risk.get("mitigation", [])
        if isinstance(mitigations, str) and mitigations:
            parts.append(f"风险建议: {mitigations}")
        elif isinstance(mitigations, list):
            for r in mitigations:
                parts.append(f"- {r}")

        rec = trend.get("recommendation", "")
        if rec and rec not in "\n".join(parts):
            parts.append(f"- {rec}")

        if not parts:
            parts.append("基于当前数据持续监控关键指标变化。")
        return "\n".join(parts)

    def _generate_executive_summary(self, trend: dict, product: dict, risk: dict) -> str:
        """使用 LLM 生成执行摘要。"""
        try:
            prompt = EXECUTIVE_SUMMARY_PROMPT.format(
                trend_summary=json.dumps(trend, ensure_ascii=False, indent=2)[:2000],
                product_summary=json.dumps(product, ensure_ascii=False, indent=2)[:2000],
                risk_summary=json.dumps(risk, ensure_ascii=False, indent=2)[:2000],
            )
            messages = [{"role": "user", "content": prompt}]
            return self._call_llm(messages)
        except Exception as e:
            logger.warning(f"Executive summary generation failed: {e}")
            parts = []
            if trend.get("trend_summary"):
                parts.append(str(trend["trend_summary"]))
            if product.get("insight"):
                parts.append(str(product["insight"]))
            return " ".join(parts) or "数据分析完成，详情请参见以下各节。"

    def _generate_conclusion(self, trend: dict, product: dict, risk: dict) -> str:
        """使用 LLM 生成结论。"""
        try:
            prompt = CONCLUSION_PROMPT.format(
                data_summary=json.dumps({
                    "trend": trend.get("trend_summary", ""),
                    "risk_level": risk.get("risk_level", ""),
                    "product_summary": product.get("insight", ""),
                }, ensure_ascii=False, indent=2)[:2000],
            )
            messages = [{"role": "user", "content": prompt}]
            return self._call_llm(messages)
        except Exception as e:
            logger.warning(f"Conclusion generation failed: {e}")
            direction = trend.get("direction", "")
            if direction:
                return f"整体业务呈{direction}趋势，建议持续关注关键指标变化，及时调整策略。"
            return "数据分析已完成，建议根据各项分析结果制定相应策略。"

    def _render_report(self, data: dict) -> str:
        """渲染报告为 Markdown 字符串。"""
        if self._template:
            try:
                return self._template.render(**data)
            except Exception as e:
                logger.warning(f"Jinja2 rendering failed, falling back to basic: {e}")

        # 后备：基本 Markdown 生成
        return _basic_markdown_report(data)

    def _save_report(self, markdown: str, title: str) -> str:
        """保存报告为 .md 文件。"""
        output_dir = get_abs_path("reports")
        os.makedirs(output_dir, exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        safe_title = "".join(c for c in title if c.isalnum() or c in " _-")[:30]
        filename = f"report_{safe_title}_{timestamp}.md"
        filepath = os.path.join(output_dir, filename)
        with open(filepath, "w", encoding="utf-8") as f:
            f.write(markdown)
        logger.info(f"Report saved to {filepath}")
        return filepath


def _basic_markdown_report(data: dict) -> str:
    """纯 Python 生成的 Markdown 报告（不使用 Jinja2 模板时）。"""
    lines = [
        f"# {data.get('title', '数据分析报告')}",
        "",
        f"**生成时间**: {data.get('generated_at', '')}",
        "",
        "---",
        "",
        "## 一、执行摘要",
        "",
        str(data.get('executive_summary', '')),
        "",
        "---",
        "",
        "## 二、总体趋势分析",
        "",
        str(data.get('trend_insight', '')),
        "",
        f"- 整体趋势方向: {data.get('direction', 'N/A')}",
        f"- 总体变化幅度: {data.get('overall_growth_pct', 'N/A')}%",
        f"- 起始值: {data.get('start_value', 'N/A')}",
        f"- 结束值: {data.get('end_value', 'N/A')}",
        "",
    ]

    if data.get("anomaly_months_detail"):
        lines.extend([
            "### 异常月份",
            "",
            str(data["anomaly_months_detail"]),
            "",
        ])

    if data.get("trend_chart"):
        lines.append(f"![趋势图]({data['trend_chart']})")
        lines.append("")

    lines.extend([
        "---",
        "",
        "## 三、产品分析",
        "",
        str(data.get('product_insight', '')),
        "",
    ])

    top_products = data.get("top_products", [])
    if top_products:
        lines.append("### TOP 产品")
        lines.append("")
        for i, p in enumerate(top_products[:10], 1):
            name = p.get("Product_Description", p.get("product", "Unknown"))
            rev = p.get("total_revenue", "N/A")
            qty = p.get("total_quantity", "N/A")
            lines.append(f"{i}. {name} - 收入: {rev}, 销量: {qty}")
        lines.append("")

    if data.get("product_chart"):
        lines.append(f"![产品分析图]({data['product_chart']})")
        lines.append("")

    lines.extend([
        "---",
        "",
        "## 四、风险分析",
        "",
        f"**风险等级**: {data.get('risk_level', 'N/A')}",
        "",
        str(data.get('risk_assessment', '')),
        "",
    ])

    key_risks = data.get("key_risks", [])
    if key_risks:
        lines.append("### 主要风险点")
        for r in key_risks:
            lines.append(f"- {r}")
        lines.append("")

    lines.extend([
        "---",
        "",
        "## 五、建议",
        "",
        str(data.get('recommendations', '')),
        "",
        "---",
        "",
        "## 六、结论",
        "",
        str(data.get('conclusion', '')),
        "",
        "*本报告由 AI Data Analyst Multi-Agent System 自动生成*",
    ])

    return "\n".join(lines)
