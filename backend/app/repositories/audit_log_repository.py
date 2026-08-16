"""audit_logs 表数据访问：最小写入（阶段 2）；阶段 5 再补查询能力。"""

from datetime import datetime

from app.models.audit_log import AuditLog
from app.repositories.base import BaseRepository


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
