"""
Unified AnalysisAgent: 接受 AnalysisModule 适配器，执行分析 + LLM 洞察生成。

替代了三个复制粘贴的 TrendAgent / ProductAgent / RiskAgent。
适配器提供分析逻辑；Agent 提供 DataFrame 解析、LLM 调用和错误处理。
"""

import json

from agents.base import BaseAgent
from agents.workflow import WorkflowRunner
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

    def __init__(self, analyzer: AnalysisModule, user_id: str | None = None, model=None):
        super().__init__(user_id=user_id, model=model)
        self.analyzer = analyzer

    def run(self, input_data: dict) -> dict:
        """执行分析。

        input_data = {"dataframe_json": str, ...}  (兼容旧接口)
        或直接从 PipelineContext 获取 DataFrame。
        可选 "workflow": WorkflowRunner（planner 注入的请求级执行边界，
        提供结构校验 + journal + 结果缓存）；缺省时自建本地实例（独立调用）。
        """
        from io import StringIO

        import pandas as pd

        df_json = input_data.get("dataframe_json", "[]")
        wf: WorkflowRunner = input_data.get("workflow") or WorkflowRunner()

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

        # LLM 洞察生成（结构校验不过 → 异常 → 适配器 fallback；
        # 校验通过的 dict 才 update，杜绝 {"raw":...,"error":...} 解析垃圾污染结果）
        try:
            insight = self._generate_insight(result, wf)
            result.update(insight)
        except Exception as e:
            logger.warning(f"LLM insight generation failed: {e}")
            self.analyzer.apply_insight_fallback(result)

        return result

    def _generate_insight(self, data: dict, wf: WorkflowRunner) -> dict:
        """使用 LLM 生成分析洞察（经 WorkflowRunner 执行边界，schema 校验）。"""
        prompt = self.analyzer.insight_prompt.format(
            data_json=json.dumps(data, ensure_ascii=False, indent=2)
        )
        messages = [{"role": "user", "content": prompt}]
        # 契约归适配器：insight_schema 由各 AnalysisModule 声明（风险用
        # risk_assessment）；未声明的未知适配器宽进（空 schema = 任意 dict）
        schema = getattr(self.analyzer, "insight_schema", None) or {"type": "object"}
        label = f"insight.{type(self.analyzer).__name__}"
        insight = wf.agent(self, messages, schema, label=label, phase="Analyze")
        if insight is None:
            raise ValueError(f"insight schema validation failed: {label}")
        return insight