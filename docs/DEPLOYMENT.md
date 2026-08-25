# 部署指南

三种部署方式：本地 conda（开发）、Docker Compose（推荐）、阿里云 ECS（公网演示）。

---

## 1. 本地开发（conda）

```bash
git clone https://github.com/MynameisKcy/InsightForge-AI.git
cd InsightForge-AI/agent
conda create -n AnalysisAgent python=3.10 -y && conda activate AnalysisAgent
pip install -r requirements.txt
cp .env.example .env          # 填入 DASHSCOPE_API_KEY
python -m api.fastapi_server  # http://localhost:8502
```

启用链路追踪：仓库根 `docker-compose up -d jaeger`，再在 `agent/.env` 打开
`OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4318`，重启服务。

---

## 2. Docker Compose（推荐）

```bash
cd InsightForge-AI
cp .env.example .env          # 填入 DASHSCOPE_API_KEY（compose 自动读取根目录 .env）
docker-compose up -d          # 或 ./scripts/deploy.sh（预检 + 60s 探活重试）
```

| 服务 | 地址 | 说明 |
|------|------|------|
| insightforge-ai | http://localhost:8502 | 应用（自动启用 OTel 上报） |
| insightforge-jaeger | http://localhost:16686 | 链路追踪 UI |

### 数据持久化

容器内 `/app` 等价于本地 `agent/` 目录，以下宿主目录为卷挂载，重启/重建容器不丢数据：

| 宿主路径 | 容器路径 | 内容 |
|----------|----------|------|
| `./data` | `/app/data` | 数据集（CSV/Excel）与用户数据库 |
| `./chroma_db` | `/app/chroma_db` | RAG 向量库 |
| `./logs` | `/app/logs` | 运行日志 + 决策 JSONL + 基准结果 |
| `./reports` | `/app/reports` | 生成的图表与报告 |

### 镜像要点

- 多阶段构建：`python:3.10-slim` builder（`pip install --user`）+ 运行层整目录拷贝
- HEALTHCHECK 用标准库 `urllib` 探活 `/api/health`（slim 镜像无 curl）
- `.dockerignore` 排除数据/密钥/文档/缓存，镜像不含 `.env`

---

## 3. 阿里云 ECS（公网演示）

```bash
# ECS（Ubuntu，2C4G 起步）上执行
git clone https://github.com/MynameisKcy/InsightForge-AI.git
cd InsightForge-AI
cp .env.example .env && vim .env    # 填入 DASHSCOPE_API_KEY

curl -fsSL https://get.docker.com | bash
sudo usermod -aG docker $USER && newgrp docker

chmod +x scripts/deploy.sh && ./scripts/deploy.sh
```

安全组放行：

| 端口 | 用途 | 建议 |
|------|------|------|
| 8502 | Demo 访问 | 公网开放（或套 Nginx + HTTPS） |
| 16686 | Jaeger UI | **仅内网/SSH 隧道**（`ssh -L 16686:localhost:16686 ecs` 后本机访问） |
| 4318 | OTLP 接收 | 不对公网开放 |

---

## 4. 环境变量参考

| 变量 | 必填 | 说明 |
|------|------|------|
| `DASHSCOPE_API_KEY` | ✅ | 通义千问 API Key（也可网页「账号设置」里配，优先级更高） |
| `CHAT_MODEL_NAME` | — | 对话模型（默认取 `agent/config/rag.yml`） |
| `EMBEDDING_MODEL_NAME` | — | 嵌入模型 |
| `INSIGHTFORGE_SETTINGS_KEY` | — | 用户配置加密密钥（缺失自动生成） |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | — | **OTel 开关**：设置即启用链路上报（如 `http://jaeger:4318`） |
| `OTEL_SERVICE_NAME` | — | 服务名（默认 `insightforge`） |
| `TOKEN_PRICE_INPUT/OUTPUT` | — | 覆盖 Token 计价（元/千 token） |

## 5. 常见问题

| 现象 | 处理 |
|------|------|
| `deploy.sh` 报缺 `.env` | `cp .env.example .env` 并填 Key |
| 容器 healthy 但提问报 LLM 错误 | 检查 `docker-compose logs insightforge` 中 DASHSCOPE 相关报错；或网页设置面板改配 |
| Jaeger 无链路 | 确认走的是 compose 部署（注入了 endpoint）；本地部署见可观测性指南 §1.1 |
| 升级镜像 | `git pull && docker-compose up -d --build`（数据在卷中，不受影响） |
