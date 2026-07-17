"""
Schema Loader: Extract table/column metadata from DuckDB for the SQL Agent.
"""

from database.duckdb_manager import _validate_table_name


class SchemaLoader:
    """Reads schema information from a DuckDBManager and returns a text
    description suitable for prompting an LLM to generate SQL."""

    @staticmethod
    def get_schema_text(duckdb_manager) -> str:
        import duckdb

        tables = duckdb_manager.execute("SHOW TABLES").fetchall()
        if not tables:
            return "No tables found."

        parts = []
        for (table_name,) in tables:
            _validate_table_name(table_name)
            cols = duckdb_manager.execute(
                f"DESCRIBE {table_name}"
            ).fetchall()
            col_lines = []
            for col_name, col_type, nullable, key, default, extra in cols:
                col_lines.append(f"  - {col_name} ({col_type})")
            parts.append(f"Table: {table_name}\n" + "\n".join(col_lines))
        return "\n\n".join(parts)

    @staticmethod
    def get_schema_dict(duckdb_manager) -> dict:
        """Return a dict representation for programmatic use."""
        import duckdb

        tables = duckdb_manager.execute("SHOW TABLES").fetchall()
        schema = {}
        for (table_name,) in tables:
            _validate_table_name(table_name)
            cols = duckdb_manager.execute(
                f"DESCRIBE {table_name}"
            ).fetchall()
            schema[table_name] = [
                {
                    "name": col_name,
                    "type": col_type,
                    "nullable": nullable,
                }
                for col_name, col_type, nullable, *_ in cols
            ]
        return schema
