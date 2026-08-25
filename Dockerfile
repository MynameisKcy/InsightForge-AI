# InsightForge AI — 多阶段构建
# /app 等价于本地的 agent/ 目录（api/agents/utils… 平铺），
# 与本地启动方式 `cd agent && python -m api.fastapi_server` 完全同构。
FROM python:3.10-slim AS builder

WORKDIR /build
COPY agent/requirements.txt .
# 装到用户目录，运行阶段整目录拷贝，不污染站点包
RUN pip install --user --no-cache-dir -r requirements.txt

# ── 运行阶段 ──
FROM python:3.10-slim

WORKDIR /app

# 复制依赖（/root/.local）
COPY --from=builder /root/.local /root/.local
ENV PATH=/root/.local/bin:$PATH

# 复制应用代码（.dockerignore 已排除数据/密钥/缓存）
COPY agent/ /app/

# 数据目录（卷挂载点；缺失时应用也会自建，此处显式声明便于挂载）
RUN mkdir -p data chroma_db logs reports

EXPOSE 8502

# slim 无 curl，用标准库探活（/api/health 已存在）
HEALTHCHECK --interval=30s --timeout=10s --start-period=15s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://localhost:8502/api/health', timeout=8).status==200 else 1)"

CMD ["python", "-m", "api.fastapi_server"]
