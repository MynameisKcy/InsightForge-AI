"""
Product Analysis: 产品销量/利润分析、TOP产品、低利润产品、类别分析
"""

import pandas as pd
import numpy as np


def _detect_columns(df: pd.DataFrame) -> dict:
    """自适应检测 DataFrame 中的关键列名。
    返回 {"product_col": str, "price_col": str, "qty_col": str, "cat_col": str}
    """
    cols = set(df.columns)
    result = {}

    # 产品列：Product_Description > Product_Name > 第一个 object 列
    prod_candidates = ["Product_Description", "Product_Name", "product_name", "product", "name"]
    result["product_col"] = next((c for c in prod_candidates if c in cols), None)
    if result["product_col"] is None:
        obj_cols = df.select_dtypes(include=["object", "category"]).columns.tolist()
        result["product_col"] = obj_cols[0] if obj_cols else None

    # 数量列：Quantity > qty > total_quantity > 含"数量"或"qty"或"count"的列 > 第二个数字列
    qty_candidates = ["Quantity", "qty", "total_quantity", "total_qty"]
    result["qty_col"] = next((c for c in qty_candidates if c in cols), None)
    if result["qty_col"] is None:
        for c in cols:
            if any(w in c.lower() for w in ["数量", "qty", "quantity", "count"]):
                result["qty_col"] = c
                break
    if result["qty_col"] is None:
        num_cols = df.select_dtypes(include=["number"]).columns.tolist()
        if len(num_cols) >= 2:
            result["qty_col"] = num_cols[1]
        elif num_cols:
            result["qty_col"] = num_cols[0]

    # 价格列：Avg_Price > price > unit_price > 含"价格"或"price"的列 > 第一个数字列
    price_candidates = ["Avg_Price", "avg_price", "Price", "price", "unit_price"]
    result["price_col"] = next((c for c in price_candidates if c in cols), None)
    if result["price_col"] is None:
        for c in cols:
            if any(w in c.lower() for w in ["价格", "price", "avg"]):
                result["price_col"] = c
                break
    if result["price_col"] is None:
        num_cols = df.select_dtypes(include=["number"]).columns.tolist()
        result["price_col"] = num_cols[0] if num_cols else None

    # 类别列
    cat_candidates = ["Product_Category", "Category", "category", "product_category"]
    result["cat_col"] = next((c for c in cat_candidates if c in cols), None)
    if result["cat_col"] is None:
        obj_cols = df.select_dtypes(include=["object", "category"]).columns.tolist()
        result["cat_col"] = obj_cols[0] if obj_cols else None

    return result


class ProductAnalysis:
    """产品维度的数据分析模块。"""

    @staticmethod
    def product_revenue(df: pd.DataFrame, product_col: str | None = None,
                        price_col: str | None = None, qty_col: str | None = None) -> pd.DataFrame:
        """按产品汇总收入。列名自适应检测。"""
        df = df.copy()
        detected = _detect_columns(df)
        product_col = product_col or detected.get("product_col") or df.columns[0]
        price_col = price_col or detected.get("price_col")
        qty_col = qty_col or detected.get("qty_col")

        # 如果缺少数量列，用计数替代
        if qty_col and qty_col not in df.columns:
            qty_col = detected.get("qty_col")
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
                "order_count": (price_col, "count"),
            }
        else:
            # 纯计数模式
            agg_dict = {"order_count": (product_col, "count")}

        product = df.groupby(product_col).agg(**agg_dict)
        sort_col = "total_revenue" if "total_revenue" in product.columns else "order_count"
        product = product.sort_values(sort_col, ascending=False).reset_index()
        return product

    @staticmethod
    def category_revenue(df: pd.DataFrame, cat_col: str | None = None,
                         price_col: str | None = None, qty_col: str | None = None) -> pd.DataFrame:
        """按类别汇总收入。列名自适应检测。"""
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
            agg_dict = {"order_count": (cat_col, "count")}

        if product_col and product_col in df.columns:
            agg_dict["product_variety"] = (product_col, "nunique")

        cat = df.groupby(cat_col).agg(**agg_dict)
        sort_col = "total_revenue" if "total_revenue" in cat.columns else "order_count"
        cat = cat.sort_values(sort_col, ascending=False).reset_index()

        # 计算收入占比
        if "total_revenue" in cat.columns:
            total = cat["total_revenue"].sum()
            cat["revenue_pct"] = (cat["total_revenue"] / total * 100).round(2) if total > 0 else 0
        return cat

    @staticmethod
    def top_products(df: pd.DataFrame, top_n: int = 10, **kwargs) -> list[dict]:
        """返回 TOP N 产品。"""
        product_df = ProductAnalysis.product_revenue(df, **kwargs)
        return product_df.head(top_n).to_dict(orient="records")

    @staticmethod
    def low_profit_products(df: pd.DataFrame, bottom_n: int = 10,
                            price_col: str | None = None, qty_col: str | None = None,
                            product_col: str | None = None) -> list[dict]:
        """返回收入最低的 N 个产品。"""
        product_df = ProductAnalysis.product_revenue(
            df, product_col=product_col, price_col=price_col, qty_col=qty_col
        )
        return product_df.tail(bottom_n).to_dict(orient="records")

    @staticmethod
    def high_quantity_low_revenue(df: pd.DataFrame, qty_threshold: float | None = None,
                                  **kwargs) -> pd.DataFrame:
        """检测高销量低利润产品。"""
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
        """生成产品分析摘要 JSON。列名自适应检测。"""
        detected = _detect_columns(df)
        product_col = detected.get("product_col")
        cat_df = ProductAnalysis.category_revenue(df)
        top = ProductAnalysis.top_products(df, top_n=top_n)
        low = ProductAnalysis.low_profit_products(df, bottom_n=top_n)

        result = {
            "top_products": top,
            "low_profit_products": low,
            "category_summary": cat_df.to_dict(orient="records"),
            "top_category": cat_df.iloc[0].to_dict() if not cat_df.empty else {},
            "category_count": len(cat_df),
            "total_products": int(df[product_col].nunique()) if product_col and product_col in df.columns else 0,
        }
        return result
