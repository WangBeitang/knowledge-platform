#!/usr/bin/env bash
# 冒烟验收（阶段 1 骨架：健康检查；阶段 2+ 追加登录/bootstrap/问答/FAQ 闭环）。
# 用法：BASE_URL=https://<域名> bash smoke.sh
set -euo pipefail

BASE="${BASE_URL:-http://localhost:8000}"

echo "==> health/live"
curl -sf "$BASE/api/v1/health/live" && echo

echo "==> health/ready（组件状态）"
curl -sf "$BASE/api/v1/health/ready" && echo

echo "==> 冒烟通过"
