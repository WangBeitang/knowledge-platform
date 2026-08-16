"""FastAPI 依赖：数据库会话、Redis、当前用户（认证依赖阶段 2 补充）。"""

from app.core.database import get_db
from app.core.redis import get_redis_dependency

__all__ = ["get_db", "get_redis_dependency"]
