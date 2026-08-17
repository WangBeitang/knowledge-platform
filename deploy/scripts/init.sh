#!/usr/bin/env bash
# 首次/重复初始化（在目标云服务器 deploy/ 目录执行；幂等可重复）。
#
# 步骤：等待服务 → Alembic 迁移 → 初始管理员 + Dataset bootstrap → health/ready 校验
# 要求：
# - 重复执行安全（alembic upgrade head / bootstrap 均为幂等）；
# - Dataset bootstrap 使用现有幂等能力（python -m app.scripts.bootstrap）；
# - 不默认写演示数据（需要时显式执行 seed_demo，见文件末尾注释）；
# - 任一步失败返回非 0。
set -euo pipefail

cd "$(dirname "$0")/.."

# 部署脚本统一从 deploy/.env 取 compose 变量（compose 自身也会读它）
if [ -f .env ]; then
  set -a; source .env; set +a
fi

BACKEND_URL="${BACKEND_URL:-http://127.0.0.1:8000}"

echo "==> 等待 backend 就绪（${BACKEND_URL}）"
ok=0
for i in $(seq 1 60); do
  if curl -sf "${BACKEND_URL}/api/v1/health/live" >/dev/null 2>&1; then
    ok=1
    break
  fi
  echo "    ... $i/60"
  sleep 2
done
if [ "$ok" != "1" ]; then
  echo "!! backend 未在 120 秒内就绪，请检查 docker compose ps" >&2
  exit 1
fi

echo "==> Alembic 迁移（幂等）"
docker compose exec -T backend alembic upgrade head

echo "==> 初始管理员 + Dataset bootstrap（幂等）"
docker compose exec -T backend python -m app.scripts.bootstrap

echo "==> health/ready 校验"
ready=$(curl -sf "${BACKEND_URL}/api/v1/health/ready" || true)
if [ -z "$ready" ]; then
  echo "!! health/ready 不可达" >&2
  exit 1
fi
echo "$ready"
# 校验返回 JSON 可解析且整体状态为 ok 或 degraded（RAG 未随 compose 部署时可 degraded）
if ! echo "$ready" | python3 -c '
import json, sys
data = json.load(sys.stdin)
components = data["data"]["components"]
print("components:", {k: v["status"] for k, v in components.items()})
if components.get("mysql", {}).get("status") != "ok":
    sys.exit("mysql 未就绪")
if components.get("redis", {}).get("status") != "ok":
    sys.exit("redis 未就绪")
'; then
  echo "!! MySQL / Redis 未就绪（RAG 可 degraded，属预期）" >&2
  exit 1
fi

echo "==> 初始化完成"
echo "提示：如需演示数据，显式执行: docker compose exec -T backend python -m app.scripts.seed_demo"
