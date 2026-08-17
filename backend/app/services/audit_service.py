"""审计服务：阶段 2 最小 `record`，供各写操作 service 调用；Stage 5 补 `list_logs`。

安全边界（冻结）：禁止记录明文密码、password_hash、JWT、jti 原值、
Service API Key、RAG 密钥。before/after 由调用方传入安全快照。
"""

from datetime import datetime
from typing import Any

from app.core.enums import AuditResult
from app.core.request_context import get_request_id
from app.core.time import utc_now_naive
from app.repositories.audit_log_repository import AuditLogRepository
from app.schemas.audit import AuditLogView

# 审计快照中禁止出现的敏感键（记录时兜底打码；含连接串/DSN 相关键）
_AUDIT_SENSITIVE_KEYS = {
    "password",
    "password_hash",
    "initial_password",
    "new_password",
    "access_token",
    "jwt",
    "jti",
    "service_api_key",
    "secret_key",
    # 连接串与数据库凭据（冻结：禁止暴露连接串）
    "connection_string",
    "database_url",
    "dsn",
    "db_password",
    "connection",
    "uri",
}


def _sanitize(payload: dict[str, Any] | None) -> dict[str, Any] | None:
    """审计快照安全兜底：任何敏感键值一律打码，防止调用方误传。"""
    if payload is None:
        return None
    return {
        k: ("***" if any(s in k.lower() for s in _AUDIT_SENSITIVE_KEYS) else v)
        for k, v in payload.items()
    }


class AuditService:
    def __init__(self, repository: AuditLogRepository) -> None:
        self.repository = repository

    async def record(
        self,
        *,
        operator_user_id: str,
        action: str,
        resource_type: str,
        resource_id: str | None = None,
        result: str = AuditResult.succeeded.value,
        before: dict[str, Any] | None = None,
        after: dict[str, Any] | None = None,
        error_code: str | None = None,
        client_ip: str | None = None,
    ) -> None:
        """写一条审计记录。request_id 从请求上下文取，保证与响应/日志一致。"""
        await self.repository.create_record(
            request_id=get_request_id(),
            operator_user_id=operator_user_id,
            action=action,
            resource_type=resource_type,
            resource_id=resource_id,
            result=result,
            before_json=_sanitize(before),
            after_json=_sanitize(after),
            error_code=error_code,
            client_ip=client_ip,
            created_at=utc_now_naive(),
        )

    async def list_logs(
        self,
        *,
        page: int,
        page_size: int,
        action: str | None = None,
        operator_user_id: str | None = None,
        resource_type: str | None = None,
        result: str | None = None,
        date_from: datetime | None = None,
        date_to: datetime | None = None,
        sort_by: str = "created_at",
        sort_order: str = "desc",
    ) -> tuple[list[AuditLogView], int]:
        """审计查询（仅管理员）：按冻结分页/筛选/排序字段，返回安全 DTO。

        响应安全（冻结 §13.3）：只暴露 audit_logs 冻结字段 + 操作人展示名；
        before/after 为已打码安全快照，不含密码/JWT/密钥/完整正文/隐藏推理。
        """
        rows, total = await self.repository.list_page(
            page=page,
            page_size=page_size,
            action=action,
            operator_user_id=operator_user_id,
            resource_type=resource_type,
            result=result,
            date_from=date_from,
            date_to=date_to,
            sort_by=sort_by,
            sort_order=sort_order,
        )
        views = [
            AuditLogView(
                id=record.id,
                request_id=record.request_id,
                operator_user_id=record.operator_user_id,
                operator_username=username,
                action=record.action,
                resource_type=record.resource_type,
                resource_id=record.resource_id,
                result=record.result,
                error_code=record.error_code,
                client_ip=record.client_ip,
                # 查询侧兜底：历史数据若未打码，响应也禁止暴露敏感键
                before=_sanitize(record.before_json),
                after=_sanitize(record.after_json),
                created_at=record.created_at,
            )
            for record, username in rows
        ]
        return views, int(total)
