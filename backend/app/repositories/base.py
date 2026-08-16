"""通用仓储骨架：get_by_id / list_page / 基本写操作。

只依赖 models / core，不含业务规则。各 Repository 继承后补充专属查询。
"""

from typing import Generic, TypeVar

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import DeclarativeBase

ModelT = TypeVar("ModelT", bound=DeclarativeBase)


class BaseRepository(Generic[ModelT]):
    """最小通用骨架：按主键查询、软分页列表、新增。"""

    model: type[ModelT]

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get_by_id(self, id_value: str) -> ModelT | None:
        stmt = select(self.model).where(self.model.id == id_value)
        return await self.session.scalar(stmt)

    async def list_page(
        self,
        *,
        page: int = 1,
        page_size: int = 20,
        sort_by: str = "created_at",
        sort_order: str = "desc",
    ) -> tuple[list[ModelT], int]:
        """通用分页：返回 (items, total)。sort_by 需为模型真实列名。"""
        column = getattr(self.model, sort_by, None)
        order_col = column if column is not None else self.model.created_at
        order = order_col.desc() if sort_order == "desc" else order_col.asc()
        count_stmt = select(func.count()).select_from(self.model)
        total = await self.session.scalar(count_stmt) or 0
        stmt = select(self.model).order_by(order).offset((page - 1) * page_size).limit(page_size)
        rows = list((await self.session.scalars(stmt)).all())
        return rows, int(total)

    async def add(self, instance: ModelT) -> ModelT:
        self.session.add(instance)
        await self.session.flush()
        return instance
