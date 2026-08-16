"""文档管理服务（仅管理员）：导入/列表/详情/重建/替换/删除（Stage 3）。

冻结规则：
- 只支持 PDF（大小写不敏感 .pdf）；非 PDF → rejected，不调用原 RAG；
- 逐文件独立提交（平台 for 循环单发上游 /upload），一个失败不影响其他；
- 文件名只保留安全 basename，不把用户路径传给 RAG；
- 平台不保存 PDF 正文/Chunk；managed_documents 是轻量映射；
- delete 先调上游、成功后才标平台 deleted；
- replace 强制同 scope、旧文档必须 active。
"""

from datetime import datetime

from app.core.enums import (
    AuditAction,
    IntegrationOperation,
    IntegrationTaskStatus,
    KnowledgeScope,
    ManagedDocumentStatus,
    SourceKind,
)
from app.core.errors import bad_request, conflict, not_found
from app.core.time import utc_now_naive
from app.models.managed_document import ManagedDocument
from app.models.rag_integration_task import RagIntegrationTask
from app.models.user import User
from app.rag.rag_document_client import RagDocumentClient, get_rag_document_client
from app.rag.rag_errors import RagError
from app.rag.rag_import_client import RagImportClient, get_rag_import_client
from app.rag.scope_policy import (
    dataset_id_for_scope,
    document_visibility_for_scope,
    service_user_for_role,
)
from app.repositories.document_replacement_repository import DocumentReplacementRepository
from app.repositories.managed_document_repository import ManagedDocumentRepository
from app.repositories.rag_integration_task_repository import RagIntegrationTaskRepository
from app.schemas.document import (
    DocumentImportData,
    DocumentImportItem,
    ManagedDocumentView,
)
from app.services.audit_service import AuditService

ADMIN_SERVICE_USER_CACHE = service_user_for_role("admin")


def _admin_service_user() -> str:
    return service_user_for_role("admin")


def safe_basename(filename: str) -> str:
    """只保留安全 basename：去除 / \\ 路径部分，拒绝空文件名。"""
    name = (filename or "").replace("\\", "/")
    name = name.rsplit("/", 1)[-1].strip()
    return name


def is_pdf_filename(filename: str) -> bool:
    return safe_basename(filename).lower().endswith(".pdf")


def document_view(doc: ManagedDocument) -> ManagedDocumentView:
    return ManagedDocumentView(
        id=doc.id,
        rag_document_id=doc.rag_document_id,
        rag_dataset_id=doc.rag_dataset_id,
        knowledge_scope=doc.knowledge_scope,
        file_name=doc.file_name,
        source_kind=doc.source_kind,
        index_version=doc.index_version,
        rag_status=doc.rag_status,
        rag_parse_status=doc.rag_parse_status,
        rag_index_status=doc.rag_index_status,
        platform_status=doc.platform_status,
        chunk_count=doc.chunk_count,
        latest_rag_task_id=doc.latest_rag_task_id,
        error_code=doc.error_code,
        error_message=doc.error_message,
        created_at=doc.created_at,
        updated_at=doc.updated_at,
    )


class DocumentService:
    def __init__(
        self,
        *,
        docs: ManagedDocumentRepository,
        tasks: RagIntegrationTaskRepository,
        replacements: DocumentReplacementRepository,
        audit: AuditService,
        import_client: RagImportClient | None = None,
        document_client: RagDocumentClient | None = None,
    ) -> None:
        self.docs = docs
        self.tasks = tasks
        self.replacements = replacements
        self.audit = audit
        self.import_client = import_client or get_rag_import_client()
        self.document_client = document_client or get_rag_document_client()

    # ---------- 导入 ----------

    async def import_documents(
        self,
        *,
        operator: User,
        knowledge_scope: str,
        files: list[tuple[str, bytes]],
        client_ip: str | None,
    ) -> DocumentImportData:
        scope = self._validate_scope(knowledge_scope)
        dataset_id = dataset_id_for_scope(scope)
        if not dataset_id:
            raise bad_request("该知识范围的 Dataset 未配置")
        visibility = document_visibility_for_scope(scope)
        service_user = _admin_service_user()
        now = utc_now_naive()

        items: list[DocumentImportItem] = []
        submitted = 0
        rejected = 0
        for file_name, file_bytes in files:
            safe_name = safe_basename(file_name)
            if not safe_name:
                rejected += 1
                items.append(
                    DocumentImportItem(
                        file_name=file_name or "",
                        status="rejected",
                        error=_unsupported_error("文件名无效"),
                    )
                )
                continue
            if not is_pdf_filename(safe_name):
                rejected += 1
                items.append(
                    DocumentImportItem(
                        file_name=safe_name,
                        status="rejected",
                        error=_unsupported_error("仅支持 PDF 文件"),
                    )
                )
                continue
            try:
                uploaded = await self.import_client.upload_file(
                    file_name=safe_name,
                    file_bytes=file_bytes,
                    dataset_id=dataset_id,
                    visibility=visibility,
                    service_user=service_user,
                )
            except RagError as exc:
                rejected += 1
                items.append(
                    DocumentImportItem(
                        file_name=safe_name,
                        status="rejected",
                        error={
                            "code": exc.code,
                            "message": exc.message,
                            "retryable": exc.retryable,
                        },
                    )
                )
                continue
            doc, task = await self._create_mapping(
                operator=operator,
                scope=scope,
                dataset_id=dataset_id,
                safe_name=safe_name,
                uploaded=uploaded,
                operation=IntegrationOperation.document_import.value,
                now=now,
            )
            submitted += 1
            items.append(
                DocumentImportItem(
                    file_name=safe_name,
                    document_id=doc.id,
                    task_id=task.id,  # 平台 task id（前端轮询平台，不接触上游 id）
                    status="pending",
                )
            )

        # 多文件 import 汇总审计（不逐 rejected 文件写审计）
        await self.audit.record(
            operator_user_id=operator.id,
            action=AuditAction.document_import.value,
            resource_type="document",
            resource_id=",".join(item.document_id or "" for item in items if item.document_id)[:500]
            or "-",
            result="succeeded" if submitted else "failed",
            after={
                "knowledge_scope": scope,
                "submitted_count": submitted,
                "rejected_count": rejected,
            },
            client_ip=client_ip,
        )
        return DocumentImportData(
            knowledge_scope=scope,
            submitted_count=submitted,
            rejected_count=rejected,
            items=items,
        )

    async def _create_mapping(
        self,
        *,
        operator: User,
        scope: str,
        dataset_id: str,
        safe_name: str,
        uploaded: dict,
        operation: str,
        now: datetime,
    ) -> tuple[ManagedDocument, RagIntegrationTask]:
        doc = ManagedDocument(
            rag_document_id=uploaded["rag_document_id"],
            rag_dataset_id=dataset_id,
            knowledge_scope=scope,
            file_name=safe_name,
            source_kind=SourceKind.manual_upload.value,
            index_version=uploaded["index_version"],
            platform_status=ManagedDocumentStatus.importing.value,
            rag_status=IntegrationTaskStatus.pending.value,
            rag_parse_status=None,
            rag_index_status=None,
            chunk_count=0,
            latest_rag_task_id=uploaded["rag_task_id"],
            created_by_user_id=operator.id,
            created_at=now,
            updated_at=now,
        )
        doc = await self.docs.add(doc)
        task = RagIntegrationTask(
            operation=operation,
            status=IntegrationTaskStatus.pending.value,
            managed_document_id=doc.id,
            rag_task_id=uploaded["rag_task_id"],
            rag_document_id=uploaded["rag_document_id"],
            rag_dataset_id=dataset_id,
            rag_status=IntegrationTaskStatus.pending.value,
            done_nodes_json=[],
            running_nodes_json=[],
            requested_by_user_id=operator.id,
            started_at=now,
            updated_at=now,
        )
        await self.tasks.add(task)
        return doc, task

    # ---------- 列表 / 详情 ----------

    async def list_documents(
        self,
        *,
        page: int,
        page_size: int,
        knowledge_scope: str | None,
        platform_status: str | None,
        file_name: str | None,
        source_kind: str | None,
        sort_by: str,
        sort_order: str,
    ) -> tuple[list[ManagedDocumentView], int]:
        rows, total = await self.docs.list_page(
            page=page,
            page_size=page_size,
            knowledge_scope=knowledge_scope,
            platform_status=platform_status,
            file_name=file_name,
            source_kind=source_kind,
            sort_by=sort_by,
            sort_order=sort_order,
        )
        return [document_view(row) for row in rows], total

    async def get_document(self, document_id: str) -> ManagedDocumentView:
        """平台 ID → 映射 → 上游真实快照 → 更新轻量快照 → 视图。"""
        doc = await self.docs.get_active(document_id)
        if doc is None:
            raise not_found("文档不存在")
        snapshot = await self.document_client.get_document(
            doc.rag_document_id, service_user=_admin_service_user()
        )
        if snapshot is not None:
            now = utc_now_naive()
            doc.rag_status = str(snapshot.get("status") or doc.rag_status)
            doc.rag_parse_status = str(snapshot.get("parse_status") or "") or None
            doc.rag_index_status = str(snapshot.get("index_status") or "") or None
            doc.index_version = int(snapshot.get("index_version") or doc.index_version)
            doc.chunk_count = int(snapshot.get("chunk_count") or 0)
            doc.latest_rag_task_id = str(snapshot.get("latest_task_id") or "") or None
            doc.updated_at = now
            await self.docs.update_snapshot(doc)
        return document_view(doc)

    # ---------- rebuild ----------

    async def rebuild_document(
        self,
        *,
        operator: User,
        document_id: str,
        client_ip: str | None,
    ) -> tuple[str, str, str]:
        """重建索引：上游 202 级语义（立即返回 task_id）。

        返回 (task_id, document_id, operation)。
        """
        doc = await self.docs.get_active(document_id)
        if doc is None:
            raise not_found("文档不存在")
        if doc.platform_status not in (
            ManagedDocumentStatus.active.value,
            ManagedDocumentStatus.import_failed.value,
        ):
            raise conflict("当前文档状态不允许重建")
        result = await self.document_client.rebuild(
            doc.rag_document_id, service_user=_admin_service_user()
        )
        if result is None:
            raise not_found("上游文档不存在")
        now = utc_now_naive()
        task = RagIntegrationTask(
            operation=IntegrationOperation.document_rebuild.value,
            status=IntegrationTaskStatus.pending.value,
            managed_document_id=doc.id,
            rag_task_id=result["task_id"],
            rag_document_id=doc.rag_document_id,
            rag_dataset_id=doc.rag_dataset_id,
            rag_status=IntegrationTaskStatus.pending.value,
            done_nodes_json=[],
            running_nodes_json=[],
            requested_by_user_id=operator.id,
            started_at=now,
            updated_at=now,
        )
        task = await self.tasks.add(task)
        await self.audit.record(
            operator_user_id=operator.id,
            action=AuditAction.document_rebuild.value,
            resource_type="document",
            resource_id=doc.id,
            result="succeeded",
            after={"knowledge_scope": doc.knowledge_scope, "rag_document_id": doc.rag_document_id},
            client_ip=client_ip,
        )
        return task.id, doc.id, task.operation

    # ---------- delete ----------

    async def delete_document(
        self,
        *,
        operator: User,
        document_id: str,
        client_ip: str | None,
    ) -> str:
        """先调上游删除，成功后才平台 mark deleted。"""
        doc = await self.docs.get_active(document_id)
        if doc is None:
            raise not_found("文档不存在")
        if doc.platform_status not in (
            ManagedDocumentStatus.active.value,
            ManagedDocumentStatus.import_failed.value,
        ):
            raise conflict("当前文档状态不允许删除")
        result = await self.document_client.delete(
            doc.rag_document_id, service_user=_admin_service_user()
        )
        if result is None:
            # 上游文档已不存在：等价删除完成
            pass
        await self.docs.mark_deleted(doc, deleted_at=utc_now_naive())
        await self.audit.record(
            operator_user_id=operator.id,
            action=AuditAction.document_delete.value,
            resource_type="document",
            resource_id=doc.id,
            result="succeeded",
            after={
                "knowledge_scope": doc.knowledge_scope,
                "file_name": doc.file_name,
                "rag_document_id": doc.rag_document_id,
            },
            client_ip=client_ip,
        )
        return doc.id

    # ---------- replace ----------

    async def replace_document(
        self,
        *,
        operator: User,
        document_id: str,
        knowledge_scope: str,
        file_name: str,
        file_bytes: bytes,
        client_ip: str | None,
    ) -> tuple[str, str, str]:
        """替换：旧文档 active + 同 scope + PDF → 上传新文档 → 建 replace 任务。"""
        old = await self.docs.get_active(document_id)
        if old is None:
            raise not_found("文档不存在")
        if old.platform_status != ManagedDocumentStatus.active.value:
            raise conflict("仅 active 文档支持替换")
        scope = self._validate_scope(knowledge_scope)
        if scope != old.knowledge_scope:
            raise bad_request("replace 不是迁移知识范围接口，知识范围必须与旧文档一致")
        safe_name = safe_basename(file_name)
        if not safe_name or not is_pdf_filename(safe_name):
            raise bad_request("仅支持 PDF 文件")
        dataset_id = dataset_id_for_scope(scope)
        if not dataset_id:
            raise bad_request("该知识范围的 Dataset 未配置")
        visibility = document_visibility_for_scope(scope)
        service_user = _admin_service_user()
        now = utc_now_naive()

        uploaded = await self.import_client.upload_file(
            file_name=safe_name,
            file_bytes=file_bytes,
            dataset_id=dataset_id,
            visibility=visibility,
            service_user=service_user,
        )
        new_doc, task = await self._create_mapping(
            operator=operator,
            scope=scope,
            dataset_id=dataset_id,
            safe_name=safe_name,
            uploaded=uploaded,
            operation=IntegrationOperation.document_replace.value,
            now=now,
        )
        from app.models.document_replacement import DocumentReplacement

        replacement = DocumentReplacement(
            old_managed_document_id=old.id,
            new_managed_document_id=new_doc.id,
            replacement_task_id=task.id,
            status="pending",
            created_by_user_id=operator.id,
            created_at=now,
        )
        replacement = await self.replacements.add(replacement)
        await self.audit.record(
            operator_user_id=operator.id,
            action=AuditAction.document_replace.value,
            resource_type="document",
            resource_id=f"{old.id}->{new_doc.id}",
            result="succeeded",
            after={
                "knowledge_scope": scope,
                "file_name": safe_name,
                "replacement_id": replacement.id,
            },
            client_ip=client_ip,
        )
        return task.id, new_doc.id, replacement.id

    @staticmethod
    def _validate_scope(scope: str) -> str:
        try:
            return KnowledgeScope(scope).value
        except ValueError:
            raise bad_request("非法知识范围") from None


def _unsupported_error(message: str) -> dict:
    return {
        "code": "UNSUPPORTED_FILE_TYPE",
        "message": message,
        "retryable": False,
    }
