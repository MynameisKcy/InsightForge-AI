# HTTP API 参考

| 接口 | 方法 | 鉴权 | 说明 |
|------|------|------|------|
| `/` | GET | - | 登录页 |
| `/app` | GET | cookie/token | 主应用页（未登录 302->`/`） |
| `/api/register` | POST | - | 注册并自动登录、种 cookie |
| `/api/login` | POST | - | 登录，返回 token + 种 cookie |
| `/api/logout` | POST | - | 删 SQLite 会话 + 清令牌缓存 + 清 cookie |
| `/api/me` `/api/profile` | GET/POST | ✓ | 用户信息 / 改昵称（清缓存） |
| `/api/password` | POST | ✓ | 改密码（清缓存） |
| `/api/chat` | POST | ✓ | SSE 流式聊天，按 user_id 隔离 |
| `/api/analysis` | POST | ✓ | 同步 JSON 数据分析（**已退役** [ADR-0001](adr/0001-single-entry-analysis-as-tool.md)，前端不调） |
| `/api/conversation/history` | GET | ✓ | 长期记忆最近 N 轮 |
| `/api/sessions` `/{id}` | GET/DEL/PATCH | ✓ | 会话列表 / 详情 / 删除 / 重命名（均 IDOR owner 校验，404 防枚举） |
| `/api/settings` `/status` | GET/POST | ✓ | 配置读取（掩码）/ 保存（热重载）/ 是否已配置 |
| `/api/files` | GET | ✓ | 统一文件列表（文本 + 表格） |
| `/api/datasets` `/{name}` `/schema` | GET/POST/DEL | ✓ | 数据集列表 / 上传 / 删除 / schema（DESCRIBE+SUMMARIZE+样本） |
| `/api/datasets/upload` | POST | ✓ | CSV/Excel 上传（multipart，max 100MB） |
| `/api/datasources/reload` | POST | ✓ | 热重载 `datasources.yml` -> 挂载外部库 |
| `/api/knowledge/files` `/{name}` | GET/DEL | ✓ | 知识库文件列表（md5/入库态）/ 删除（删向量+文件） |
| `/api/knowledge/upload` | POST | ✓ | 文本文件增量入库（txt/pdf/docx/md） |
| `/api/knowledge/reindex` | POST | ✓ | 全量重建（需 `confirm=true`） |
| `/api/knowledge/stats` | GET | ✓ | 知识库统计 |
| `/api/health` | GET | - | 健康检查 |

> 鉴权为自研 opaque token（`secrets.token_hex(16)`，24h 过期，SQLite 持久化 + 30s 进程内 TTL 缓存），非 JWT/OAuth。`require_auth` 优先取 `Authorization: Bearer`，缺失时回退 `token` cookie（页面导航场景）。
