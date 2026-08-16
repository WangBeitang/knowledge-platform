#!/usr/bin/env bash
# 首次部署初始化（在目标云服务器 deploy/ 目录执行；幂等）。
# 步骤：等后端就绪 → Alembic 迁移 → Dataset 初始化（阶段 2 后启用）→ 演示数据（可选）
set -euo pipefail

cd "$(dirname "$0")/.."

echo "==> 等待后端就绪"
for i in $(seq 1 30); do
  if curl -sf http://localhost:8000/api/v1/health/live >/dev/null 2>&1; then
    break
  fi
  echo "    ... $i/30"
  sleep 2
done

echo "==> Alembic 迁移"
docker compose exec backend alembic upgrade head

echo "==> Dataset 初始化（幂等，阶段 2 可用）"
# docker compose exec backend python -m app.scripts.bootstrap

echo "==> 初始化完成"
