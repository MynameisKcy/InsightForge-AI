"""
Trend Analysis Agent: 接收 DataFrame，执行趋势分析，生成自然语言洞察。
"""

import json

import pandas as pd

from agents.base import BaseAgent
from analysis.trend_analysis import TrendAnalysis
from utils.logger_handler import logger

TREND_INSIGHT_PROMPT = """你是一个数据分析师，请基于以下趋势分析数据生成简洁的数据洞察。

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

    def __init__(self, user_id=None, model=None):
        # user_id 透传给 BaseAgent → factory 按用户解析 LLM 配置（网页设置 > .env）。
        # 不传则回退默认配置（.env 模型），多用户下会 403/串配置。
        # model= 注入优先（测试注入哨兵可跳过真客户端构造；与其他子 Agent 一致）。
        super().__init__(user_id=user_id, model=model)

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
            # 兜底:选真正的数值列做 value_col,避免取到文本列导致 str/str
            num_cols = df.select_dtypes(include="number").columns.tolist()
            if not num_cols:
                return {
                    "error": "数据集无可分析的数值列,无法做趋势分析",
                    "trend_summary": "No numeric column for trend analysis",
                    "growth_rate": "N/A",
                    "anomaly_months": [],
                }
            # month_col 取第一个非数值列,若无则用 df.columns[0]
            non_num = [c for c in df.columns if c not in num_cols]
            picked_value = num_cols[0]
            picked_month = non_num[0] if non_num else df.columns[0]
            summary = TrendAnalysis.build_trend_summary(
                df, value_col=picked_value, month_col=picked_month
            )

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
