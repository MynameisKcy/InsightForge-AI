"""
Chart Knowledge Base: 存储图表元数据和分析结果，作为 RAG 内部参考资料。
图表生成时自动存储，后续分析可检索历史图表数据并结合外部搜索生成建议。

Owner 隔离（chart-knowledge-isolation spec）：
- 写入记录归属（显式参数 > 请求上下文 contextvars > "default"）；
- 检索仅返回 当前用户 + 公共 system 的记录，他人记录不可见；
- 迁移前无归属字段的存量行统一归属 system，作为对所有用户可见的公共参考
  （语义与先例 rag/vector_store.py 的 PUBLIC_OWNER / _migrate_legacy_owner 一致）。
"""

import json
import os
import sqlite3
from datetime import datetime, timedelta

from utils.logger_handler import logger
from utils.path_tool import get_abs_path
from utils.request_context import get_user_id

CHART_DB_PATH = get_abs_path("database/chart_knowledge.db")

# 公共 owner：存量迁移行与公共图表知识归属 system，对所有用户可见
PUBLIC_OWNER = "system"
# 无请求上下文时的归属兜底（与 DuckDB 实例的 default 用户语义对齐）
_DEFAULT_OWNER = "default"


def _ensure_db():
    os.makedirs(os.path.dirname(CHART_DB_PATH), exist_ok=True)


class ChartKnowledgeBase:
    """图表知识库：存储图表元数据、数据摘要和分析文案，支持按 owner 隔离检索。"""

    def __init__(self, db_path: str | None = None):
        self.db_path = db_path or CHART_DB_PATH
        _ensure_db()
        self._init_tables()

    def _get_conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    # ── owner 解析：显式参数 > 请求上下文(contextvars) > default ──
    # @tool 工具签名不宜暴露 user_id（会进 LLM 工具 schema），靠请求上下文
    # 自动解析（见 utils/request_context.py 设计说明）。
    @staticmethod
    def _resolve_user(user_id: str | None) -> str:
        if user_id:
            return user_id
        try:
            return get_user_id() or _DEFAULT_OWNER
        except Exception:
            return _DEFAULT_OWNER

    def _init_tables(self):
        with self._get_conn() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS chart_archive (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    chart_type TEXT NOT NULL,
                    title TEXT NOT NULL,
                    x_col TEXT,
                    y_col TEXT,
                    data_summary TEXT,
                    analysis_text TEXT,
                    chart_path TEXT,
                    task_context TEXT,
                    owner_user_id TEXT NOT NULL DEFAULT 'system',
                    created_at TEXT NOT NULL
                )
            """)
            # 旧库迁移：无 owner_user_id 列时补列，存量行经 DEFAULT 归公共 system。
            # 幂等：列已存在时 SQLite 报 duplicate column，静默忽略（不算失败）。
            try:
                conn.execute(
                    "ALTER TABLE chart_archive ADD COLUMN owner_user_id "
                    "TEXT NOT NULL DEFAULT 'system'"
                )
                logger.info("chart_knowledge 迁移：已为存量图表补充 owner_user_id（公共 system）")
            except sqlite3.OperationalError as e:
                if "duplicate column" not in str(e).lower():
                    logger.warning(f"chart_knowledge owner 列迁移失败（可忽略）: {e}")
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_chart_owner
                ON chart_archive(owner_user_id, created_at DESC)
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_chart_type
                ON chart_archive(chart_type, created_at DESC)
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_chart_title
                ON chart_archive(title, created_at DESC)
            """)
            conn.commit()

    def save_chart(self, chart_info: dict, user_id: str | None = None) -> int:
        """保存图表信息到知识库，记录归属 owner（显式参数 > 请求上下文 > default）。
        chart_info = {
            "chart_type": "line",
            "title": "月度销售趋势",
            "x_col": "Month",
            "y_col": "total_revenue",
            "data_summary": "1-12月，月均销售额约XX",
            "analysis_text": "整体呈上升趋势，6月和11月为峰值...",
            "chart_path": "/reports/charts/trend_xxx.html",
            "task_context": "分析各月销售趋势",
        }
        """
        uid = self._resolve_user(user_id)
        with self._get_conn() as conn:
            cursor = conn.execute(
                """INSERT INTO chart_archive
                   (chart_type, title, x_col, y_col, data_summary, analysis_text,
                    chart_path, task_context, owner_user_id, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    chart_info.get("chart_type", ""),
                    chart_info.get("title", ""),
                    chart_info.get("x_col", ""),
                    chart_info.get("y_col", ""),
                    chart_info.get("data_summary", ""),
                    chart_info.get("analysis_text", ""),
                    chart_info.get("chart_path", ""),
                    chart_info.get("task_context", ""),
                    uid,
                    datetime.now().isoformat(),
                ),
            )
            conn.commit()
            chart_id = cursor.lastrowid
            logger.info(f"ChartKnowledge: saved chart #{chart_id} - {chart_info.get('title')} (owner={uid})")
            return chart_id

    def search_by_keywords(self, keywords: str, limit: int = 5,
                           user_id: str | None = None) -> list[dict]:
        """按关键词搜索相关图表（在 title, analysis_text, task_context 中匹配）。

        owner 隔离：仅返回当前用户 + 公共 system 的记录。
        中文查询用 jieba 搜索引擎模式分词，比按空格 split 多召回若干 term，
        显著提升「销售下降原因分析」这类无空格中文查询的 LIKE 命中率。
        """
        uid = self._resolve_user(user_id)
        try:
            import jieba
            tokens = jieba.cut_for_search(keywords)
        except ImportError:
            tokens = keywords.split()
        terms = [f"%{kw.strip()}%" for kw in tokens if kw.strip()]
        if not terms:
            return self.get_recent_charts(limit=limit, user_id=uid)

        with self._get_conn() as conn:
            # 构建 OR 查询（owner 过滤与关键词命中 AND 组合）
            conditions = " OR ".join(
                ["title LIKE ? OR analysis_text LIKE ? OR task_context LIKE ? OR data_summary LIKE ?"]
                * len(terms)
            )
            params = []
            for t in terms:
                params.extend([t, t, t, t])
            rows = conn.execute(
                f"""SELECT * FROM chart_archive
                    WHERE ({conditions}) AND (owner_user_id = ? OR owner_user_id = ?)
                    ORDER BY created_at DESC LIMIT ?""",
                (*params, uid, PUBLIC_OWNER, limit),
            ).fetchall()
            return [dict(r) for r in rows]

    def search_by_type(self, chart_type: str, limit: int = 5,
                       user_id: str | None = None) -> list[dict]:
        """按图表类型检索历史图表（owner 隔离：仅自己 + 公共 system）。"""
        uid = self._resolve_user(user_id)
        with self._get_conn() as conn:
            rows = conn.execute(
                """SELECT * FROM chart_archive
                   WHERE chart_type = ? AND (owner_user_id = ? OR owner_user_id = ?)
                   ORDER BY created_at DESC LIMIT ?""",
                (chart_type, uid, PUBLIC_OWNER, limit),
            ).fetchall()
            return [dict(r) for r in rows]

    def get_recent_charts(self, limit: int = 10,
                          user_id: str | None = None) -> list[dict]:
        """获取最近的图表记录（owner 隔离：仅自己 + 公共 system）。"""
        uid = self._resolve_user(user_id)
        with self._get_conn() as conn:
            rows = conn.execute(
                """SELECT * FROM chart_archive
                   WHERE owner_user_id = ? OR owner_user_id = ?
                   ORDER BY created_at DESC LIMIT ?""",
                (uid, PUBLIC_OWNER, limit),
            ).fetchall()
            return [dict(r) for r in rows]

    def get_chart_context_for_rag(self, query: str, max_charts: int = 5,
                                  user_id: str | None = None) -> str:
        """为 RAG 检索提供历史图表上下文（owner 隔离：仅自己 + 公共 system）。

        user_id 缺省时从请求上下文解析——@tool 工具签名不宜暴露 user_id
        （会进 LLM 工具 schema），contextvars 在调用链内自然传递。
        """
        uid = self._resolve_user(user_id)
        charts = self.search_by_keywords(query, limit=max_charts, user_id=uid)
        if not charts:
            charts = self.get_recent_charts(limit=max_charts, user_id=uid)

        if not charts:
            return "暂无历史图表参考数据。"

        lines = ["## 历史图表参考（内部知识库）", ""]
        for i, c in enumerate(charts, 1):
            lines.append(f"### {i}. {c['title']} ({c['chart_type']}图)")
            if c.get("x_col") or c.get("y_col"):
                cols = ", ".join(filter(None, [c.get("x_col"), c.get("y_col")]))
                lines.append(f"- 数据列: {cols}")
            if c.get("data_summary"):
                lines.append(f"- 数据摘要: {c['data_summary']}")
            if c.get("analysis_text"):
                lines.append(f"- 分析结论: {c['analysis_text']}")
            if c.get("task_context"):
                lines.append(f"- 任务背景: {c['task_context']}")
            lines.append("")
        return "\n".join(lines)

    def build_insight_prompt(self, query: str, chart_specs: list[dict],
                             user_id: str | None = None) -> str:
        """生成分析建议的提示词，结合历史图表数据和外部搜索建议（owner 隔离）。"""
        # 获取相关历史图表（当前用户 + 公共 system）
        chart_context = self.get_chart_context_for_rag(query, max_charts=5, user_id=user_id)

        # 汇总当前要生成的图表
        current_charts = []
        for spec in chart_specs:
            current_charts.append(
                f"- {spec.get('chart_type', 'bar')} 图: {spec.get('title', '未命名')}"
                f" (x={spec.get('x_col', '?')}, y={spec.get('y_col', '?')})"
            )

        prompt = f"""你是一个数据分析顾问。根据用户需求和历史图表数据，生成针对性的分析建议。

## 用户需求
{query}

## 本次将生成的图表
{chr(10).join(current_charts) if current_charts else '暂无'}

## 历史图表参考（来自内部知识库）
{chart_context}

## 要求
请基于历史数据和当前需求，提供 3-5 条具体的分析建议：
1. 应重点关注哪些指标或维度
2. 可能发现的问题或趋势
3. 建议采取的行动或进一步分析方向

输出简洁的要点列表，每条不超过 50 字。
"""
        return prompt

    def clear_old_data(self, days: int = 90, user_id: str | None = None):
        """清理超过指定天数的旧图表数据（仅当前用户 + 公共 system 的过期行，不动他人）。"""
        uid = self._resolve_user(user_id)
        cutoff = (datetime.now() - timedelta(days=days)).isoformat()
        with self._get_conn() as conn:
            conn.execute(
                "DELETE FROM chart_archive WHERE created_at < ? "
                "AND (owner_user_id = ? OR owner_user_id = ?)",
                (cutoff, uid, PUBLIC_OWNER),
            )
            conn.commit()


# 全局实例
chart_knowledge = ChartKnowledgeBase()
