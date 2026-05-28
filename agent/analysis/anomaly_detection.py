"""
Anomaly Detection: 异常订单检测、利润异常、销量异常、区域异常
"""

import pandas as pd
import numpy as np


def _detect_columns(df: pd.DataFrame) -> dict:
    """自适应检测关键列名。"""
    cols = set(df.columns)
    result = {}

    qty_candidates = ["Quantity", "qty", "total_quantity", "total_qty"]
    result["qty_col"] = next((c for c in qty_candidates if c in cols), None)
    if result["qty_col"] is None:
        for c in cols:
            if any(w in c.lower() for w in ["数量", "qty", "quantity", "count"]):
                result["qty_col"] = c
                break

    price_candidates = ["Avg_Price", "avg_price", "Price", "price", "unit_price"]
    result["price_col"] = next((c for c in price_candidates if c in cols), None)
    if result["price_col"] is None:
        for c in cols:
            if any(w in c.lower() for w in ["价格", "price", "avg"]):
                result["price_col"] = c
                break

    return result


class AnomalyDetection:
    """异常检测模块：使用统计方法检测数据中的异常。"""

    @staticmethod
    def iqr_anomalies(series: pd.Series, threshold: float = 1.5) -> pd.Series:
        """基于 IQR 方法的异常值标记 (返回布尔 Series，True=异常)。"""
        q1 = series.quantile(0.25)
        q3 = series.quantile(0.75)
        iqr = q3 - q1
        lower = q1 - threshold * iqr
        upper = q3 + threshold * iqr
        return (series < lower) | (series > upper)

    @staticmethod
    def zscore_anomalies(series: pd.Series, threshold: float = 2.0) -> pd.Series:
        """基于 Z-score 的异常值标记。"""
        mean = series.mean()
        std = series.std()
        if std == 0:
            return pd.Series([False] * len(series), index=series.index)
        z_scores = (series - mean).abs() / std
        return z_scores > threshold

    @staticmethod
    def detect_revenue_anomalies(df: pd.DataFrame, group_col: str = "Month",
                                 price_col: str | None = None, qty_col: str | None = None) -> dict:
        """检测月度收入异常（按月份分组检测）。列名自适应检测。"""
        df = df.copy()
        detected = _detect_columns(df)
        qty_col = qty_col or detected.get("qty_col")
        price_col = price_col or detected.get("price_col")

        if qty_col and qty_col in df.columns and price_col and price_col in df.columns:
            df["_revenue"] = df[qty_col] * df[price_col]
        elif price_col and price_col in df.columns:
            df["_revenue"] = df[price_col]
        else:
            return {"anomaly_months": [], "monthly_revenue": {}}

        monthly = df.groupby(group_col)["_revenue"].sum().sort_index()
        if len(monthly) < 3:
            return {"anomaly_months": [], "monthly_revenue": monthly.to_dict()}

        iqr_flags = AnomalyDetection.iqr_anomalies(monthly)
        zscore_flags = AnomalyDetection.zscore_anomalies(monthly)
        anomaly_months = []
        for idx in monthly.index:
            if iqr_flags[idx] or zscore_flags[idx]:
                anomaly_months.append({
                    "month": str(idx),
                    "revenue": float(monthly[idx]),
                    "iqr_flag": bool(iqr_flags[idx]),
                    "zscore_flag": bool(zscore_flags[idx]),
                })
        return {"anomaly_months": anomaly_months, "monthly_revenue": monthly.to_dict()}

    @staticmethod
    def detect_location_anomalies(df: pd.DataFrame, location_col: str = "Location",
                                  price_col: str | None = None, qty_col: str | None = None) -> dict:
        """检测区域订单异常（按区域分组检测）。列名自适应检测。"""
        df = df.copy()
        detected = _detect_columns(df)
        qty_col = qty_col or detected.get("qty_col")
        price_col = price_col or detected.get("price_col")

        if qty_col and qty_col in df.columns and price_col and price_col in df.columns:
            df["_revenue"] = df[qty_col] * df[price_col]
        elif price_col and price_col in df.columns:
            df["_revenue"] = df[price_col]
        else:
            return {"anomaly_locations": [], "location_stats": []}

        agg_dict = {
            "total_revenue": ("_revenue", "sum"),
        }
        if qty_col and qty_col in df.columns:
            agg_dict["order_count"] = (qty_col, "count")
        else:
            agg_dict["order_count"] = ("_revenue", "count")

        location_stats = df.groupby(location_col).agg(**agg_dict).reset_index()

        if len(location_stats) < 3:
            return {"anomaly_locations": [], "location_stats": location_stats.to_dict(orient="records")}

        order_flags = AnomalyDetection.iqr_anomalies(location_stats["order_count"])
        rev_flags = AnomalyDetection.iqr_anomalies(location_stats["total_revenue"])
        anomaly_locations = []
        for i, row in location_stats.iterrows():
            if order_flags[i] or rev_flags[i]:
                anomaly_locations.append({
                    "location": str(row[location_col]),
                    "total_revenue": float(row["total_revenue"]),
                    "order_count": int(row["order_count"]),
                    "order_flag": bool(order_flags[i]),
                    "revenue_flag": bool(rev_flags[i]),
                })
        return {
            "anomaly_locations": anomaly_locations,
            "location_stats": location_stats.to_dict(orient="records"),
        }

    @staticmethod
    def detect_category_loss(df: pd.DataFrame, cat_col: str = "Product_Category",
                             price_col: str | None = None, qty_col: str | None = None) -> dict:
        """检测可能亏损严重的类别。列名自适应检测。"""
        df = df.copy()
        detected = _detect_columns(df)
        qty_col = qty_col or detected.get("qty_col")
        price_col = price_col or detected.get("price_col")

        if qty_col and qty_col in df.columns and price_col and price_col in df.columns:
            df["_revenue"] = df[qty_col] * df[price_col]
        elif price_col and price_col in df.columns:
            df["_revenue"] = df[price_col]
        else:
            return {"low_performance_categories": [], "median_revenue": 0, "category_stats": []}

        agg_dict = {"total_revenue": ("_revenue", "sum")}
        if qty_col and qty_col in df.columns:
            agg_dict["order_count"] = (qty_col, "count")
        if price_col and price_col in df.columns:
            agg_dict["avg_price"] = (price_col, "mean")

        cat_stats = df.groupby(cat_col).agg(**agg_dict).reset_index()
        if len(cat_stats) < 2:
            return {"low_performance_categories": [], "median_revenue": 0,
                    "category_stats": cat_stats.to_dict(orient="records")}

        median_rev = cat_stats["total_revenue"].median()
        cat_stats["is_low_performer"] = cat_stats["total_revenue"] < median_rev * 0.5
        low_performers = cat_stats[cat_stats["is_low_performer"]].to_dict(orient="records")
        return {
            "low_performance_categories": low_performers,
            "median_revenue": float(median_rev),
            "category_stats": cat_stats.to_dict(orient="records"),
        }

    @staticmethod
    def build_risk_summary(df: pd.DataFrame) -> dict:
        """生成综合风险分析摘要。列名自适应检测。"""
        rev_anomalies = AnomalyDetection.detect_revenue_anomalies(df)
        loc_anomalies = AnomalyDetection.detect_location_anomalies(df)
        cat_loss = AnomalyDetection.detect_category_loss(df)

        risk_level = "low"
        anomaly_count = len(rev_anomalies.get("anomaly_months", []))
        if anomaly_count >= 3:
            risk_level = "high"
        elif anomaly_count >= 1:
            risk_level = "medium"

        return {
            "risk_level": risk_level,
            "revenue_anomalies": rev_anomalies,
            "location_anomalies": loc_anomalies,
            "category_risk": cat_loss,
        }
