"""
AnalysisModule Protocol + 适配器。

将分析逻辑封装为统一接口，使 AnalysisAgent 可以通过依赖注入
适配不同分析类型（趋势 / 产品 / 风险），不再需要三个复制粘贴的 Agent 类。
"""

from typing import Protocol

import pandas as pd

from analysis.anomaly_detection import AnomalyDetection
from analysis.product_analysis import ProductAnalysis
from analysis.trend_analysis import TrendAnalysis


class AnalysisModule(Protocol):
    """分析模块协议 —— 真实接缝，三个适配器实现。

    一个适配器 = 假设接缝；两个 = 真实接缝。此处三个适配器证明接缝是真实的。
    """

    insight_prompt: str
    """LLM 洞察生成的提示词模板，包含 {data_json} 占位符。"""

    def analyze(self, df: pd.DataFrame) -> dict:
        """分析 DataFrame，返回结构化结果字典。"""
        ...

    def apply_insight_fallback(self, result: dict) -> None:
        """LLM 洞察失败时，用本适配器的输出键补安全默认值（契约归适配器，不泄漏到他类）。"""
        ...


# ── 趋势分析适配器 ──

TREND_INSIGHT_PROMPT = """你是一个数据分析师，请基于以下趋势分析数据生成简洁的数据洞察。

## 数据
{data_json}

## 要求
1. 用 3-5 句话总结关键趋势。
2. 指出最重要的发现。
3. 如果有异常值，说明可能的影响。
4. 输出 JSON 格式：{{"insight": "文本总结", "key_findings": ["发现1", "发现2"], "recommendation": "建议"}}
"""


class TrendAnalysisAdapter:
    """趋势分析适配器 —— 封装列选择与 TrendAnalysis 调用。"""

    insight_prompt = TREND_INSIGHT_PROMPT

    def analyze(self, df: pd.DataFrame) -> dict:
        if df.empty:
            return {"error": "Empty dataset", "trend_summary": "No data available"}

        # 原始交易数据 → 月度汇总
        if "Avg_Price" in df.columns and "Quantity" in df.columns:
            monthly_df = TrendAnalysis.monthly_revenue(df)
            return TrendAnalysis.build_trend_summary(
                monthly_df, value_col="total_revenue", month_col="Month"
            )

        # 已是月度汇总数据
        if "Month" in df.columns and "total_revenue" in df.columns:
            return TrendAnalysis.build_trend_summary(
                df, value_col="total_revenue", month_col="Month"
            )

        # 兜底：选真正的数值列做 value_col，避免取到文本列导致 str/str
        num_cols = df.select_dtypes(include="number").columns.tolist()
        if not num_cols:
            return {
                "error": "数据集无可分析的数值列,无法做趋势分析",
                "trend_summary": "No numeric column for trend analysis",
                "growth_rate": "N/A",
                "anomaly_months": [],
            }
        non_num = [c for c in df.columns if c not in num_cols]
        picked_value = num_cols[0]
        picked_month = non_num[0] if non_num else df.columns[0]
        return TrendAnalysis.build_trend_summary(
            df, value_col=picked_value, month_col=picked_month
        )

    def apply_insight_fallback(self, result: dict) -> None:
        result.setdefault("insight", result.get("trend_summary", ""))
        result.setdefault("key_findings", [])
        result.setdefault("recommendation", "")


# ── 产品分析适配器 ──

PRODUCT_INSIGHT_PROMPT = """你是一个数据分析专家，请基于以下分组对比分析数据生成数据洞察。

## 数据
{data_json}

## 要求
1. 指出 TOP 项及其表现。
2. 分析表现较弱项的原因。
3. 给出可落地的优化建议。
4. 输出 JSON 格式：{{"insight": "文本总结", "top_item_analysis": "分析", "low_performer_analysis": "分析", "recommendations": ["建议1", "建议2"]}}
"""


class ProductAnalysisAdapter:
    """产品分析适配器。"""

    insight_prompt = PRODUCT_INSIGHT_PROMPT

    def analyze(self, df: pd.DataFrame) -> dict:
        if df.empty:
            return {"error": "Empty dataset"}
        return ProductAnalysis.build_product_summary(df, top_n=5)

    def apply_insight_fallback(self, result: dict) -> None:
        result.setdefault("insight", "")
        result.setdefault("top_item_analysis", "")
        result.setdefault("low_performer_analysis", "")
        result.setdefault("recommendations", [])


# ── 风险分析适配器 ──

RISK_INSIGHT_PROMPT = """你是一个风险分析专家，请基于以下异常检测数据生成风险评估。

## 数据
{data_json}

## 要求
1. 评估整体风险等级。
2. 指出关键风险点。
3. 给出风险缓解建议。
4. 输出 JSON 格式：{{"risk_assessment": "评估文本", "key_risks": ["风险1", "风险2"], "mitigation": ["建议1", "建议2"]}}
"""


class RiskAnalysisAdapter:
    """风险分析适配器。"""

    insight_prompt = RISK_INSIGHT_PROMPT

    def analyze(self, df: pd.DataFrame) -> dict:
        if df.empty:
            return {"error": "Empty dataset"}
        return AnomalyDetection.build_risk_summary(df)

    def apply_insight_fallback(self, result: dict) -> None:
        result.setdefault("risk_assessment", "")
        result.setdefault("key_risks", [])
        result.setdefault("mitigation", [])