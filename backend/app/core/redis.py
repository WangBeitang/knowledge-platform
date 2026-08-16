"""Redis 客户端：不可用时返回 None（降级），不抛错中断业务。"""

from collections.abc import AsyncIterator

import redis.asyncio as aioredis

from app.core.config import get_settings

_client: aioredis.Redis | None = None


async def get_redis() -> aioredis.Redis | None:
    """异步获取可用 Redis 客户端；连接失败返回 None，由调用方走降级路径。"""
    global _client
    if _client is not None:
        try:
            await _client.ping()
            return _client
        except Exception:
            _client = None
    try:
        settings = get_settings()
        candidate = aioredis.Redis(
            host=settings.redis_host,
            port=settings.redis_port,
            password=settings.redis_password or None,
            db=settings.redis_db,
            socket_connect_timeout=3,
            socket_timeout=3,
            decode_responses=True,
        )
        await candidate.ping()
        _client = candidate
        return _client
    except Exception:
        return None


async def get_redis_dependency() -> AsyncIterator[aioredis.Redis | None]:
    """FastAPI 依赖：请求内 Redis；不可用时为 None（上层回退 MySQL）。"""
    yield await get_redis()


async def close_redis() -> None:
    global _client
    if _client is not None:
        try:
            await _client.aclose()
        except Exception:
            pass
    _client = None
