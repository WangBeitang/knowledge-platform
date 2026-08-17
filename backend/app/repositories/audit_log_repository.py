"""audit_logs 表数据访问：最小写入（阶段 2）；查询补全（Stage 5 Batch 3）。"""

from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.orm import aliased

from app.models.audit_log import AuditLog
from app.models.user import User
from app.repositories.base import BaseRepository
from app.schemas.audit import AUDIT_SORT_WHITELIST


class AuditLogRepository(BaseRepository[AuditLog]):
    model = AuditLog

    async def create_record(
        self,
        *,
        request_id: str,
        operator_user_id: str,
        action: str,
        resource_type: str,
        resource_id: str | None,
        result: str,
        before_json: dict | None = None,
        after_json: dict | None = None,
        error_code: str | None = None,
        client_ip: str | None = None,
        created_at: datetime,
    ) -> AuditLog:
        record = AuditLog(
            request_id=request_id,
            operator_user_id=operator_user_id,
            action=action,
            resource_type=resource_type,
            resource_id=resource_id,
            result=result,
            before_json=before_json,
            after_json=after_json,
            error_code=error_code,
            client_ip=client_ip,
            created_at=created_at,
        )
        return await self.add(record)

    async def list_page(
        self,
        *,
        page: int = 1,
        page_size: int = 20,
        action: str | None = None,
        operator_user_id: str | None = None,
        resource_type: str | None = None,
        result: str | None = None,
        date_from: datetime | None = None,
        date_to: datetime | None = None,
        sort_by: str = "created_at",
        sort_order: str = "desc",
    ) -> tuple[list[tuple[AuditLog, str | None]], int]:
        """分页查询审计（冻结字段筛选 + 白名单排序），左连接 users 取操作人展示名。

        安全边界：只返回 audit_logs 冻结字段；before/after 为写入方打码后的
        安全快照，密码/JWT/密钥等敏感键已被 AuditService 拦截。
        """
        if sort_by not in AUDIT_SORT_WHITELIST:
            sort_by = "created_at"
        operator_alias = aliased(User)
        column = getattr(AuditLog, sort_by)
        order = column.desc() if sort_order == "desc" else column.asc()

        conditions = []
        if action:
            conditions.append(AuditLog.action == action)
        if operator_user_id:
            conditions.append(AuditLog.operator_user_id == operator_user_id)
        if resource_type:
            conditions.append(AuditLog.resource_type == resource_type)
        if result:
            conditions.append(AuditLog.result == result)
        if date_from is not None:
            conditions.append(AuditLog.created_at >= date_from)
        if date_to is not None:
            conditions.append(AuditLog.created_at <= date_to)

        count_stmt = select(func.count()).select_from(AuditLog).where(*conditions)
        total = await self.session.scalar(count_stmt) or 0

        stmt = (
            select(AuditLog, operator_alias.username)
            .outerjoin(operator_alias, operator_alias.id == AuditLog.operator_user_id)
            .where(*conditions)
            .order_by(order, AuditLog.id)
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
        rows = list((await self.session.execute(stmt)).all())
        return rows, int(total)
