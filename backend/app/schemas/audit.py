"""审计日志查询 DTO（仅管理员）（《API 接口设计》§13.3）。

安全边界（冻结）：响应只暴露 audit_logs 冻结字段 + 操作人展示名；
before/after 为写入方已打码的安全快照（AuditService._sanitize 兜底）；
禁止返回密码、JWT、Service API Key、连接串、完整文档正文、模型隐藏推理。
"""

from datetime import datetime

from pydantic import BaseModel

from app.schemas.common import ApiResponse, Page

# 审计列表排序白名单（冻结字段：禁止任意列排序）
AUDIT_SORT_WHITELIST = {"created_at", "action", "resource_type", "result"}


class AuditLogView(BaseModel):
    id: str
    request_id: str
    operator_user_id: str
    operator_username: str | None
    action: str
    resource_type: str
    resource_id: str | None
    result: str
    error_code: str | None
    client_ip: str | None
    before: dict | None
    after: dict | None
    created_at: datetime | None


class AuditListData(Page[AuditLogView]):
    pass


class AuditListResponse(ApiResponse[AuditListData]):
    pass
