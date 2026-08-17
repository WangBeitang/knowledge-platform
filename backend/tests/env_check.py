"""真实链路 smoke 前置环境预检：venv 依赖 / 本地模型 / 端口 / 云上资源。

背景：Stage 5 smoke 多次因环境问题返工（.venv-test 缺 torch/FlagEmbedding、
nohup 服务被沙箱回收、云上资源不可达、BGE-M3 模型未就绪）。本脚本一条命令
确认环境就绪，再跑 smoke_faq_real.py / smoke_stage4.py 等真实链路。

用法（在 knowledge-platform/backend 下，用平台 venv 运行）：
  .venv/bin/python tests/env_check.py            # 快速：依赖 import + 模型目录 + TCP 可达
  .venv/bin/python tests/env_check.py --deep     # 深度：云上资源真实认证 + BGE-M3 加载
  .venv/bin/python tests/env_check.py --scope internal_shared   # 附加：scope 映射打印

退出码：0=全部通过；1=存在 FAIL 项。
注意：本地服务 8000/8001/8002 预期为「已停」属正常（INFO，不算 FAIL）；
云上资源探活仅测可达性，--deep 才做真实认证。
"""

from __future__ import annotations

import argparse
import asyncio
import socket
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]  # knowledge-platform/
BACKEND_DIR = PROJECT_ROOT / "backend"
RAG_ROOT = Path("/Users/beitang/Desktop/项目实战/ai_knowledge_base_after_class")
RAG_VENV_PY = RAG_ROOT / ".venv-test" / "bin" / "python"
BGE_M3_DIR = Path("/Users/beitang/ai_models/modelscope_cache/models/BAAI/bge-m3")

CLOUD_HOST = "49.235.159.92"
# (port, 名称, 服务)
CLOUD_PORTS = [
    (3308, "MySQL(平台)", "knowledge_platform@3308"),
    (6379, "Redis", "WYN.1995"),
    (27017, "Mongo", "ai_mfg_kb_meta"),
    (19530, "Milvus", "root:Milvus"),
    (9000, "MinIO", "minioadmin"),
]
LOCAL_PORTS = [8000, 8001, 8002]  # 平台 / 原 RAG query / 原 RAG import（预期停止）

PLATFORM_DEPS = ["fastapi", "sqlalchemy", "aiomysql", "redis", "httpx", "jwt", "pydantic", "pytest"]
RAG_DEPS = ["fastapi", "uvicorn", "pymilvus", "torch", "FlagEmbedding"]

results: list[tuple[str, str, str]] = []  # (level, name, detail)  level: PASS/FAIL/WARN/INFO


def record(level: str, name: str, detail: str = "") -> None:
    results.append((level, name, detail))
    print(f"  [{level:4}] {name} {detail}")


def check_platform_venv() -> None:
    """当前解释器（应为平台 .venv）直接 import 检查。"""
    for dep in PLATFORM_DEPS:
        try:
            __import__(dep)
            record("PASS", f"平台依赖 {dep}")
        except Exception as exc:  # noqa: BLE001
            record("FAIL", f"平台依赖 {dep}", f"({exc.__class__.__name__})")
    # 平台 DB/Redis 配置可读（校验 .env 是否完整）
    try:
        from app.core.config import get_settings  # noqa: PLC0415

        s = get_settings()
        record("PASS", "平台 settings 加载", f"db={s.db_host}:{s.db_port}")
    except Exception as exc:  # noqa: BLE001
        record("FAIL", "平台 settings 加载", f"({exc.__class__.__name__}) {exc}")


def check_rag_venv() -> None:
    """原 RAG .venv-test 依赖（subprocess，避免污染当前解释器）。"""
    if not RAG_VENV_PY.exists():
        record("FAIL", "原 RAG .venv-test 存在", str(RAG_VENV_PY))
        return
    for dep in RAG_DEPS:
        code = subprocess.run(
            [str(RAG_VENV_PY), "-c", f"import {dep}"],
            capture_output=True,
            text=True,
            timeout=30,
        ).returncode
        record("PASS" if code == 0 else "FAIL", f"原 RAG .venv-test {dep}")


def check_bge_model(deep: bool) -> None:
    """BGE-M3：快速只查目录；--deep 真实加载（约 1 分钟）。"""
    if not BGE_M3_DIR.is_dir():
        record("FAIL", "BGE-M3 模型目录", str(BGE_M3_DIR))
        return
    record("PASS", "BGE-M3 模型目录", str(BGE_M3_DIR))
    if deep:
        code = subprocess.run(
            [
                str(RAG_VENV_PY),
                "-c",
                "from app.shared.model.embedding_utils import get_bge_m3_ef; "
                "get_bge_m3_ef(); print('ok')",
            ],
            capture_output=True,
            text=True,
            timeout=240,
            cwd=str(RAG_ROOT),
        )
        if code.returncode == 0:
            record("PASS", "BGE-M3 模型加载")
        else:
            tail = (code.stderr or "").strip().splitlines()[-1] if code.stderr else ""
            record("FAIL", "BGE-M3 模型加载", tail[:120])


def _tcp(host: str, port: int, timeout: float = 5.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def check_ports() -> None:
    for port in LOCAL_PORTS:
        up = _tcp("127.0.0.1", port)
        # 本地服务「已停」是预期状态（smoke 前手动启动），只提示不判失败
        record(
            "WARN" if up else "INFO",
            f"本地端口 {port}",
            "运行中（smoke 前如未启动属正常）" if up else "未监听（预期，smoke 前启动）",
        )
    for port, name, _desc in CLOUD_PORTS:
        ok = _tcp(CLOUD_HOST, port)
        record("PASS" if ok else "FAIL", f"云上 {name} {port}", "可达" if ok else "不可达")


def _rag_py_check(label: str, code: str, timeout: int = 60) -> None:
    """用原 RAG .venv-test 解释器执行认证代码（pymongo/pymilvus/minio 依赖在其 venv）。"""
    if not RAG_VENV_PY.exists():
        record("FAIL", label, "原 RAG .venv-test 不存在")
        return
    proc = subprocess.run(
        [str(RAG_VENV_PY), "-c", code],
        capture_output=True,
        text=True,
        timeout=timeout,
        cwd=str(RAG_ROOT),
    )
    if proc.returncode == 0:
        record("PASS", label, "ok")
    else:
        tail = (proc.stderr or "").strip().splitlines()
        err = tail[-1][:120] if tail else ""
        record("FAIL", label, err)


def check_cloud_deep() -> None:
    """深度认证：MySQL SELECT 1 / Redis PING / Mongo ping / Milvus 连接 / MinIO 桶列表。"""
    sys.path.insert(0, str(BACKEND_DIR))
    # MySQL（平台 settings）
    try:
        from app.core.database import get_session_factory

        async def _mysql() -> bool:
            async with get_session_factory()() as s:
                from sqlalchemy import text

                await s.execute(text("SELECT 1"))
            return True

        asyncio.run(_mysql())
        record("PASS", "MySQL 真实连接", "SELECT 1 ok")
    except Exception as exc:  # noqa: BLE001
        record("FAIL", "MySQL 真实连接", f"({exc.__class__.__name__}) {str(exc)[:120]}")
    # Redis PING
    try:
        import redis.asyncio as aioredis

        from app.core.config import get_settings

        s = get_settings()

        async def _redis() -> bool:
            r = aioredis.Redis(
                host=s.redis_host, port=s.redis_port, password=s.redis_password or None
            )
            await r.ping()
            await r.aclose()
            return True

        asyncio.run(_redis())
        record("PASS", "Redis 真实连接", "PING ok")
    except Exception as exc:  # noqa: BLE001
        record("FAIL", "Redis 真实连接", f"({exc.__class__.__name__}) {str(exc)[:120]}")
    # Mongo / Milvus / MinIO：依赖在原 RAG .venv-test，用子进程认证
    _rag_py_check(
        "Mongo 真实连接",
        "from pymongo import MongoClient; "
        "c = MongoClient('mongodb://root:WYN.1995@49.235.159.92:27017/?authSource=admin', "
        "serverSelectionTimeoutMS=5000); c.admin.command('ping'); c.close()",
    )
    _rag_py_check(
        "Milvus 真实连接",
        "from pymilvus import MilvusClient; "
        "mc = MilvusClient(uri='http://49.235.159.92:19530', token='root:Milvus'); "
        "mc.list_collections()",
    )
    _rag_py_check(
        "MinIO 真实连接",
        "from minio import Minio; "
        "m = Minio('49.235.159.92:9000', access_key='minioadmin', "
        "secret_key='minioadmin', secure=False); "
        "m.list_buckets()",
    )


def print_scope_mapping(scope: str | None) -> None:
    if not scope:
        return
    try:
        sys.path.insert(0, str(BACKEND_DIR))
        from app.rag.scope_policy import (  # noqa: PLC0415
            dataset_id_for_scope,
            document_visibility_for_scope,
        )

        did = dataset_id_for_scope(scope)
        vis = document_visibility_for_scope(scope)
        record("PASS", f"scope={scope} 映射", f"dataset={did} visibility={vis}")
    except Exception as exc:  # noqa: BLE001
        record("FAIL", f"scope={scope} 映射", f"({exc.__class__.__name__}) {exc}")


def main() -> int:
    parser = argparse.ArgumentParser(description="真实 smoke 环境预检")
    parser.add_argument(
        "--deep", action="store_true", help="云上资源真实认证 + BGE-M3 加载（约 1-2 分钟）"
    )
    parser.add_argument("--scope", default=None, help="打印 scope → dataset/visibility 映射")
    args = parser.parse_args()

    print("== 平台 venv 依赖 ==")
    check_platform_venv()
    print("== 原 RAG .venv-test 依赖 ==")
    check_rag_venv()
    print("== BGE-M3 本地模型 ==")
    check_bge_model(deep=args.deep)
    print("== 本地端口 & 云上可达性 ==")
    check_ports()
    print_scope_mapping(args.scope)
    if args.deep:
        print("== 云上资源真实认证 ==")
        check_cloud_deep()

    fails = [r for r in results if r[0] == "FAIL"]
    passed = sum(1 for r in results if r[0] == "PASS")
    print()
    print(f"SUMMARY: {len(results)} 项，{len(fails)} FAIL，{passed} PASS")
    for _level, name, detail in fails:
        print(f"  FAILED: {name} {detail}")
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(main())
