#!/bin/bash
# InsightForge AI — 一键部署（Docker）
# 用法：chmod +x scripts/deploy.sh && ./scripts/deploy.sh
set -e

echo "🚀 部署 InsightForge AI..."

# ── 前置检查 ──
if ! command -v docker >/dev/null 2>&1; then
    echo "❌ 未安装 Docker，请先安装：curl -fsSL https://get.docker.com | bash"
    exit 1
fi

if [ ! -f .env ]; then
    echo "❌ 缺少 .env 文件：cp .env.example .env 并填入 DASHSCOPE_API_KEY"
    exit 1
fi

if ! grep -E "^DASHSCOPE_API_KEY=sk-" .env >/dev/null; then
    echo "❌ .env 中 DASHSCOPE_API_KEY 未填写"
    exit 1
fi

# ── 构建并启动（app + jaeger）──
if docker compose version >/dev/null 2>&1; then
    DC="docker compose"
else
    DC="docker-compose"
fi
$DC up -d --build

# ── 等待服务就绪（最多 60s，每 3s 探活）──
echo "⏳ 等待服务启动..."
elapsed=0
until curl -sf http://localhost:8502/api/health >/dev/null 2>&1; do
    sleep 3
    elapsed=$((elapsed + 3))
    if [ "$elapsed" -ge 60 ]; then
        echo "❌ 服务 60s 内未就绪，请查看日志：$DC logs insightforge"
        exit 1
    fi
done

echo "✅ 部署成功！"
echo ""
echo "📍 Demo:      http://localhost:8502"
echo "📍 Jaeger:    http://localhost:16686"
