"""v1 路由汇总。后续阶段按模块追加。"""

from fastapi import APIRouter

from app.api.v1 import health

router = APIRouter(prefix="/api/v1")
router.include_router(health.router)
