# 数据源管理增强设计

**日期**: 2026-07-20
**状态**: 已批准
**范围**: 数据能力增强 — 多数据源支持与跨源关联分析

## 背景

InsightForge AI 当前仅绑定单个 Superstore Sales 数据集（`train.csv`），用户无法使用自己的数据，系统实用性受限。本次改造目标是让用户能够：

1. 上传自己的 CSV/Excel 文件进行分析
2. 连接本地/局域网的关系型数据库（MySQL、PostgreSQL）
3. 跨数据集关联分析（DuckDB 联邦查询）

**部署场景**: 纯本地单机使用，无多租户需求。

## 方案选择

**选定方案 A：DuckDB 统一联邦查询**

所有数据源注册为 DuckDB 的表/视图，SQLAgent 只需生成标准 DuckDB SQL 即可跨源 JOIN。

选择理由：
- 最小改动，与现有 SQLAgent 架构完美契合
- DuckDB 原生支持 `read_csv_auto`、`postgres_scan`、`mysql_scan` 等扩展
- 避免数据同步的复杂性和一致性问题
- 后续可渐进式加入缓存能力

## 架构设计

### 数据流

```
用户上传 CSV/Excel → 保存到 data/datasets/
                    → DuckDB LOAD (read_csv_auto / read_excel)
                    → Schema 自动解析并缓存到 datasources.db

管理员预配置数据库 → 连接信息存 config/datasources.yml (密码从 .env 读取)
                    → DuckDB 安装扩展 (postgres_scan / mysql_scan)
                    → 注册外部表为 DuckDB VIEW

SQLAgent 生成 SQL  → 读取所有可用数据集 schema
                    → 生成跨表/跨源 DuckDB SQL
                    → 执行并返回结果
```

### 文件结构变更

```
agent/
├── data/
│   ├── train.csv                # 现有默认数据集（保留）
│   ├── datasets/                # 新增：用户上传的数据文件
│   │   ├── sales_2024.csv
│   │   └── inventory.xlsx
│   └── external/                # 补建：现有配置引用的目录
├── database/
│   ├── datasources_db.py        # 新增：数据源元数据管理
│   └── duckdb_manager.py        # 改造：多数据源加载 + 扩展管理
├── config/
│   └── datasources.yml          # 新增：数据库连接配置
├── api/
│   └── fastapi_server.py        # 改造：新增数据集管理端点 + 前端面板
├── agents/
│   └── sql_agent.py             # 改造：多表 schema 注入 + DataResolver 动态化
└── agent/
    └── tools/
        └── agent_tools.py       # 改造：更新数据概览工具
```

### 数据源元数据

新建 SQLite 数据库 `datasources.db`，包含 `datasets` 表：

| 字段 | 类型 | 说明 |
|------|------|------|
| id | TEXT | UUID |
| name | TEXT | 数据集显示名 |
| source_type | TEXT | `csv` / `excel` / `postgres` / `mysql` |
| file_path | TEXT | 文件路径（本地文件）或连接标识（数据库） |
| table_name | TEXT | DuckDB 中的表名 |
| schema_json | TEXT | 列名、类型、统计信息 JSON |
| row_count | INTEGER | 行数 |
| description | TEXT | 数据集描述（可选） |
| created_at | TIMESTAMP | 创建时间 |
| updated_at | TIMESTAMP | 更新时间 |

### 数据集命名规则

- 用户上传文件：`{sanitized_filename}`，如 `sales_2024`
- 管理员数据库表：`{db_alias}_{table_name}`，如 `erp_orders`
- 同名冲突：追加数字后缀 `sales_2024_2`

### DuckDB 生命周期

- **启动时**：创建 DuckDB 实例，从 `datasources.db` 读取数据集列表，重新加载文件和注册外部表
- **上传时**：即时加载到 DuckDB，更新元数据
- **删除时**：从 DuckDB `DROP TABLE`，删除文件，更新元数据
- **热加载**：`/api/datasources/reload` 端点重新读取 `datasources.yml` 并注册数据库连接

### 数据持久化策略

文件存磁盘 + DuckDB 启动时重加载：
- 用户上传的文件保存在 `data/datasets/` 目录
- DuckDB 使用 `:memory:` 模式（保持现有行为）
- 每次服务器启动时，从 `datasources.db` 读取元数据，重新加载所有数据集到 DuckDB
- 服务器重启后数据不丢失（文件在磁盘，元数据在 SQLite）

## API 端点设计

### 新增端点

| 端点 | 方法 | 说明 |
|------|------|------|
| `/api/datasets` | GET | 列出所有可用数据集（含类型、行数、schema 摘要） |
| `/api/datasets/upload` | POST | 上传 CSV/Excel 文件，自动解析并加载到 DuckDB |
| `/api/datasets/{name}` | DELETE | 删除数据集（卸载 DuckDB 表 + 删除文件） |
| `/api/datasets/{name}/schema` | GET | 获取数据集的详细 schema（列名、类型、统计、样本行） |
| `/api/datasources/reload` | POST | 热加载 datasources.yml 配置的数据库连接 |

### 上传流程

1. 用户选择文件 → `POST /api/datasets/upload` (multipart/form-data)
2. 后端校验：文件类型（csv/xlsx/xls）、大小限制（默认 100MB）
3. 保存到 `data/datasets/{sanitized_filename}`
4. DuckDB 执行加载：
   - CSV: `CREATE TABLE {table_name} AS SELECT * FROM read_csv_auto('{path}')`
   - Excel: `CREATE TABLE ... AS SELECT * FROM read_excel('{path}')`（DuckDB 1.0+ 内置支持）
5. 解析 schema：`DESCRIBE {table_name}` + `SUMMARIZE {table_name}`
6. 写入 `datasources.db` 元数据
7. 返回数据集信息（名称、列、行数、样本前5行）

### 数据库连接配置

`config/datasources.yml` 格式：

```yaml
databases:
  - name: local_mysql
    type: mysql
    host: 127.0.0.1
    port: 3306
    database: my_business
    user: root
    password_env: MYSQL_PASSWORD    # 从 .env 读取密码变量名

  - name: local_postgres
    type: postgres
    host: 127.0.0.1
    port: 5432
    database: analytics
    user: postgres
    password_env: PG_PASSWORD

    # 可选：只暴露特定表，不填则暴露所有表
    tables:
      - orders
      - customers
```

密码不硬编码在 YAML 中，通过 `password_env` 字段指定 `.env` 中的变量名，运行时从环境变量读取。

## SQLAgent 改造

### Schema 注入增强

当前 prompt 只注入单表 schema，改为注入所有可用表的 schema：

```
可用数据表：
1. train (CSV, 9800行) - Superstore销售数据
   列: order_id(TEXT), order_date(DATE), sales(DOUBLE), ...
2. inventory (CSV, 500行) - 库存数据
   列: product_id(TEXT), quantity(INTEGER), ...
3. erp_orders (MySQL:local_mysql, 50000行) - ERP订单
   列: order_id(TEXT), customer_id(TEXT), amount(DOUBLE), ...

跨表查询时请使用标准 SQL JOIN。
```

### DataResolver 改造

从硬编码的 `DATASET_MAP` 改为从 `datasources.db` 动态读取：

```python
# 旧：DATASET_MAP = {"销售": "train.csv"}
# 新：从 datasources.db 查询所有已注册数据集
def resolve_datasets(self, query: str) -> list[str]:
    """返回与查询相关的表名列表"""
    all_datasets = self._load_from_db()
    # 关键词匹配或 LLM 辅助匹配
    return matched_table_names
```

### DuckDB 扩展加载

启动时自动安装和加载数据库扩展：

```python
def _register_external_databases(self):
    for db_config in load_datasources_config():
        password = os.environ.get(db_config.password_env, "")
        if db_config.type == "postgres":
            self.conn.execute("INSTALL postgres_scan; LOAD postgres_scan;")
            for table in db_config.tables:
                # 使用参数化构建避免 SQL 注入
                self.conn.execute(
                    f"CREATE VIEW {safe_ident(db_config.name + '_' + table)} AS "
                    f"SELECT * FROM postgres_scan(host=?, port=?, database=?, user=?, password=?, table=?)",
                    [db_config.host, db_config.port, db_config.database,
                     db_config.user, password, table]
                )
        elif db_config.type == "mysql":
            self.conn.execute("INSTALL mysql_scan; LOAD mysql_scan;")
            for table in db_config.tables:
                self.conn.execute(
                    f"CREATE VIEW {safe_ident(db_config.name + '_' + table)} AS "
                    f"SELECT * FROM mysql_scan(host=?, port=?, database=?, user=?, password=?, table=?)",
                    [db_config.host, db_config.port, db_config.database,
                     db_config.user, password, table]
                )

def safe_ident(name: str) -> str:
    """转义 DuckDB 标识符，防止注入"""
    return '"' + name.replace('"', '""') + '"'
```

### SQL 沙箱调整

保持只读白名单不变，新增：
- 查询超时：`SET statement_timeout='30s'`
- 结果行数限制：默认 `LIMIT 10000`，防止内存溢出
- 大表提示：当查询的表行数 > 100000 时，prompt 中提示 LLM 注意添加 LIMIT

## 前端交互设计

### 数据集管理面板

在现有 FastAPI 嵌入式 HTML 的左侧边栏中，知识库管理面板下方新增"数据集管理"区域：

- **数据集列表**：显示名称、类型图标（CSV/Excel/MySQL/PG）、行数、上传时间
- **上传按钮**：点击弹出文件选择，支持拖拽上传
- **删除按钮**：每个数据集旁有删除图标
- **Schema 预览**：点击数据集展开显示列信息和样本数据（前5行）
- **数据库连接状态**：显示管理员配置的数据库连接是否可用（绿/红指示灯）

### 对话交互

- 用户上传数据集后，系统自动发送消息："已加载数据集「sales_2024」，包含 15 列 2300 行数据，你可以开始提问了"
- SQLAgent 回答时，如果涉及跨表查询，在回答中标注使用了哪些数据源

## 错误处理

| 场景 | 处理方式 |
|------|---------|
| 上传文件格式错误 | 返回 400 + "仅支持 CSV/XLSX/XLS" |
| 文件过大（>100MB） | 返回 413 + "文件超过大小限制" |
| CSV 编码问题 | DuckDB `read_csv_auto` 自动检测编码，失败则提示用户转为 UTF-8 |
| Excel 多 Sheet | 默认加载第一个 Sheet，UI 可选择加载哪个 |
| 数据库连接失败 | 启动时检测，标记为不可用，SQLAgent prompt 中标注"当前不可用" |
| 大表查询超时 | SQL 沙箱 `SET statement_timeout='30s'`，超时返回友好提示 |
| DuckDB 扩展安装失败 | 优雅降级，跳过该数据源，日志记录错误 |
| 同名文件重复上传 | 提示用户数据集已存在，可选择覆盖或跳过 |

## 不在本次范围内

- 实时数据流（Kafka 等）
- API 数据源
- 数据缓存层
- 多租户权限隔离
- 数据清洗/预处理 UI
