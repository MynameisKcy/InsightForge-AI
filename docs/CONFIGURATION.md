# 配置说明

| 文件 | 作用 | 真实默认值 |
|------|------|------------|
| `.env` | **真相源**：`DASHSCOPE_API_KEY` / `CHAT_MODEL_NAME` / `EMBEDDING_MODEL_NAME` / `INSIGHTFORGE_SETTINGS_KEY`（gitignored） | - |
| `config/rag.yml` | rerank 与检索参数（模型名仅作 fallback） | `rerank_model: gte-rerank-v2`、`retrieve_k: 15`、`rerank_top_n: 3`、`rerank_score_threshold: 0.3` |
| `config/chroma.yml` | 向量库与分块 | `collection_name: agent`、`chunk_size: 500`、`chunk_overlap: 50`、`allowed_knowledge_file_type: [txt,pdf,docx,md]` |
| `config/agent.yml` | 外部数据路径 | `external_data_path: data/external/records.csv` |
| `config/datasources.yml` | 管理员预置数据库连接 | `databases: []`（默认空；每条含 name/type/host/port/database/user/`password_env`/tables） |
| `config/prompts.yml` | Prompt 模板路径 | main / rag_summarize / report / document_report 四个 .txt 路径 |

**配置优先级**：用户网页配置 > `.env` > YAML（`factory.py:65-92`）。

`config/datasources.yml` 结构示例：

```yaml
databases:
  - name: local_mysql
    type: mysql              # 或 postgres
    host: 127.0.0.1
    port: 3306
    database: my_business
    user: root
    password_env: MYSQL_PASSWORD   # 引用 .env 变量名，密码不硬编码
    tables: []                     # [] = 自动发现并暴露全部；列表 = 限定
```

> 改动 `chunk_size` 后需清库重灌：前端「📚 知识库 -> ⟳ 全量重建索引」或 `POST /api/knowledge/reindex`（需 `confirm=true`）。
