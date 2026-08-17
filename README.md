# 券商财富业务知识管理平台（knowledge-platform）

面向券商财富业务投顾资讯服务产品的知识管理平台：统一管理产品介绍、订单与错误码、客服知识、晨会资讯与内部话术；复用原 RAG 的导入/检索/流式回答能力，提供三档知识范围（外部公开 / 内部共享 / 管理员专属）的管理、问答、FAQ 运营与看板。

## 仓库结构

```text
backend/   FastAPI 单体后端（Python 3.11+，SQLAlchemy async，Alembic）
frontend/  Vue 3 + TypeScript + Vite 前端（Element Plus / ECharts / SSE）
deploy/    Docker Compose + Nginx + 初始化/冒烟脚本（部署在目标云服务器执行）
```

## 本地运行（开发）

前置：Python 3.11+、Node 18+；MySQL 与 Redis 可用（本机或远程）。

```bash
# 后端
cd backend
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env    # 填入真实 DB/Redis/RAG 配置
alembic upgrade head    # 建表
uvicorn app.main:app --reload --port 8000

# 前端
cd frontend
npm install
npm run dev
```

## 生产部署（Docker Compose，目标云服务器）

### 环境要求

- Linux x86_64 云服务器（单机）；
- Docker Engine + Docker Compose v2；
- Nginx（宿主侧，用于 TLS 终止与 80/443 公网入口）；
- 原 RAG 服务运行于宿主机进程或内网可达地址（平台 backend 容器经 `host.docker.internal` 或内网 IP 访问，不对公网新增暴露）。

### 端口与网络暴露（冻结）

| 组件 | 监听 | 说明 |
|---|---|---|
| MySQL / Redis | 仅容器内网 | **不发布宿主端口** |
| backend | `127.0.0.1:8000` | 仅宿主机回环（初始化/调试） |
| frontend | `127.0.0.1:8080` | 仅宿主机回环（Nginx 反代入口） |
| 宿主 Nginx | `0.0.0.0:80/443` | 唯一公网入口 |

部署后应验证：`ss -lntp | grep -E ':(3306|6379|8000|8080)\b'` 不得出现 `0.0.0.0` 监听。

### 配置（deploy/.env 与 backend/.env）

```bash
cd deploy
cp .env.example .env      # 生产必须生成真实强随机值，禁止 change-me-*
# SECRET_KEY=$(openssl rand -hex 32)
# SERVICE_API_KEY=$(openssl rand -hex 24)
# MYSQL_ROOT_PASSWORD=$(openssl rand -hex 24)
# MYSQL_APP_PASSWORD=$(openssl rand -hex 24)
# REDIS_PASSWORD=$(openssl rand -hex 24)

cd ../backend
cp .env.example .env
# 填写真实 SECRET_KEY / SERVICE_API_KEY / CORS_ORIGINS（实际前端来源，禁止 *）
# DB_USER=kp_app 与 deploy/.env 的 MYSQL_APP_USER 一致；DB_PASSWORD 一致
# RAG_QUERY_BASE_URL=http://host.docker.internal:<query_port>
# RAG_IMPORT_BASE_URL=http://host.docker.internal:<import_port>
# INIT_ADMIN_PASSWORD=初始管理员强密码（首次初始化后建议重置）
```

- `backend/.env` 与 `deploy/.env` 均不提交 Git；
- MySQL 平台应用账号（`MYSQL_APP_USER`，默认 `kp_app`）由 MySQL 容器首次初始化脚本
  `deploy/mysql/init/01-app-user.sh` 创建并授权 `knowledge_platform` 库；backend 禁止使用 root；
- Redis 必须设置 `REDIS_PASSWORD`。

### 构建与启动

```bash
cd deploy
docker compose -f docker-compose.yml config          # 预检配置
docker compose -f docker-compose.yml up -d --build
docker compose -f docker-compose.yml ps              # 盯：mysql/redis/backend/frontend 全部 running
```

### 宿主 Nginx（TLS + 反代）

```bash
cp deploy/nginx/conf.d/platform.conf /etc/nginx/conf.d/platform.conf
# 替换 <your-domain> 为真实域名；把 TLS 证书放到 /etc/nginx/certs/（server.crt/server.key）
nginx -t && systemctl reload nginx
```

- 公网入口统一 80/443；HTTP 301 → HTTPS；
- `/api` 经 frontend 容器反代到 backend；SSE 已 `proxy_buffering off`、`proxy_read_timeout 300s`；
- `X-Real-IP` / `X-Forwarded-For` / `X-Forwarded-Proto` 正确透传。

### 初始化（幂等，可重复执行）

```bash
cd deploy
bash scripts/init.sh
# 步骤：等待 backend → alembic upgrade head → 初始管理员 + 三档 Dataset bootstrap → health/ready 校验
# 需要演示数据时显式执行（默认不写）：
#   docker compose exec -T backend python -m app.scripts.seed_demo
```

任一步失败脚本返回非 0；`init.sh` 可重复执行（alembic 与 bootstrap 均幂等）。

### 冒烟验收（16 项）

```bash
cd deploy
BASE_URL=https://<域名> \
SERVICE_API_KEY=<外部密钥实际值> \
SMOKE_ADMIN_USERNAME=admin SMOKE_ADMIN_PASSWORD=<初始管理员密码> \
bash scripts/smoke.sh
# 覆盖：live/ready/登录/bootstrap verify/文档导入轮询终态/文档查询/Chunk/内部问答 SSE/
# FAQ 发布+精确命中/FAQ 同步状态/Dashboard/Audit/外部 Key 拒绝/外部合法 SSE/
# 外部 dataset_ids 422/外部不可命中 internal/admin 范围；测试数据唯一前缀并自动清理。
# 若原 RAG 未配置 MinerU，可显式 SMOKE_DOC_EXT=md 用 .md 验证文档导入链路（默认 pdf）。
```

失败返回非 0；测试数据（文档/FAQ/会话）唯一前缀 `smoke_stage6_<ts>` 并在脚本末尾清理或下线。

### 升级（应用代码）

```bash
cd deploy
git fetch && git checkout <新 commit>    # 记录当前稳定 commit 后切到新版本
docker compose -f docker-compose.yml up -d --build
docker compose exec -T backend alembic upgrade head   # 如有迁移
bash scripts/init.sh
```

### 查看日志

```bash
docker compose -f deploy/docker-compose.yml logs -f backend    # 后端
docker compose -f deploy/docker-compose.yml logs -f frontend   # 前端
docker compose -f deploy/docker-compose.yml logs -f mysql      # 数据库
```

### 常见故障

- **health/ready 显示 degraded**：逐组件查看 `data.components`。mysql/redis degraded 检查
  compose 健康状态与 `deploy/.env` 密码；rag_query/rag_import degraded 属预期（原 RAG 未随
  compose 部署），确认 backend/.env 的 `RAG_*_BASE_URL` 为容器可达地址。
- **backend 连不上 MySQL/Redis**：确认 `deploy/.env` 的 `MYSQL_APP_USER/PASSWORD` 与
  `backend/.env` 的 `DB_USER/DB_PASSWORD` 一致；容器内 `DB_HOST=mysql`、`REDIS_HOST=redis` 由 compose 覆盖。
- **外部接口 401 SERVICE_AUTH_FAILED**：`backend/.env` 的 `SERVICE_API_KEY` 与请求头 `X-Service-Key` 不一致。
- **SSE 流中断**：确认宿主 Nginx 与前端容器 Nginx 的 `proxy_buffering off` 与
  `proxy_read_timeout`（默认 300s）。

### 回滚（第一版，保持简单）

```bash
cd deploy
git log --oneline -5                     # 记录当前稳定 commit
git checkout <上一个稳定 commit>          # 回退到上一稳定版本
docker compose -f docker-compose.yml up -d --build
bash scripts/init.sh                     # 幂等初始化（health + 冒烟验证）
```

本版不涉及数据库 Schema 回滚（部署收口未新增迁移；如未来引入破坏性迁移，另设计 DB rollback）。

## 文档基线

- [概要设计总纲](../2.9_券商财富业务知识管理平台_概要设计总纲.md)
- [数据对象设计](../2.9_券商财富业务知识管理平台_数据对象设计.md)
- [API 接口设计](../2.9_券商财富业务知识管理平台_API接口设计.md)
- [模块级实施 SPEC](../2.9_券商财富业务知识管理平台_模块级实施SPEC.md)

> 设计文档位于工作区上级目录，正式交付时同步归档到 `docs/`。
