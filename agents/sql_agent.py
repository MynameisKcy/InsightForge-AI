"""
SQL Agent: 自然语言 → SQL → DataFrame
"""

import json
import re

import pandas as pd

from agents.base import BaseAgent
from database.duckdb_manager import DuckDBManager, init_duckdb
from utils.logger_handler import logger


SQL_AGENT_SYSTEM_PROMPT = """你是一个专业的 SQL 生成助手。根据用户的数据分析需求和数据库 Schema，生成可执行的 DuckDB SQL 语句。

## 规则
1. **严格使用下面 Schema 中列出的列名和表名** —— 不要编造任何 Schema 中不存在的列名或表名。
2. 只输出 SQL 语句，放在 ```sql 代码块中。
3. SQL 必须完整、可直接执行，不要使用占位符。
4. 使用双引号引用列名（如果列名包含空格或特殊字符）。
5. 对于聚合查询，确保 GROUP BY 包含所有非聚合列。
6. 如果用户没有指定 LIMIT，默认添加 LIMIT 100。
7. 使用 DuckDB 兼容的 SQL 语法。
8. **只生成 SELECT 查询** —— 禁止生成 DROP/CREATE/INSERT/UPDATE/DELETE/ATTACH/ALTER/TRUNCATE 等任何写操作或 DDL 语句。
9. **跨表查询时请使用标准 SQL JOIN** —— 系统支持跨数据集关联分析。

10. **列名已自动清洗**（HTML 实体/不可见字符/全角已归一），可直接使用 Schema 中的列名。若列名语义不明确，依据 Schema 提供的列统计（取值 values / nunique / min-max）推断该列含义（维度列还是度量列），**不要要求用户清洗或重传数据**。

## 数据库 Schema
{schema}

## 重要提示
- 仔细阅读上述 Schema 中的表名和列名。
- 只能使用 Schema 中实际存在的表名和列名来编写 SQL。
- 如果 Schema 中有 "Product Name" 列，请用双引号引用为 "Product Name"。
- 不要假设存在 Schema 中未出现的列名。
- 如果用户的问题涉及多个表，请使用 JOIN 关联查询。
- 列名含义不清时，结合列取值与统计推断（取值为月份/年龄段/类别则是维度列，含 min-max 数值则是度量列）。

请根据用户需求生成 SQL："""


class SQLAgent(BaseAgent):
    """自然语言转 SQL 并执行，返回 DataFrame。"""

    name = "sql_agent"

    def __init__(self, csv_path: str | None = None, user_id: str = "default", model=None):
        super().__init__(model=model)
        # 不在构造时绑定全局单例 db；改为按 user_id 获取独立实例，保证多用户隔离。
        # 保留 csv_path 参数仅为向后兼容；实际数据层隔离由 user_id 决定。
        self._default_user_id = user_id
        self.db = init_duckdb(csv_path, user_id=user_id)

    def run(self, input_data: dict) -> dict:
        """
        input_data = {
            "task": "查询2020年每个月的销售额",
            "table_name": "transactions",  # optional, default "transactions"
            "user_id": "u_alice_...",        # optional, 决定使用哪个用户的 DuckDB 实例
        }
        returns: {"sql": str, "dataframe_json": str, "row_count": int, "error": str | None}

        SQL 执行失败时，会把错误信息 + 原 SQL + 原 task 回灌 LLM 重新生成，
        最多重试 MAX_SQL_RETRIES 次（防止死循环）。
        """
        task = input_data.get("task", "")
        table_name = input_data.get("table_name", "transactions")
        # 按 user_id 切换到该用户的独立 DuckDB 实例（多用户隔离）
        user_id = input_data.get("user_id") or self._default_user_id or "default"
        self.db = init_duckdb(user_id=user_id)

        if not task:
            return {"error": "No task provided", "dataframe_json": "[]", "row_count": 0, "sql": ""}

        # 生成 SQL
        sql = self._generate_sql(task, table_name)
        if not sql:
            return {"error": "Failed to generate SQL", "dataframe_json": "[]", "row_count": 0, "sql": ""}

        # 执行 SQL，失败则把错误回灌 LLM 重新生成并重试
        max_retries = 2
        current_sql = sql
        last_error = None
        for attempt in range(max_retries + 1):
            try:
                df = self.db.query_df(current_sql)
                logger.info(f"SQLAgent executed query (attempt {attempt}), got {len(df)} rows")
                return {
                    "sql": current_sql,
                    "dataframe_json": df.to_json(orient="records", force_ascii=False),
                    "row_count": len(df),
                    "error": None,
                }
            except Exception as e:
                last_error = e
                logger.error(f"SQL execution failed (attempt {attempt}): {e}")
                if attempt >= max_retries:
                    break
                # 把错误信息回灌 LLM 重新生成 SQL
                fixed_sql = self._fix_sql(current_sql, str(e), task, table_name)
                if not fixed_sql or fixed_sql == current_sql:
                    # LLM 未产出新 SQL，放弃
                    break
                current_sql = fixed_sql
                logger.info(f"Retrying with LLM-regenerated SQL: {current_sql[:200]}")

        return {"error": str(last_error), "dataframe_json": "[]", "row_count": 0, "sql": current_sql}

    def _generate_sql(self, task: str, table_name: str = "", fix_hint: dict | None = None) -> str:
        """使用 LLM 生成 SQL。若提供 fix_hint（上一轮错误信息），则要求 LLM 据此修正。"""
        schema_text = self.db.get_enhanced_schema_text()
        prompt = SQL_AGENT_SYSTEM_PROMPT.format(schema=schema_text)
        if fix_hint:
            user_content = (
                f"之前生成的 SQL 执行失败，请根据错误信息修正后重新生成 SQL。\n"
                f"用户需求：\n{task}\n\n"
                f"之前生成的 SQL：\n```sql\n{fix_hint['sql']}\n```\n\n"
                f"执行错误：\n{fix_hint['error']}\n\n"
                f"请修正上述错误，只输出修正后的 SQL 代码块。常见原因：列名错误（请严格用 Schema 中的列名）、"
                f"列名含空格需双引号、DuckDB 语法不兼容等。"
            )
        else:
            user_content = f"请为以下需求生成 SQL：\n{task}"
        messages = [
            {"role": "system", "content": prompt},
            {"role": "user", "content": user_content},
        ]
        response = self._call_llm(messages)
        return self._extract_sql(response)

    def _extract_sql(self, text: str) -> str:
        """从 LLM 响应中提取 SQL 语句。"""
        # 尝试从代码块中提取
        match = re.search(r"```sql\s*([\s\S]*?)```", text)
        if match:
            return match.group(1).strip()
        # 尝试匹配 SELECT 开头的语句
        match = re.search(r"(SELECT[\s\S]*?);", text, re.IGNORECASE)
        if match:
            return match.group(1).strip() + ";"
        match = re.search(r"(SELECT[\s\S]*)", text, re.IGNORECASE)
        if match:
            sql = match.group(1).strip()
            if not sql.endswith(";"):
                sql += ";"
            return sql
        return text.strip()

    def _fix_sql(self, sql: str, error_msg: str, task: str, table_name: str) -> str:
        """把错误信息 + 原 SQL + 原 task 回灌 LLM，重新生成修正后的 SQL。

        若 LLM 未产出新 SQL 或返回与原 SQL 相同，则返回空串表示放弃。
        """
        if not error_msg:
            return ""
        try:
            fixed = self._generate_sql(
                task, table_name,
                fix_hint={"sql": sql, "error": error_msg},
            )
        except Exception as e:
            logger.error(f"_fix_sql LLM regeneration failed: {e}")
            return ""
        # 兜底：反引号→双引号（保留旧逻辑的快速修正能力）
        if fixed and "`" in fixed:
            fixed = fixed.replace("`", '"')
        if not fixed or fixed.strip() == sql.strip():
            logger.warning("_fix_sql: LLM 未产出有效的新 SQL")
            return ""
        return fixed

    def query_direct(self, sql: str) -> pd.DataFrame:
        """直接执行 SQL 查询（绕过 LLM），用于已知 SQL 的场景。"""
        return self.db.query_df(sql)
