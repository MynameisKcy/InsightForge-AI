"""
Data Resolver: 根据用户提示词从 data/ 目录的 .txt 描述文件中自动匹配合适的数据集。
"""

import os
import sys

CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(CURRENT_DIR)
for path in (PROJECT_ROOT, os.path.dirname(PROJECT_ROOT)):
    if path not in sys.path:
        sys.path.insert(0, path)

from utils.logger_handler import logger
from utils.path_tool import get_abs_path

# 预定义的数据集描述文件 → 数据文件映射
DATASET_MAP = {
    "About_dataset_train.txt": {
        "csv": "data/train.csv",
        "name": "Superstore Sales Dataset",
        "keywords": ["superstore", "超市", "零售", "retail", "forecast", "预测",
                     "time series", "时间序列", "EDA", "ship", "运输", "region",
                     "区域", "segment", "部门", "Sales", "Order", "订单",
                     "sales", "利润", "profit", "产品", "product", "客户", "customer",
                     "类别", "category", "销售", "趋势", "trend", "月度", "monthly"],
        "description": "全球超市 4 年零售数据集，包含订单、运输、区域、产品类别、销售额等字段。"
                       "适用于时间序列预测、区域销售分析、产品分析、趋势分析。",
        "prefer_when": ["城市", "city", "区域", "region", "州", "state", "省份",
                       "ship", "运输", "segment", "部门", "时间序列", "预测",
                       "forecast", "超市", "零售", "retail", "邮编", "postal"],
    },
}

# 默认使用 Superstore Sales Dataset
DEFAULT_DATASET = "About_dataset_train.txt"


class DataResolver:
    """根据用户查询自动选择最合适的数据集。"""

    @staticmethod
    def resolve(query: str) -> dict:
        """
        根据用户查询返回最匹配的数据集配置。
        return: {"csv_path": str, "name": str, "description": str, "matched_by": str}
        """
        query_lower = query.lower()

        # 1. 关键词匹配打分
        scores = {}
        for desc_file, info in DATASET_MAP.items():
            score = 0
            for kw in info.get("keywords", []):
                if kw.lower() in query_lower:
                    score += 1
            scores[desc_file] = score

        # 2. 找最高分
        best = max(scores, key=scores.get)
        best_score = scores[best]

        # 3. 如果最高分为 0（无关键词匹配），返回默认
        if best_score == 0:
            best = DEFAULT_DATASET
            matched_by = "default"
        else:
            # 检查是否有平局，有平局时使用 prefer_when 二次打分
            tied = [k for k, v in scores.items() if v == best_score]
            if len(tied) > 1:
                # prefer_when 加权：每命中一个 +2
                for t in tied:
                    for pw in DATASET_MAP[t].get("prefer_when", []):
                        if pw.lower() in query_lower:
                            scores[t] += 2
                # 重新选最高分
                best = max(tied, key=lambda t: scores[t])
                if scores[best] > best_score:
                    matched_by = f"prefer_when(score={scores[best]}, base={best_score})"
                else:
                    matched_by = f"keyword_match(score={scores[best]})"
            else:
                matched_by = f"keyword_match(score={best_score})"

        info = DATASET_MAP[best]
        csv_path = get_abs_path(info.get("csv", ""))

        logger.info(f"DataResolver: matched '{info['name']}' for query (method={matched_by})")
        return {
            "csv_path": csv_path,
            "name": info["name"],
            "description": info.get("description", ""),
            "matched_by": matched_by,
            "desc_file": best,
        }

    @staticmethod
    def get_all_datasets() -> list[dict]:
        """返回所有可用数据集的列表。"""
        results = []
        for desc_file, info in DATASET_MAP.items():
            results.append({
                "name": info["name"],
                "csv_path": get_abs_path(info["csv"]),
                "description": info.get("description", ""),
                "desc_file": desc_file,
            })
        return results

    @staticmethod
    def read_desc_file(desc_filename: str) -> str:
        """读取 .txt 描述文件的完整内容。"""
        desc_path = get_abs_path(f"data/{desc_filename}")
        if os.path.exists(desc_path):
            with open(desc_path, "r", encoding="utf-8") as f:
                return f.read()
        return ""
