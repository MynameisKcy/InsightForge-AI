"""
Unified AnalysisAgent: 接受 AnalysisModule 适配器，执行分析 + LLM 洞察生成。

替代了三个复制粘贴的 TrendAgent / ProductAgent / RiskAgent。
适配器提供分析逻辑；Agent 提供 DataFrame 解析、LLM 调用和错误处理。
"""

import json
import os
import sys

CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(CURRENT_DIR)
for path in (PROJECT_ROOT, os.path.dirname(PROJECT_ROOT)):
    if path not in sys.path:
        sys.path.insert(0, path)

from agents.base import BaseAgent
from analysis.analysis_module import AnalysisModule
from utils.logger_handler import logger


class AnalysisAgent(BaseAgent):
    """统一分析 Agent —— 一个深层模块，三个适配器。

    构造时接受一个 AnalysisModule 适配器，适配器的 analyze() 方法
    封装了该分析类型特有的列选择与计算逻辑。Agent 负责：
    1. 从 PipelineContext 获取 DataFrame（共享反序列化）
    2. 调用适配器的 analyze()
    3. 调用 LLM 生成自然语言洞察
    """

    name = "analysis_agent"

    def __init__(self, analyzer: AnalysisModule, model=None):
        super().__init__(model=model)
        self.analyzer = analyzer

    def run(self, input_data: dict) -> dict:
        """执行分析。

        input_data = {"dataframe_json": str, ...}  (兼容旧接口)
        或直接从 PipelineContext 获取 DataFrame。
        """
        import pandas as pd
        from io import StringIO

        df_json = input_data.get("dataframe_json", "[]")

        # 解析 DataFrame
        try:
            df = pd.read_json(StringIO(df_json), orient="records")
        except Exception as e:
            logger.error(f"Failed to parse DataFrame JSON: {e}")
            return {"error": f"Failed to parse data: {e}"}

        if df.empty:
            return {"error": "Empty dataset"}

        # 调用适配器的分析逻辑（列选择 + 计算）
        result = self.analyzer.analyze(df)

        # 如果适配器返回了错误，直接返回
        if "error" in result:
            return result

        # LLM 洞察生成
        try:
            insight = self._generate_insight(result)
            result.update(insight)
        except Exception as e:
            logger.warning(f"LLM insight generation failed: {e}")
            self.analyzer.apply_insight_fallback(result)

        return result

    def _generate_insight(self, data: dict) -> dict:
        """使用 LLM 生成分析洞察。"""
        prompt = self.analyzer.insight_prompt.format(
            data_json=json.dumps(data, ensure_ascii=False, indent=2)
        )
        messages = [{"role": "user", "content": prompt}]
        response = self._call_llm(messages)
        return self._parse_json(response)