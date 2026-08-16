"""健康检查：live 仅进程存活；ready 逐组件探测，不泄漏连接串。"""

import httpx
from fastapi import APIRouter
from pydantic import BaseModel
from sqlalchemy import text

from app.core.config import get_settings
from app.core.database import get_db
from app.core.redis import get_redis
from app.core.request_context import get_request_id
from app.schemas.common import ApiResponse

router = APIRouter(prefix="/health", tags=["health"])


class ComponentStatus(BaseModel):
    status: str  # ok | degraded


class ReadyData(BaseModel):
    status: str  # ok | degraded
    components: dict[str, ComponentStatus]


@router.get("/live", response_model=ApiResponse[dict])
async def live_check() -> ApiResponse[dict]:
    return ApiResponse(request_id=get_request_id(), data={"status": "ok"})


async def _probe_mysql() -> str:
    try:
        async for session in get_db():
            await session.execute(text("SELECT 1"))
        return "ok"
    except Exception:
        return "degraded"


async def _probe_redis() -> str:
    try:
        client = await get_redis()
        return "ok" if client is not None else "degraded"
    except Exception:
        return "degraded"


async def _probe_rag(base_url: str) -> str:
    """RAG 本地未启动属预期，返回 degraded 而非 error。仅 HTTP 200 视为 ok。"""
    if not base_url:
        return "degraded"
    try:
        async with httpx.AsyncClient(timeout=2.0) as client:
            resp = await client.get(f"{base_url}/health")
        return "ok" if resp.status_code == 200 else "degraded"
    except Exception:
        return "degraded"


@router.get("/ready", response_model=ApiResponse[ReadyData])
async def ready_check() -> ApiResponse[ReadyData]:
    settings = get_settings()
    components = {
        "mysql": ComponentStatus(status=await _probe_mysql()),
        "redis": ComponentStatus(status=await _probe_redis()),
        "rag_query": ComponentStatus(status=await _probe_rag(settings.rag_query_base_url)),
        "rag_import": ComponentStatus(status=await _probe_rag(settings.rag_import_base_url)),
    }
    status = "ok" if all(c.status == "ok" for c in components.values()) else "degraded"
    return ApiResponse(
        request_id=get_request_id(),
        data=ReadyData(status=status, components=components),
    )
