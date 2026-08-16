"""审计服务：阶段 2 最小 `record`，供各写操作 service 调用。

安全边界（冻结）：禁止记录明文密码、password_hash、JWT、jti 原值、
Service API Key、RAG 密钥。before/after 由调用方传入安全快照。
"""

from typing import Any

from app.core.enums import AuditResult
from app.core.request_context import get_request_id
from app.core.time import utc_now_naive
from app.repositories.audit_log_repository import AuditLogRepository

# 审计快照中禁止出现的敏感键（记录时兜底打码）
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
