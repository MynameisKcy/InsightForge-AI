"""
Dimension / Breakdown Analysis: 按维度分组对比度量 -- TOP 项、分布占比、低表现项。

领域中立：销售数据（有 price+qty 列）走"收入=单价×数量"快路径；人口/流量/运营等
任意数据走通用路径（按类别列分组、对数值度量列求和）。不再对非销售数据强行 qty×price。
保留类名 ProductAnalysis / 方法名 作为遗留标识符，语义已是"分组对比分析"。
"""

import pandas as pd

# ── 维度/度量列的命名候选（仅用于"按名识别"销售快路径与常见维度列）──
_PRODUCT_CANDIDATES = ["Product_Description", "Product_Name", "product_name", "product", "name"]
_CATEGORY_CANDIDATES = ["Product_Category", "Category", "category", "product_category"]
_QTY_CANDIDATES = ["Quantity", "qty", "total_quantity", "total_qty"]
_PRICE_CANDIDATES = ["Avg_Price", "avg_price", "Price", "price", "unit_price"]
# 通用度量列候选（非销售）：优先把这些列当作"要求和的度量"
_MEASURE_CANDIDATES = [
    "revenue", "amount", "sales", "total", "value", "count",
    "population", "流量", "人数", "人口", "金额", "收入", "总量", "数量", "计数",
]


def _detect_columns(df: pd.DataFrame) -> dict:
    """自适应检测 DataFrame 中的关键列名。

    返回:
      product_col  -- TOP 项的分组维度列（类别列）
      cat_col      -- 分组占比的维度列（类别列）
      qty_col      -- 销售数量列（仅按名列匹配，不数值兜底，避免误乘）
      price_col    -- 单价列（仅按名列匹配，不数值兜底）
      measure_source -- 通用度量列（数值列），无 price/qty 时对其求和
    """
    cols = set(df.columns)
    result = {}

    # 维度列：Product_Description > Product_Name > 第一个 object 列
    result["product_col"] = next((c for c in _PRODUCT_CANDIDATES if c in cols), None)
    if result["product_col"] is None:
        obj_cols = df.select_dtypes(include=["object", "category"]).columns.tolist()
        result["product_col"] = obj_cols[0] if obj_cols else None

    # 类别列
    result["cat_col"] = next((c for c in _CATEGORY_CANDIDATES if c in cols), None)
    if result["cat_col"] is None:
        obj_cols = df.select_dtypes(include=["object", "category"]).columns.tolist()
        result["cat_col"] = obj_cols[0] if obj_cols else None

    # 数量列 / 价格列：仅按名列匹配，不做数值兜底（否则会把无关数值列误当 price/qty 相乘）
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

    # 通用度量列（数值列）：优先命名候选，否则取第一个数值列
    result["measure_source"] = next((c for c in _MEASURE_CANDIDATES if c in cols), None)
    if result["measure_source"] is None:
        # 取第一个数值列，尽量避开明显是 ID/年份的列
        num_cols = df.select_dtypes(include="number").columns.tolist()
        for c in num_cols:
            lc = c.lower()
            if any(w in lc for w in ["id", "year", "month", "date", "time", "年"]):
                continue
            result["measure_source"] = c
            break
        if result["measure_source"] is None and num_cols:
            result["measure_source"] = num_cols[0]

    return result


def _is_sales_like(detected: dict) -> bool:
    """是否走销售快路径（识别到价格列即认为是金额类数据）。"""
    return bool(detected.get("price_col"))


class ProductAnalysis:
    """分组对比分析模块（领域中立）。类名保留为 ProductAnalysis 作遗留标识符。"""

    @staticmethod
    def product_revenue(df: pd.DataFrame, product_col: str | None = None,
                        price_col: str | None = None, qty_col: str | None = None) -> pd.DataFrame:
        """按维度分组汇总度量。列名自适应检测。

        销售快路径（有 price 列）: 收入 = qty*price（或 price 本身）。
        通用路径（无 price 列）: 对检测到的数值度量列求和。
        """
        df = df.copy()
        detected = _detect_columns(df)
        product_col = product_col or detected.get("product_col") or df.columns[0]
        price_col = price_col or detected.get("price_col")
        qty_col = qty_col or detected.get("qty_col")

        has_qty = qty_col and qty_col in df.columns
        has_price = price_col and price_col in df.columns

        if has_qty and has_price:
            df["_revenue"] = df[qty_col] * df[price_col]
            agg_dict = {
                "total_revenue": ("_revenue", "sum"),
                "total_quantity": (qty_col, "sum"),
                "avg_price": (price_col, "mean"),
                "order_count": (qty_col, "count"),
            }
        elif has_price:
            df["_revenue"] = df[price_col]
            agg_dict = {
                "total_revenue": ("_revenue", "sum"),
                "avg_price": (price_col, "mean"),
                "order_count": (price_col, "count"),
            }
        else:
            # 通用路径：对度量列求和（人口/流量/运营等任意数值度量）
            measure = detected.get("measure_source")
            if measure and measure in df.columns:
                agg_dict = {
                    "total_value": (measure, "sum"),
                    "order_count": (measure, "count"),
                }
            else:
                # 纯计数模式（无数值列）
                agg_dict = {"order_count": (product_col, "count")}

        product = df.groupby(product_col).agg(**agg_dict)
        sort_col = next((c for c in ("total_revenue", "total_value", "order_count")
                         if c in product.columns), "order_count")
        product = product.sort_values(sort_col, ascending=False).reset_index()
        return product

    @staticmethod
    def category_revenue(df: pd.DataFrame, cat_col: str | None = None,
                         price_col: str | None = None, qty_col: str | None = None) -> pd.DataFrame:
        """按类别分组汇总度量（含占比）。列名自适应检测。"""
        df = df.copy()
        detected = _detect_columns(df)
        cat_col = cat_col or detected.get("cat_col") or df.columns[0]
        price_col = price_col or detected.get("price_col")
        qty_col = qty_col or detected.get("qty_col")
        product_col = detected.get("product_col")

        has_qty = qty_col and qty_col in df.columns
        has_price = price_col and price_col in df.columns

        if has_qty and has_price:
            df["_revenue"] = df[qty_col] * df[price_col]
            agg_dict = {
                "total_revenue": ("_revenue", "sum"),
                "total_quantity": (qty_col, "sum"),
                "avg_price": (price_col, "mean"),
                "order_count": (qty_col, "count"),
            }
        elif has_price:
            df["_revenue"] = df[price_col]
            agg_dict = {
                "total_revenue": ("_revenue", "sum"),
                "avg_price": (price_col, "mean"),
                "order_count": (price_col, "count"),
            }
        else:
            measure = detected.get("measure_source")
            if measure and measure in df.columns:
                agg_dict = {
                    "total_value": (measure, "sum"),
                    "order_count": (measure, "count"),
                }
            else:
                agg_dict = {"order_count": (cat_col, "count")}

        if product_col and product_col in df.columns:
            agg_dict["product_variety"] = (product_col, "nunique")

        cat = df.groupby(cat_col).agg(**agg_dict)
        sort_col = next((c for c in ("total_revenue", "total_value", "order_count")
                         if c in cat.columns), "order_count")
        cat = cat.sort_values(sort_col, ascending=False).reset_index()

        # 计算占比（基于 whichever 度量列存在）
        measure_col = sort_col
        if measure_col in cat.columns:
            total = cat[measure_col].sum()
            cat["revenue_pct"] = (cat[measure_col] / total * 100).round(2) if total > 0 else 0
        return cat

    @staticmethod
    def top_products(df: pd.DataFrame, top_n: int = 10, **kwargs) -> list[dict]:
        """返回 TOP N 项。"""
        product_df = ProductAnalysis.product_revenue(df, **kwargs)
        return product_df.head(top_n).to_dict(orient="records")

    @staticmethod
    def low_profit_products(df: pd.DataFrame, bottom_n: int = 10,
                            price_col: str | None = None, qty_col: str | None = None,
                            product_col: str | None = None) -> list[dict]:
        """返回度量最低的 N 项（销售场景即"低利润产品"，通用场景即"低表现项"）。"""
        product_df = ProductAnalysis.product_revenue(
            df, product_col=product_col, price_col=price_col, qty_col=qty_col
        )
        return product_df.tail(bottom_n).to_dict(orient="records")

    @staticmethod
    def high_quantity_low_revenue(df: pd.DataFrame, qty_threshold: float | None = None,
                                  **kwargs) -> pd.DataFrame:
        """检测高销量低利润产品（仅销售场景有意义）。"""
        product_df = ProductAnalysis.product_revenue(df, **kwargs)
        if "total_quantity" not in product_df.columns or "total_revenue" not in product_df.columns:
            return pd.DataFrame()
        qty_median = product_df["total_quantity"].median()
        rev_median = product_df["total_revenue"].median()
        mask = (product_df["total_quantity"] > (qty_threshold or qty_median)) & \
               (product_df["total_revenue"] < rev_median)
        return product_df[mask].sort_values("total_quantity", ascending=False)

    @staticmethod
    def build_product_summary(df: pd.DataFrame, top_n: int = 5) -> dict:
        """生成分组对比分析摘要 JSON。列名自适应检测，领域中立。

        输出含 dimension_col/category_col/measure_col 与 *_label 元数据，
        供报告模板用真实列名渲染表头（不再硬编码"产品/总收入/销量"）。
        """
        detected = _detect_columns(df)
        product_col = detected.get("product_col")
        cat_col = detected.get("cat_col")
        is_sales = _is_sales_like(detected)

        cat_df = ProductAnalysis.category_revenue(df)
        top = ProductAnalysis.top_products(df, top_n=top_n)
        low = ProductAnalysis.low_profit_products(df, bottom_n=top_n)

        # 度量列的记录键：销售 -> total_revenue；通用 -> total_value（按 product_revenue 实际产出）
        if is_sales:
            measure_key = "total_revenue"
            measure_label = "总收入(元)"
            dimension_label = "产品"
            category_label = "类别"
        else:
            measure_key = "total_value" if (top and "total_value" in top[0]) else "order_count"
            measure_label = detected.get("measure_source") or measure_key
            dimension_label = product_col or "维度"
            category_label = cat_col or "类别"

        result = {
            "top_products": top,
            "low_profit_products": low,
            "category_summary": cat_df.to_dict(orient="records"),
            "top_category": cat_df.iloc[0].to_dict() if not cat_df.empty else {},
            "category_count": len(cat_df),
            "total_products": int(df[product_col].nunique()) if product_col and product_col in df.columns else 0,
            # 报告元数据：模板用 p[dimension_col] / p[measure_col] 取值，用 *_label 做表头
            "dimension_col": product_col,
            "category_col": cat_col,
            "measure_col": measure_key,
            "dimension_label": dimension_label,
            "category_label": category_label,
            "measure_label": measure_label,
            "is_sales": is_sales,
        }
        return result
