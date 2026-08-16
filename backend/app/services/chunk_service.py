"""Chunk 管理服务（仅管理员）：分页列表、详情、人工启停（Stage 3）。

Adapter 字段映射（§三十六/§三十七/§三十八）：
- position ← chunk_index
- 列表 text ← content_preview；详情 text ← content
- enabled ← effective_enabled（业务上“不参与查询”的状态，非 Milvus base enabled）
- disabled_reason_code ← latest_event.reason_type
- disabled_reason_text ← latest_event.reason_detail
- metadata 只保留安全白名单字段
禁止 Chunk 正文编辑（无 update_text/PATCH text）。
"""

from typing import Any

from app.core.enums import AuditAction
from app.core.errors import bad_request, not_found
from app.models.user import User
from app.rag.rag_document_client import (
    RagDocumentClient,
    get_rag_document_client,
)
from app.rag.scope_policy import service_user_for_role
from app.repositories.managed_document_repository import ManagedDocumentRepository
from app.schemas.chunk import ChunkView
from app.services.audit_service import AuditService

# 允许透传展示的安全 metadata 白名单（§三十六）
CHUNK_METADATA_WHITELIST = {
    "title",
    "parent_title",
    "source_title",
    "content_length",
    "subject_id",
    "standard_subject_name",
    "equipment_model",
    "alarm_code",
    "part_name",
    "sop_type",
    "safety_level",
    "maintenance_stage",
}

# 启停原因类型（§三十七）
CHUNK_REASON_TYPES = {
    "parse_error",
    "header_footer",
    "garbled_text",
    "outdated_content",
    "human_misjudgment",
    "manual_restore",
    "other",
}

MAX_PAGE_SIZE = 100


def _admin_service_user() -> str:
    return service_user_for_role("admin")


def _safe_metadata(raw: dict | None) -> dict[str, Any]:
    if not isinstance(raw, dict):
        return {}
    return {key: raw[key] for key in CHUNK_METADATA_WHITELIST if key in raw}


def _latest_event(item: dict) -> dict:
    event = item.get("latest_event")
    return event if isinstance(event, dict) else {}


def chunk_view_from_list_item(
    item: dict, *, platform_document_id: str, index_version: int
) -> ChunkView:
    event = _latest_event(item)
    return ChunkView(
        chunk_id=str(item.get("chunk_id") or ""),
        document_id=platform_document_id,
        index_version=index_version,
        position=int(item.get("chunk_index") or 0),
        text=str(item.get("content_preview") or item.get("content") or ""),
        enabled=bool(item.get("effective_enabled", True)),
        disabled_reason_code=str(event.get("reason_type") or "") or None,
        disabled_reason_text=str(event.get("reason_detail") or "") or None,
        metadata=_safe_metadata(item),
    )


def chunk_view_from_detail(
    item: dict, *, platform_document_id: str, index_version: int
) -> ChunkView:
    event = _latest_event(item)
    return ChunkView(
        chunk_id=str(item.get("chunk_id") or ""),
        document_id=platform_document_id,
        index_version=index_version,
        position=int(item.get("chunk_index") or 0),
        text=str(item.get("content") or ""),
        enabled=bool(item.get("effective_enabled", True)),
        disabled_reason_code=str(event.get("reason_type") or "") or None,
        disabled_reason_text=str(event.get("reason_detail") or "") or None,
        metadata=_safe_metadata(item),
    )


class ChunkService:
    def __init__(
        self,
        *,
        docs: ManagedDocumentRepository,
        audit: AuditService,
        document_client: RagDocumentClient | None = None,
    ) -> None:
        self.docs = docs
        self.audit = audit
        self.document_client = document_client or get_rag_document_client()

    async def _active_document(self, document_id: str):
        doc = await self.docs.get_active(document_id)
        if doc is None:
            raise not_found("文档不存在")
        return doc

    async def list_chunks(
        self, *, document_id: str, page: int, page_size: int
    ) -> tuple[list[ChunkView], int, int, int]:
        """分页列表：page/page_size → offset/limit → 上游分页 → Adapter 映射。

        total 取刚刷新过的 managed_document.chunk_count（当前版本全部 Chunk 数）。
        """
        if page < 1:
            page = 1
        page_size = min(max(page_size, 1), MAX_PAGE_SIZE)
        doc = await self._active_document(document_id)
        offset = (page - 1) * page_size
        payload = await self.document_client.list_chunks(
            doc.rag_document_id,
            offset=offset,
            limit=page_size,
            enabled="all",
            service_user=_admin_service_user(),
        )
        items = [
            chunk_view_from_list_item(
                item, platform_document_id=doc.id, index_version=doc.index_version
            )
            for item in payload.get("items", [])
        ]
        total = int(doc.chunk_count or 0)
        return items, total, page, page_size

    async def get_chunk(self, *, document_id: str, chunk_id: str) -> ChunkView:
        doc = await self._active_document(document_id)
        item = await self.document_client.get_chunk(
            doc.rag_document_id, chunk_id, service_user=_admin_service_user()
        )
        if item is None:
            raise not_found("Chunk 不存在")
        return chunk_view_from_detail(
            item, platform_document_id=doc.id, index_version=doc.index_version
        )

    async def set_enabled(
        self,
        *,
        operator: User,
        document_id: str,
        chunk_id: str,
        enabled: bool,
        reason_code: str,
        reason_text: str,
        expected_index_version: int,
        client_ip: str | None,
    ) -> dict:
        doc = await self._active_document(document_id)
        if reason_code not in CHUNK_REASON_TYPES:
            raise bad_request("非法原因类型")
        if reason_code == "other" and not reason_text.strip():
            raise bad_request("other 原因必须填写说明")
        try:
            result = await self.document_client.set_chunk_enabled(
                doc.rag_document_id,
                chunk_id,
                enabled=enabled,
                expected_index_version=expected_index_version,
                reason_type=reason_code,
                reason_detail=reason_text.strip(),
                service_user=_admin_service_user(),
            )
        except Exception as exc:
            # INDEX_VERSION_CONFLICT 等稳定错误码直接上抛（由异常处理中间件映射）
            raise exc
        await self.audit.record(
            operator_user_id=operator.id,
            action=AuditAction.chunk_status_changed.value,
            resource_type="chunk",
            resource_id=f"{doc.id}:{chunk_id}",
            result="succeeded",
            after={
                "knowledge_scope": doc.knowledge_scope,
                "chunk_id": chunk_id,
                "enabled": enabled,
                "reason_code": reason_code,
                "index_version": expected_index_version,
            },
            client_ip=client_ip,
        )
        return {
            "document_id": doc.id,
            "chunk_id": chunk_id,
            "index_version": int(result.get("index_version") or doc.index_version),
            "enabled": bool(result.get("effective_enabled", enabled)),
        }
