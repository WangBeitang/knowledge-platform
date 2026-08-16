"""users 表数据访问。"""

from sqlalchemy import func, select

from app.core.enums import UserRole, UserStatus
from app.models.user import User
from app.repositories.base import BaseRepository

# 用户列表可排序白名单（冻结契约：禁止任意列排序）
USER_SORT_WHITELIST = {"username", "display_name", "role", "status", "created_at", "updated_at"}


class UserRepository(BaseRepository[User]):
    model = User

    async def get_by_username(self, username: str) -> User | None:
        stmt = select(User).where(User.username == username)
        return await self.session.scalar(stmt)

    async def count_active_admins(self) -> int:
        stmt = (
            select(func.count())
            .select_from(User)
            .where(
                User.role == UserRole.admin.value,
                User.status == UserStatus.active.value,
            )
        )
        return int(await self.session.scalar(stmt) or 0)

    async def lock_active_admins(self) -> list[User]:
        """SELECT ... FOR UPDATE 锁定当前全部 active admin 行（同一事务内使用）。

        用于“最后 active admin”保护的并发窗口消除：锁定后重算数量，与后续
        UPDATE 在同一事务内，避免 count → update 之间其他请求把数量降为 0。
        """
        stmt = (
            select(User)
            .where(
                User.role == UserRole.admin.value,
                User.status == UserStatus.active.value,
            )
            .with_for_update()
        )
        return list((await self.session.scalars(stmt)).all())

    async def is_active_admin(self, user_id: str) -> bool:
        stmt = select(User).where(
            User.id == user_id,
            User.role == UserRole.admin.value,
            User.status == UserStatus.active.value,
        )
        return await self.session.scalar(stmt) is not None

    async def list_page(
        self,
        *,
        page: int = 1,
        page_size: int = 20,
        sort_by: str = "created_at",
        sort_order: str = "desc",
    ) -> tuple[list[User], int]:
        if sort_by not in USER_SORT_WHITELIST:
            sort_by = "created_at"
        return await super().list_page(
            page=page,
            page_size=page_size,
            sort_by=sort_by,
            sort_order=sort_order,
        )

    async def touch_login(self, user: User, last_login_at) -> None:
        """更新最近登录时间。"""
        user.last_login_at = last_login_at
        await self.session.flush()
