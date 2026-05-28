"""
SQL Agent: 自然语言 → SQL → DataFrame
"""

import json
import os
import re
import sys

import pandas as pd

CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(CURRENT_DIR)
for path in (PROJECT_ROOT, os.path.dirname(PROJECT_ROOT)):
    if path not in sys.path:
        sys.path.insert(0, path)

from agents.base import BaseAgent
from database.duckdb_manager import DuckDBManager, init_duckdb
from utils.logger_handler import logger


SQL_AGENT_SYSTEM_PROMPT = """你是一个专业的 SQL 生成助手。根据用户的数据分析需求和数据库 Schema，生成可执行的 DuckDB SQL 语句。

## 规则
1. **严格使用下面 Schema 中列出的列名** —— 不要编造任何 Schema 中不存在的列名。
2. 只输出 SQL 语句，放在 ```sql 代码块中。
3. SQL 必须完整、可直接执行，不要使用占位符。
4. 使用双引号引用列名（如果列名包含空格或特殊字符）。
5. 对于聚合查询，确保 GROUP BY 包含所有非聚合列。
6. 如果用户没有指定 LIMIT，默认添加 LIMIT 100。
7. 使用 DuckDB 兼容的 SQL 语法。

## 数据库 Schema
{schema}

## 当前表
当前数据库中可查询的表:
- {table_name}: 包含该数据集的所有字段

## 重要提示
请仔细阅读上述 Schema 中的列名。只能使用 Schema 中实际存在的列名来编写 SQL。
如果 Schema 中有 "Product Name" 列，请用双引号引用为 "Product Name"。
如果 Schema 中有 "State" 列，它就是 State 列。
不要假设存在 "Product_Category"、"Avg_Price"、"Month" 等列名 —— 必须根据实际 Schema 来写。

请根据用户需求生成 SQL："""


class SQLAgent(BaseAgent):
    """自然语言转 SQL 并执行，返回 DataFrame。"""

    name = "sql_agent"

    def __init__(self, csv_path: str | None = None):
        super().__init__()
        self.db = init_duckdb(csv_path)

    def run(self, input_data: dict) -> dict:
        """
        input_data = {
            "task": "查询2020年每个月的销售额",
            "table_name": "transactions"  # optional, default "transactions"
        }
        returns: {"sql": str, "dataframe_json": str, "row_count": int, "error": str | None}
        """
        task = input_data.get("task", "")
        table_name = input_data.get("table_name", "transactions")

        if not task:
            return {"error": "No task provided", "dataframe_json": "[]", "row_count": 0, "sql": ""}

        # 生成 SQL
        sql = self._generate_sql(task, table_name)
        if not sql:
            return {"error": "Failed to generate SQL", "dataframe_json": "[]", "row_count": 0, "sql": ""}

        # 执行 SQL
        try:
            df = self.db.query_df(sql)
            logger.info(f"SQLAgent executed query, got {len(df)} rows")
            return {
                "sql": sql,
                "dataframe_json": df.to_json(orient="records", force_ascii=False),
                "row_count": len(df),
                "error": None,
            }
        except Exception as e:
            logger.error(f"SQL execution failed: {e}")
            # 尝试修复 SQL 后重试一次
            try:
                fixed_sql = self._fix_sql(sql, str(e))
                if fixed_sql != sql:
                    logger.info(f"Retrying with fixed SQL: {fixed_sql[:200]}")
                    df = self.db.query_df(fixed_sql)
                    return {
                        "sql": fixed_sql,
                        "dataframe_json": df.to_json(orient="records", force_ascii=False),
                        "row_count": len(df),
                        "error": None,
                    }
            except Exception as e2:
                logger.error(f"SQL retry also failed: {e2}")

            return {"error": str(e), "dataframe_json": "[]", "row_count": 0, "sql": sql}

    def _generate_sql(self, task: str, table_name: str) -> str:
        """使用 LLM 生成 SQL。"""
        schema_text = self.db.get_schema_text()
        prompt = SQL_AGENT_SYSTEM_PROMPT.format(
            schema=schema_text,
            table_name=table_name,
        )
        messages = [
            {"role": "system", "content": prompt},
            {"role": "user", "content": f"请为以下需求生成 SQL：\n{task}"},
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

    def _fix_sql(self, sql: str, error_msg: str) -> str:
        """尝试修复常见的 SQL 错误。"""
        fixed = sql
        # 移除可能冲突的反引号
        if "`" in fixed:
            fixed = fixed.replace("`", '"')
        # 如果缺少 LIMIT 且查询结果太大，添加 LIMIT
        if "does not" in error_msg.lower() or "column" in error_msg.lower():
            pass  # 无法自动修复列名问题
        return fixed

    def query_direct(self, sql: str) -> pd.DataFrame:
        """直接执行 SQL 查询（绕过 LLM），用于已知 SQL 的场景。"""
        return self.db.query_df(sql)
