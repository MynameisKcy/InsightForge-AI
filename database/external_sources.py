"""外部数据库 ATTACH 注册（R1 候选1 剩余子项：从 duckdb_manager 拆出）。

读 datasources.yml 配置，安装 DuckDB 扩展（postgres_scan/mysql_scan），
ATTACH 外部数据库并把配置/自动发现的表注册为本地视图。失败不崩溃，
逐项记入 failed 继续。
"""
from database.safety import safe_ident
from utils.logger_handler import logger


def register_external_databases(conn) -> dict:
    """读取 datasources_conf 配置，安装 DuckDB 扩展，注册外部数据库表为视图。

    返回 {"registered": [...], "failed": [...]}。
    失败时不会崩溃，仅记录错误并继续。
    """
    registered = []
    failed = []

    try:
        from utils.config_handler import datasources_conf
    except Exception:
        logger.info("register_external_databases: datasources_conf not available, skipping")
        return {"registered": registered, "failed": failed}

    if not datasources_conf or not datasources_conf.get("databases"):
        return {"registered": registered, "failed": failed}

    for db_conf in datasources_conf["databases"]:
        db_name = db_conf.get("name", "unknown")
        db_type = db_conf.get("type", "").lower()

        try:
            # 安装并加载对应扩展
            if db_type == "postgres":
                conn.execute("INSTALL postgres_scan")
                conn.execute("LOAD postgres_scan")
            elif db_type == "mysql":
                conn.execute("INSTALL mysql_scan")
                conn.execute("LOAD mysql_scan")
            else:
                failed.append({"name": db_name, "error": f"不支持的数据库类型: {db_type}"})
                continue

            # 读取密码（从环境变量）
            import os as _os
            password = _os.environ.get(db_conf.get("password_env", ""), "")

            # 构建连接参数
            host = db_conf.get("host", "127.0.0.1")
            port = db_conf.get("port", 5432 if db_type == "postgres" else 3306)
            database = db_conf.get("database", "")
            user = db_conf.get("user", "")

            # ATTACH 外部数据库（连接字符串中的单引号需转义，防 SQL 注入）
            attach_name = safe_ident(db_name)
            # 数值型 port 不转义；其余字段单引号需翻倍转义
            port_str = str(port) if str(port).isdigit() else str(port).replace("'", "''")
            host_e = str(host).replace("'", "''")
            user_e = str(user).replace("'", "''")
            password_e = str(password).replace("'", "''")
            database_e = str(database).replace("'", "''")
            if db_type == "postgres":
                conn.execute(
                    f"ATTACH 'host={host_e} port={port_str} user={user_e} password={password_e} dbname={database_e}' AS {attach_name} (TYPE postgres)"
                )
            elif db_type == "mysql":
                conn.execute(
                    f"ATTACH 'host={host_e} port={port_str} user={user_e} password={password_e} database={database_e}' AS {attach_name} (TYPE mysql)"
                )

            # 确定要暴露的表
            tables_list = db_conf.get("tables", [])
            if not tables_list:
                # 自动发现：查询 information_schema
                try:
                    if db_type == "postgres":
                        schema_rows = conn.execute(
                            f"SELECT table_name FROM {attach_name}.information_schema.tables WHERE table_schema='public'"
                        ).fetchall()
                    elif db_type == "mysql":
                        schema_rows = conn.execute(
                            f"SELECT table_name FROM {attach_name}.information_schema.tables WHERE table_schema=DATABASE()"
                        ).fetchall()
                    else:
                        schema_rows = []
                    tables_list = [r[0] for r in schema_rows]
                except Exception as e:
                    logger.warning(f"register_external_databases: auto-discover tables failed for {db_name}: {e}")
                    tables_list = []

            # 为每个表创建视图
            for tbl in tables_list:
                try:
                    view_name = safe_ident(tbl)
                    conn.execute(
                        f"CREATE OR REPLACE VIEW {view_name} AS SELECT * FROM {attach_name}.{safe_ident(tbl)}"
                    )
                    registered.append({"database": db_name, "table": tbl})
                    logger.info(f"register_external_databases: registered view '{tbl}' from {db_name}")
                except Exception as e:
                    failed.append({"name": f"{db_name}.{tbl}", "error": str(e)})
                    logger.warning(f"register_external_databases: failed to create view for {db_name}.{tbl}: {e}")

        except Exception as e:
            failed.append({"name": db_name, "error": str(e)})
            logger.warning(f"register_external_databases: failed for {db_name}: {e}")

    return {"registered": registered, "failed": failed}
