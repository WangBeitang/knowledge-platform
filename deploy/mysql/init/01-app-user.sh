#!/usr/bin/env bash
# 平台 MySQL 应用账号（docker-entrypoint-initdb.d 首次初始化执行；仅首次建库时运行）。
# 冻结（Stage 6 Batch 2）：backend 使用独立应用账号，禁止使用 root。
# MYSQL_APP_USER / MYSQL_APP_PASSWORD 由 deploy/.env 提供，仅授权平台独立库。
# 注意：官方镜像对 .sql 不做环境变量展开，因此用 .sh 形式。
set -euo pipefail

mysql -uroot -p"${MYSQL_ROOT_PASSWORD}" <<SQL
CREATE USER IF NOT EXISTS '${MYSQL_APP_USER}'@'%' IDENTIFIED BY '${MYSQL_APP_PASSWORD}';
GRANT ALL PRIVILEGES ON knowledge_platform.* TO '${MYSQL_APP_USER}'@'%';
FLUSH PRIVILEGES;
SQL
