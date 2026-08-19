"""
Anomaly Detection: 度量异常检测、分组异常、低表现项检测（领域中立）。

销售数据（有 price+qty 列）走"收入=单价×数量"快路径；人口/流量/运营等任意数据
对检测到的数值度量列做 IQR / Z-score 异常检测。不再对非销售数据强行 qty×price。
"""

import pandas as pd

_QTY_CANDIDATES = ["Quantity", "qty", "total_quantity", "total_qty"]
_PRICE_CANDIDATES = ["Avg_Price", "avg_price", "Price", "price", "unit_price"]
_MEASURE_CANDIDATES = [
    "revenue", "amount", "sales", "total", "value", "count",
    "population", "流量", "人数", "人口", "金额", "收入", "总量", "数量", "计数",
]


def _detect_columns(df: pd.DataFrame) -> dict:
    """自适应检测关键列名（price/qty 仅按名列匹配，不数值兜底）。"""
    cols = set(df.columns)
    result = {}

    result["qty_col"] = next((c for c in _QTY_CANDIDATES if c in cols), None)
    if result["qty_col"] is None:
        for c in cols:
            if any(w in c.lower() for w in ["数量", "qty", "quantity"]):
                result["qty_col"] = c
                break

    result["price_col"] = next((c for c in _PRICE_CANDIDATES if c in cols), None)
    if result["price_col"] is None:
        for c in cols:
            if any(w in c.lower() for w in ["价格", "price", "avg"]):
                result["price_col"] = c
                break

    result["measure_source"] = next((c for c in _MEASURE_CANDIDATES if c in cols), None)
    if result["measure_source"] is None:
        num_cols = df.select_dtypes(include="number").columns.tolist()
        for c in num_cols:
            lc = c.lower()
            if any(w in lc for w in ["id", "year", "date", "time", "年"]):
                continue
            result["measure_source"] = c
            break
        if result["measure_source"] is None and num_cols:
            result["measure_source"] = num_cols[0]

    return result


def _resolve_measure_series(df: pd.DataFrame, group_col: str) -> pd.Series | None:
    """按 group_col 分组求和一条度量序列。

    优先级：qty*price（销售）> price > 通用度量列。无可用度量返回 None。
    """
    detected = _detect_columns(df)
    qty_col = detected.get("qty_col")
    price_col = detected.get("price_col")
    has_qty = qty_col and qty_col in df.columns
    has_price = price_col and price_col in df.columns

    df = df.copy()
    if has_qty and has_price:
        df["_measure"] = df[qty_col] * df[price_col]
    elif has_price:
        df["_measure"] = df[price_col]
    else:
        measure = detected.get("measure_source")
        if not measure or measure not in df.columns:
            return None
        df["_measure"] = df[measure]

    return df.groupby(group_col)["_measure"].sum().sort_index()


def _pick_time_col(df: pd.DataFrame) -> str | None:
    """优先选时间/月份列作为时序异常的分组列。"""
    for c in df.columns:
        lc = c.lower()
        if any(w in lc for w in ["month", "date", "time", "月份", "日期", "时间"]):
            return c
    return None


def _pick_cat_col(df: pd.DataFrame, exclude: str | None = None) -> str | None:
    """选第一个类别列（排除指定列）作为分组异常的分组列。"""
    obj_cols = df.select_dtypes(include=["object", "category"]).columns.tolist()
    for c in obj_cols:
        if c != exclude:
            return c
    return obj_cols[0] if obj_cols else None


class AnomalyDetection:
    """异常检测模块：使用统计方法检测数据中的异常（领域中立）。"""

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
    def detect_measure_anomalies(df: pd.DataFrame, group_col: str | None = None) -> dict:
        """检测度量时序异常（按时间列分组检测）。列名自适应检测。

        销售场景即"收入异常月份"，通用场景即"度量异常时段"。
        """
        df = df.copy()
        if group_col is None or group_col not in df.columns:
            group_col = _pick_time_col(df)
        if group_col is None:
            return {"anomaly_months": [], "monthly_values": {}}

        series = _resolve_measure_series(df, group_col)
        if series is None or len(series) < 3:
            return {"anomaly_months": [], "monthly_values": (series.to_dict() if series is not None else {})}

        iqr_flags = AnomalyDetection.iqr_anomalies(series)
        zscore_flags = AnomalyDetection.zscore_anomalies(series)
        anomaly_months = []
        for idx in series.index:
            if iqr_flags[idx] or zscore_flags[idx]:
                anomaly_months.append({
                    "month": str(idx),
                    "value": float(series[idx]),
                    "iqr_flag": bool(iqr_flags[idx]),
                    "zscore_flag": bool(zscore_flags[idx]),
                })
        return {"anomaly_months": anomaly_months, "monthly_values": series.to_dict()}

    @staticmethod
    def detect_group_anomalies(df: pd.DataFrame, group_col: str | None = None,
                               price_col: str | None = None, qty_col: str | None = None) -> dict:
        """检测分组异常（按类别列分组检测）。列名自适应检测。

        销售场景即"区域订单异常"，通用场景即"某维度分组异常"。
        """
        df = df.copy()
        if group_col is None or group_col not in df.columns:
            # 排除时间列，选一个类别列
            group_col = _pick_cat_col(df, exclude=_pick_time_col(df))
        if group_col is None:
            return {"anomaly_groups": [], "group_stats": []}

        detected = _detect_columns(df)
        qty_col = qty_col or detected.get("qty_col")
        price_col = price_col or detected.get("price_col")
        has_qty = qty_col and qty_col in df.columns
        has_price = price_col and price_col in df.columns

        if has_qty and has_price:
            df["_measure"] = df[qty_col] * df[price_col]
        elif has_price:
            df["_measure"] = df[price_col]
        else:
            measure = detected.get("measure_source")
            if not measure or measure not in df.columns:
                return {"anomaly_groups": [], "group_stats": []}
            df["_measure"] = df[measure]

        agg_dict = {"total_value": ("_measure", "sum"), "order_count": ("_measure", "count")}
        group_stats = df.groupby(group_col).agg(**agg_dict).reset_index()

        if len(group_stats) < 3:
            return {"anomaly_groups": [], "group_stats": group_stats.to_dict(orient="records")}

        order_flags = AnomalyDetection.iqr_anomalies(group_stats["order_count"])
        val_flags = AnomalyDetection.iqr_anomalies(group_stats["total_value"])
        anomaly_groups = []
        for i, row in group_stats.iterrows():
            if order_flags[i] or val_flags[i]:
                anomaly_groups.append({
                    "group": str(row[group_col]),
                    "total_value": float(row["total_value"]),
                    "order_count": int(row["order_count"]),
                    "order_flag": bool(order_flags[i]),
                    "value_flag": bool(val_flags[i]),
                })
        return {
            "anomaly_groups": anomaly_groups,
            "group_stats": group_stats.to_dict(orient="records"),
        }

    @staticmethod
    def detect_low_performers(df: pd.DataFrame, cat_col: str | None = None,
                              price_col: str | None = None, qty_col: str | None = None) -> dict:
        """检测低表现分组（度量显著低于中位数的分组）。列名自适应检测。

        销售场景即"亏损类别"，通用场景即"低表现分组"。
        """
        df = df.copy()
        if cat_col is None or cat_col not in df.columns:
            cat_col = _pick_cat_col(df, exclude=_pick_time_col(df)) or df.columns[0]

        detected = _detect_columns(df)
        qty_col = qty_col or detected.get("qty_col")
        price_col = price_col or detected.get("price_col")
        has_qty = qty_col and qty_col in df.columns
        has_price = price_col and price_col in df.columns

        if has_qty and has_price:
            df["_measure"] = df[qty_col] * df[price_col]
        elif has_price:
            df["_measure"] = df[price_col]
        else:
            measure = detected.get("measure_source")
            if not measure or measure not in df.columns:
                return {"low_performance_groups": [], "median_value": 0, "group_stats": []}
            df["_measure"] = df[measure]

        agg_dict = {"total_value": ("_measure", "sum"), "order_count": ("_measure", "count")}
        if has_price:
            agg_dict["avg_price"] = (price_col, "mean")

        group_stats = df.groupby(cat_col).agg(**agg_dict).reset_index()
        if len(group_stats) < 2:
            return {"low_performance_groups": [], "median_value": 0,
                    "group_stats": group_stats.to_dict(orient="records")}

        median_val = group_stats["total_value"].median()
        group_stats["is_low_performer"] = group_stats["total_value"] < median_val * 0.5
        low_performers = group_stats[group_stats["is_low_performer"]].to_dict(orient="records")
        return {
            "low_performance_groups": low_performers,
            "median_value": float(median_val),
            "group_stats": group_stats.to_dict(orient="records"),
        }

    @staticmethod
    def build_risk_summary(df: pd.DataFrame) -> dict:
        """生成综合风险分析摘要。列名自适应检测，领域中立。"""
        measure_anomalies = AnomalyDetection.detect_measure_anomalies(df)
        group_anomalies = AnomalyDetection.detect_group_anomalies(df)
        low_performers = AnomalyDetection.detect_low_performers(df)

        risk_level = "low"
        anomaly_count = len(measure_anomalies.get("anomaly_months", []))
        if anomaly_count >= 3:
            risk_level = "high"
        elif anomaly_count >= 1:
            risk_level = "medium"

        return {
            "risk_level": risk_level,
            "measure_anomalies": measure_anomalies,
            "group_anomalies": group_anomalies,
            "low_performers": low_performers,
        }
