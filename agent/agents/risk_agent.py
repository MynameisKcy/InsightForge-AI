"""
Risk Analysis Agent: 接收 DataFrame，执行异常检测和风险分析。
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
from analysis.anomaly_detection import AnomalyDetection
from utils.logger_handler import logger


RISK_INSIGHT_PROMPT = """你是一个风险分析专家，请基于以下异常检测数据生成风险评估。

## 数据
{data_json}

## 要求
1. 评估整体风险等级。
2. 指出关键风险点。
3. 给出风险缓解建议。
4. 输出 JSON 格式：{{"risk_assessment": "评估文本", "key_risks": ["风险1", "风险2"], "mitigation": ["建议1", "建议2"]}}
"""


class RiskAgent(BaseAgent):
    """风险分析 Agent。"""

    name = "risk_agent"

    def __init__(self):
        super().__init__()

    def run(self, input_data: dict) -> dict:
        """
        input_data = {
            "dataframe_json": str,
        }
        """
        df_json = input_data.get("dataframe_json", "[]")

        try:
            from io import StringIO
            df = pd.read_json(StringIO(df_json), orient="records")
        except Exception as e:
            logger.error(f"Failed to parse DataFrame JSON: {e}")
            return {"error": f"Failed to parse data: {e}"}

        if df.empty:
            return {"error": "Empty dataset"}

        summary = AnomalyDetection.build_risk_summary(df)

        try:
            insight = self._generate_insight(summary)
            summary.update(insight)
        except Exception as e:
            logger.warning(f"LLM insight failed: {e}")
            summary["risk_assessment"] = ""
            summary["key_risks"] = []
            summary["mitigation"] = []

        return summary

    def _generate_insight(self, data: dict) -> dict:
        prompt = RISK_INSIGHT_PROMPT.format(data_json=json.dumps(data, ensure_ascii=False, indent=2))
        messages = [{"role": "user", "content": prompt}]
        response = self._call_llm(messages)
        return self._parse_json(response)
