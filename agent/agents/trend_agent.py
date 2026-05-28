"""
Trend Analysis Agent: 接收 DataFrame，执行趋势分析，生成自然语言洞察。
"""

import json
import os
import sys

import pandas as pd

CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(CURRENT_DIR)
for path in (PROJECT_ROOT, os.path.dirname(PROJECT_ROOT)):
    if path not in sys.path:
        sys.path.insert(0, path)

from agents.base import BaseAgent
from analysis.trend_analysis import TrendAnalysis
from utils.logger_handler import logger


TREND_INSIGHT_PROMPT = """你是一个数据分析师，请基于以下趋势分析数据生成简洁的商业洞察。

## 数据
{data_json}

## 要求
1. 用 3-5 句话总结关键趋势。
2. 指出最重要的发现。
3. 如果有异常值，说明可能的影响。
4. 输出 JSON 格式：{{"insight": "文本总结", "key_findings": ["发现1", "发现2"], "recommendation": "建议"}}
"""


class TrendAgent(BaseAgent):
    """趋势分析 Agent：结合统计分析和 LLM 洞察。"""

    name = "trend_agent"

    def __init__(self):
        super().__init__()

    def run(self, input_data: dict) -> dict:
        """
        input_data = {
            "dataframe_json": str (JSON serialized DataFrame),
            "value_col": str (optional, default "total_revenue"),
            "date_col": str (optional, default "Month"),
        }
        returns: dict with trend_summary, insight, key_findings, recommendation
        """
        df_json = input_data.get("dataframe_json", "[]")
        value_col = input_data.get("value_col", "total_revenue")
        date_col = input_data.get("date_col", "Month")

        try:
            from io import StringIO
            df = pd.read_json(StringIO(df_json), orient="records")
        except Exception as e:
            logger.error(f"Failed to parse DataFrame JSON: {e}")
            return {"error": f"Failed to parse data: {e}"}

        if df.empty:
            return {"error": "Empty dataset", "trend_summary": "No data available"}

        # 如果数据是原始交易数据（包含 Avg_Price 和 Quantity 列），先做月度汇总
        if "Avg_Price" in df.columns and "Quantity" in df.columns:
            monthly_df = TrendAnalysis.monthly_revenue(df)
            summary = TrendAnalysis.build_trend_summary(monthly_df, value_col="total_revenue", month_col="Month")
        elif date_col in df.columns and value_col in df.columns:
            summary = TrendAnalysis.build_trend_summary(df, value_col=value_col, month_col=date_col)
        else:
            # 尝试直接用现有列做趋势分析
            summary = TrendAnalysis.build_trend_summary(df, value_col=df.columns[-1], month_col=df.columns[0])

        # 使用 LLM 生成自然语言洞察
        try:
            insight = self._generate_insight(summary)
            summary.update(insight)
        except Exception as e:
            logger.warning(f"LLM insight generation failed, using computed summary only: {e}")
            summary["insight"] = summary.get("trend_summary", "")
            summary["key_findings"] = []
            summary["recommendation"] = ""

        return summary

    def _generate_insight(self, data: dict) -> dict:
        """使用 LLM 生成趋势洞察。"""
        prompt = TREND_INSIGHT_PROMPT.format(data_json=json.dumps(data, ensure_ascii=False, indent=2))
        messages = [
            {"role": "user", "content": prompt},
        ]
        response = self._call_llm(messages)
        return self._parse_json(response)
