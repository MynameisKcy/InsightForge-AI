"""
Trend Analysis: 销售趋势、利润趋势、同比环比、增长率、时间序列分析
"""

import pandas as pd
import numpy as np
from typing import Any


class TrendAnalysis:
    """纯计算分析模块：不依赖 LLM，使用 pandas/numpy 做趋势分析。"""

    @staticmethod
    def monthly_revenue(df: pd.DataFrame, date_col: str = "Month",
                        price_col: str = "Avg_Price", qty_col: str = "Quantity") -> pd.DataFrame:
        """按月汇总收入 (Quantity * Avg_Price) 趋势。"""
        df = df.copy()
        df["_revenue"] = df[qty_col] * df[price_col]
        monthly = df.groupby(date_col).agg(
            total_revenue=("_revenue", "sum"),
            total_orders=(qty_col, "count"),
            total_quantity=(qty_col, "sum"),
        ).sort_index().reset_index()
        # 确保 Month 列是整数类型，避免 Plotly x 轴显示为科学计数法
        if date_col in monthly.columns:
            try:
                monthly[date_col] = monthly[date_col].astype(int)
            except (ValueError, TypeError):
                pass
        return monthly

    @staticmethod
    def growth_rate(series: pd.Series) -> pd.Series:
        """计算环比增长率 (MoM)。"""
        return series.pct_change() * 100

    @staticmethod
    def moving_average(series: pd.Series, window: int = 3) -> pd.Series:
        """移动平均 (窗口期默认为3个月)。"""
        return series.rolling(window=window, min_periods=1).mean()

    @staticmethod
    def detect_anomalies(series: pd.Series, threshold: float = 1.5) -> list[dict]:
        """使用 IQR (四分位距) 检测异常值。返回异常索引和值列表。"""
        q1 = series.quantile(0.25)
        q3 = series.quantile(0.75)
        iqr = q3 - q1
        lower = q1 - threshold * iqr
        upper = q3 + threshold * iqr
        anomalies = []
        for idx, val in series.items():
            if val < lower or val > upper:
                anomalies.append({"index": idx, "value": val, "bound": "lower" if val < lower else "upper"})
        return anomalies

    @staticmethod
    def peak_valley_analysis(series: pd.Series, labels: list | None = None) -> dict:
        """找出峰值和谷值月份。"""
        if labels is None:
            labels = list(range(len(series)))
        values = series.values
        n = len(values)
        peaks, valleys = [], []
        for i in range(1, n - 1):
            if values[i] > values[i - 1] and values[i] > values[i + 1]:
                peaks.append({"label": str(labels[i]), "value": float(values[i]), "index": int(i)})
            if values[i] < values[i - 1] and values[i] < values[i + 1]:
                valleys.append({"label": str(labels[i]), "value": float(values[i]), "index": int(i)})
        return {"peaks": peaks, "valleys": valleys}

    @staticmethod
    def build_trend_summary(monthly_df: pd.DataFrame, value_col: str = "total_revenue",
                            month_col: str = "Month") -> dict:
        """从月度数据生成趋势摘要 JSON。"""
        if monthly_df.empty or value_col not in monthly_df.columns:
            return {"trend_summary": "No data available", "growth_rate": "N/A", "anomaly_months": []}

        series = monthly_df[value_col]
        # 强制数值化:文本列或脏数据转 NaN,避免 pct_change 的 str/str 崩溃
        series = pd.to_numeric(series, errors="coerce")
        if series.dropna().empty:
            return {
                "trend_summary": f"列 {value_col} 非数值或无有效数据,无法计算趋势",
                "growth_rate": "N/A",
                "anomaly_months": [],
                "overall_growth_pct": "N/A",
            }
        labels = monthly_df[month_col].tolist()
        growth = TrendAnalysis.growth_rate(series)
        anomalies = TrendAnalysis.detect_anomalies(series)
        pv = TrendAnalysis.peak_valley_analysis(series, labels)
        ma = TrendAnalysis.moving_average(series)

        total_start = float(series.iloc[0])
        total_end = float(series.iloc[-1])
        overall_growth = ((total_end - total_start) / total_start * 100) if total_start > 0 else 0
        direction = "上升" if overall_growth > 0 else "下降"

        return {
            "trend_summary": f"整体趋势：{direction}，变化幅度 {overall_growth:.2f}%",
            "overall_growth_pct": round(overall_growth, 2),
            "direction": direction,
            "start_value": total_start,
            "end_value": total_end,
            "monthly_data": monthly_df.to_dict(orient="records"),
            "mom_growth": {str(labels[i]): round(float(growth.iloc[i]), 2) if not np.isnan(growth.iloc[i]) else None
                           for i in range(1, len(growth))},
            "anomaly_months": anomalies,
            "peaks": pv["peaks"],
            "valleys": pv["valleys"],
            "moving_average": {str(labels[i]): round(float(ma.iloc[i]), 2) for i in range(len(ma))},
        }
