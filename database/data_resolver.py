"""
Data Resolver: 根据用户提示词从 data/ 目录的 .txt 描述文件中自动匹配合适的数据集。
支持从 datasources_db 动态读取数据集列表，旧 DATASET_MAP 作为 fallback。
"""

import os

from utils.logger_handler import logger
from utils.path_tool import get_abs_path

# 预定义的数据集描述文件 → 数据文件映射（fallback）
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


def _import_datasources_db():
    """尝试导入 datasources_db 单例，失败返回 None。"""
    try:
        from database.datasources_db import datasources_db
        return datasources_db
    except ModuleNotFoundError:
        return None


class DataResolver:
    """根据用户查询自动选择最合适的数据集。"""

    @staticmethod
    def _load_dynamic_datasets(user_id: str | None = None) -> list[dict]:
        """从 datasources_db 动态读取数据集列表。

        Args:
            user_id: 若提供，只返回该用户拥有的数据集（跨用户隔离）。

        Returns:
            list[dict]: 每个元素包含 name, file_path, table_name, description 等字段。
            如果 datasources_db 不可用或无数据集，返回空列表。
        """
        db = _import_datasources_db()
        if db is None:
            logger.debug("DataResolver: datasources_db not available, skipping dynamic load")
            return []
        try:
            datasets = db.list_datasets(owner_user_id=user_id)
            if datasets:
                logger.info(f"DataResolver: loaded {len(datasets)} dynamic dataset(s) for user={user_id}")
            return datasets
        except Exception as e:
            logger.warning(f"DataResolver: failed to load dynamic datasets: {e}")
            return []

    @staticmethod
    def resolve(query: str, user_id: str | None = None) -> dict:
        """
        根据用户查询返回最匹配的数据集配置。仅在该用户拥有的数据集中匹配（隔离）。

        优先从 datasources_db 动态匹配；若无动态数据集则 fallback 到 DATASET_MAP 关键词打分。

        return: {
            "csv_path": str,
            "name": str,
            "description": str,
            "matched_by": str,
            "desc_file": str,
            "table_names": list[str],
            "datasets": list[dict],
        }
        """
        query_lower = query.lower()

        # --- 1. 尝试动态数据集（按 user_id 隔离）---
        dynamic_datasets = DataResolver._load_dynamic_datasets(user_id=user_id)
        if dynamic_datasets:
            # 关键词匹配：在 name、display_name（原始中文文件名）和 description 中搜索。
            # display_name 是用户能认得的名字（如「山东省经济...」），用户输入"山东"
            # 应命中它，而非只匹配安全化表名 ds_202507242126。
            # 中文无空格分词，无法靠 split 切词；改用滑动窗口 n-gram：取 display/name/desc
            # 的 2~3 字符片段，若出现在 query 中则计分（3-gram 权重高于 2-gram，避免"数据"
            # 这种泛词压过"山东"）。最终取最高分数据集，并列取第一。
            scored = []  # [(score, ds), ...]
            for ds in dynamic_datasets:
                name_lower = (ds.get("name") or "").lower()
                display_lower = (ds.get("display_name") or "").lower()
                desc_lower = (ds.get("description") or "").lower()
                score = 0
                if query_lower:
                    # 数据集名/显示名/描述的 n-gram 出现在 query 中即加分
                    for src in (display_lower, name_lower, desc_lower):
                        if not src:
                            continue
                        for n, weight in ((3, 3), (2, 1)):
                            for i in range(len(src) - n + 1):
                                if src[i:i + n] in query_lower:
                                    score += weight
                    # 英文空格场景：query 切出的英文词是数据集名子串（"sales" in "sales_2024"）
                    for w in query_lower.split():
                        if len(w) > 1 and (w in name_lower or w in display_lower or w in desc_lower):
                            score += 2
                if score > 0:
                    scored.append((score, ds))

            if scored:
                # 降序取最高分；分数相同则保持原顺序（稳定）
                scored.sort(key=lambda x: x[0], reverse=True)
                matched = [ds for _, ds in scored]
                matched_by = "dynamic_keyword_match"
            else:
                matched = dynamic_datasets
                matched_by = "dynamic_all"

            # 取第一个匹配的数据集作为主结果
            primary = matched[0]
            csv_path = get_abs_path(primary.get("file_path", ""))
            table_names = [ds.get("table_name", "transactions") for ds in matched]

            logger.info(f"DataResolver: matched '{primary['name']}' for query "
                        f"(method={matched_by}, dynamic_count={len(matched)}, "
                        f"top_score={scored[0][0] if scored else 0})")
            return {
                "csv_path": csv_path,
                "name": primary["name"],
                "description": primary.get("description", ""),
                "matched_by": matched_by,
                "desc_file": "",
                "table_names": table_names,
                "datasets": matched,
            }

        # --- 2. Fallback: 旧 DATASET_MAP 关键词打分 ---
        scores = {}
        for desc_file, info in DATASET_MAP.items():
            score = 0
            for kw in info.get("keywords", []):
                if kw.lower() in query_lower:
                    score += 1
            scores[desc_file] = score

        # 找最高分
        best = max(scores, key=scores.get)
        best_score = scores[best]

        # 如果最高分为 0（无关键词匹配），返回默认
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
            "table_names": ["transactions"],
            "datasets": [],
        }

    @staticmethod
    def get_all_datasets(user_id: str | None = None) -> list[dict]:
        """返回所有可用数据集的列表（动态 + 静态合并）。

        动态部分按 user_id 隔离，只返回该用户拥有的数据集。
        """
        results = []

        # 动态数据集
        dynamic_datasets = DataResolver._load_dynamic_datasets(user_id=user_id)
        for ds in dynamic_datasets:
            results.append({
                "name": ds.get("name", ""),
                "csv_path": get_abs_path(ds.get("file_path", "")),
                "description": ds.get("description", ""),
                "desc_file": "",
                "table_name": ds.get("table_name", ""),
                "source": "dynamic",
            })

        # 静态数据集（去重：如果 name 已在动态列表中则跳过）
        dynamic_names = {ds.get("name") for ds in dynamic_datasets}
        for desc_file, info in DATASET_MAP.items():
            if info["name"] in dynamic_names:
                continue
            results.append({
                "name": info["name"],
                "csv_path": get_abs_path(info["csv"]),
                "description": info.get("description", ""),
                "desc_file": desc_file,
                "table_name": "transactions",
                "source": "static",
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
