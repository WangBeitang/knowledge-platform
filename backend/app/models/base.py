"""ORM 基类：DeclarativeBase + UUID 主键 + 时间戳 mixin。

所有表模型在 app/models/__init__.py 中统一导入（供 Alembic autogenerate 收集）。
"""

import uuid
from datetime import datetime

from sqlalchemy import func, text
from sqlalchemy.dialects.mysql import CHAR, DATETIME
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


def _uuid4_str() -> str:
    return str(uuid.uuid4())


class UUIDPrimaryKeyMixin:
    """平台主键统一 CHAR(36) UUID。"""

    id: Mapped[str] = mapped_column(CHAR(36), primary_key=True, default=_uuid4_str)


class TimestampMixin:
    """created_at/updated_at：冻结要求 DATETIME(6)（微秒精度），默认 CURRENT_TIMESTAMP(6)。"""

    created_at: Mapped[datetime] = mapped_column(
        DATETIME(fsp=6), nullable=False, server_default=text("CURRENT_TIMESTAMP(6)")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DATETIME(fsp=6),
        nullable=False,
        server_default=text("CURRENT_TIMESTAMP(6)"),
        onupdate=func.now(),
    )


__all__ = ["Base", "UUIDPrimaryKeyMixin", "TimestampMixin"]
