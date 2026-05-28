"""
Product Analysis Agent: 接收 DataFrame，执行产品分析，生成自然语言洞察。
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
from analysis.product_analysis import ProductAnalysis
from utils.logger_handler import logger


PRODUCT_INSIGHT_PROMPT = """你是一个产品分析专家，请基于以下产品分析数据生成商业洞察。

## 数据
{data_json}

## 要求
1. 指出 TOP 产品及其表现。
2. 分析低利润产品的原因猜测。
3. 给出产品优化建议。
4. 输出 JSON 格式：{{"insight": "文本总结", "top_product_analysis": "分析", "low_profit_analysis": "分析", "recommendations": ["建议1", "建议2"]}}
"""


class ProductAgent(BaseAgent):
    """产品分析 Agent。"""

    name = "product_agent"

    def __init__(self):
        super().__init__()

    def run(self, input_data: dict) -> dict:
        """
        input_data = {
            "dataframe_json": str,
            "top_n": int (optional)
        }
        """
        df_json = input_data.get("dataframe_json", "[]")
        top_n = input_data.get("top_n", 5)

        try:
            from io import StringIO
            df = pd.read_json(StringIO(df_json), orient="records")
        except Exception as e:
            logger.error(f"Failed to parse DataFrame JSON: {e}")
            return {"error": f"Failed to parse data: {e}"}

        if df.empty:
            return {"error": "Empty dataset"}

        summary = ProductAnalysis.build_product_summary(df, top_n=top_n)

        try:
            insight = self._generate_insight(summary)
            summary.update(insight)
        except Exception as e:
            logger.warning(f"LLM insight failed: {e}")
            summary["insight"] = ""
            summary["recommendations"] = []

        return summary

    def _generate_insight(self, data: dict) -> dict:
        prompt = PRODUCT_INSIGHT_PROMPT.format(data_json=json.dumps(data, ensure_ascii=False, indent=2))
        messages = [{"role": "user", "content": prompt}]
        response = self._call_llm(messages)
        return self._parse_json(response)
