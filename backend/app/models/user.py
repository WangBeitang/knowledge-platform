"""users：平台内部账号（管理员/普通员工），外部用户不入表。"""

from datetime import datetime

from sqlalchemy import Index, String
from sqlalchemy.dialects.mysql import DATETIME
from sqlalchemy.orm import Mapped, mapped_column

from app.core.enums import UserRole, UserStatus
from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class User(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "users"
    __table_args__ = (Index("ix_users_status_role", "status", "role"),)

    username: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    display_name: Mapped[str] = mapped_column(String(100), nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[str] = mapped_column(String(20), nullable=False, default=UserRole.employee.value)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default=UserStatus.active.value)
    password_changed_at: Mapped[datetime] = mapped_column(DATETIME(fsp=6), nullable=False)
    last_login_at: Mapped[datetime | None] = mapped_column(DATETIME(fsp=6), nullable=True)
    created_by_user_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    disabled_at: Mapped[datetime | None] = mapped_column(DATETIME(fsp=6), nullable=True)
