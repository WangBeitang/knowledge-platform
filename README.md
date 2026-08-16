# 券商财富业务知识管理平台（knowledge-platform）

面向券商财富业务投顾资讯服务产品的知识管理平台：统一管理产品介绍、订单与错误码、客服知识、晨会资讯与内部话术；复用原 RAG 的导入/检索/流式回答能力，提供三档知识范围（外部公开 / 内部共享 / 管理员专属）的管理、问答、FAQ 运营与看板。

## 仓库结构

```text
backend/   FastAPI 单体后端（Python 3.11+，SQLAlchemy async，Alembic）
frontend/  Vue 3 + TypeScript + Vite 前端（Element Plus / ECharts / SSE）
deploy/    Docker Compose + Nginx + 初始化/冒烟脚本（部署在目标云服务器执行）
```

## 本地运行（阶段 1 基线）

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

## 文档基线

- [概要设计总纲](../2.9_券商财富业务知识管理平台_概要设计总纲.md)
- [数据对象设计](../2.9_券商财富业务知识管理平台_数据对象设计.md)
- [API 接口设计](../2.9_券商财富业务知识管理平台_API接口设计.md)
- [模块级实施 SPEC](../2.9_券商财富业务知识管理平台_模块级实施SPEC.md)

> 设计文档位于工作区上级目录，正式交付时同步归档到 `docs/`。
