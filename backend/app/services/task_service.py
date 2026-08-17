"""平台任务服务：轮询上游任务、映射平台状态、刷新文档快照、replace 状态机。

冻结规则（Stage 3 §十八/§二十/§三十~§三十三）：
- 上游 pending/processing/completed/failed → pending/running/succeeded/failed；
  未知状态 → 平台 running + 保留原文，绝不映射 succeeded；
- terminal 只依据明确 terminal status，done_nodes 非空不视为成功；
- succeeded 必须再读上游文档快照刷新 managed_document；
- replace 状态机：新文档失败 → 旧文档保持 active 且不删除；
  新文档成功后才允许删除旧文档（SELECT ... FOR UPDATE 保证只执行一次）。
"""

from app.core.enums import (
    IntegrationOperation,
    IntegrationTaskStatus,
    ManagedDocumentStatus,
    ReplacementStatus,
)
from app.core.errors import not_found
from app.core.time import utc_now_naive
from app.models.rag_integration_task import RagIntegrationTask
from app.models.user import User
from app.rag.rag_document_client import RagDocumentClient, get_rag_document_client
from app.rag.rag_errors import RagError
from app.rag.rag_import_client import RagImportClient, get_rag_import_client
from app.rag.scope_policy import service_user_for_role
from app.repositories.document_replacement_repository import DocumentReplacementRepository
from app.repositories.managed_document_repository import ManagedDocumentRepository
from app.repositories.rag_integration_task_repository import RagIntegrationTaskRepository
from app.schemas.integration import IntegrationTaskView

# 上游状态 → 平台状态（固定映射；未知状态绝不映射 succeeded）
TASK_STATUS_MAP = {
    "pending": IntegrationTaskStatus.pending.value,
    "processing": IntegrationTaskStatus.running.value,
    "completed": IntegrationTaskStatus.succeeded.value,
    "failed": IntegrationTaskStatus.failed.value,
}


def map_upstream_task_status(rag_status: str) -> tuple[str, str]:
    """返回 (platform_status, 上游原文)。未知状态 → (running, 原文)。"""
    platform = TASK_STATUS_MAP.get(rag_status)
    if platform is None:
        return IntegrationTaskStatus.running.value, rag_status
    return platform, rag_status


def safe_error_summary(text: str | None, *, limit: int = 1000) -> str | None:
    """安全错误摘要：截断 + 去除路径类信息，不显示堆栈/密钥。"""
    if not text:
        return None
    cleaned = text.strip()
    # 去除常见 Windows/Unix 路径（可能泄露 file_path/local_dir）
    import re

    cleaned = re.sub(r"[A-Za-z]:[\\/][^\s,;:\"]*", "<path>", cleaned)
    cleaned = re.sub(r"(?:/|\\|\.\./)+[^\s,;:\"]*", "<path>", cleaned)
    cleaned = cleaned.replace("\n", " ").replace("\r", " ")
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned[:limit] or None


def _admin_service_user() -> str:
    return service_user_for_role("admin")


def task_view(task: RagIntegrationTask) -> IntegrationTaskView:
    return IntegrationTaskView(
        id=task.id,
        operation=task.operation,
        status=task.status,
        document_id=task.managed_document_id,
        rag_status=task.rag_status,
        done_nodes=task.done_nodes_json or [],
        running_nodes=task.running_nodes_json or [],
        failed_node=task.failed_node,
        error_code=task.error_code,
        error_message=task.error_message,
        started_at=task.started_at,
        finished_at=task.finished_at,
        updated_at=task.updated_at,
    )


class TaskService:
    def __init__(
        self,
        *,
        tasks: RagIntegrationTaskRepository,
        docs: ManagedDocumentRepository,
        replacements: DocumentReplacementRepository,
        import_client: RagImportClient | None = None,
        document_client: RagDocumentClient | None = None,
    ) -> None:
        self.tasks = tasks
        self.docs = docs
        self.replacements = replacements
        self.import_client = import_client or get_rag_import_client()
        self.document_client = document_client or get_rag_document_client()

    async def get_task(self, task_id: str, *, operator: User) -> IntegrationTaskView:
        """平台任务 → 上游轮询 → 映射 → 更新 → 刷新文档 → replace 状态机 → 视图。

        终态（succeeded/failed/cancelled）不可被重新覆盖：直接返回持久化终态，
        不再刷新上游（避免 replace 删除旧文档失败后，下一次轮询又按上游 completed 改回 succeeded）。
        """
        task = await self.tasks.get_by_id(task_id)
        if task is None:
            raise not_found("任务不存在")
        if task.status in (
            IntegrationTaskStatus.succeeded.value,
            IntegrationTaskStatus.failed.value,
            IntegrationTaskStatus.cancelled.value,
        ):
            # updated_at 由 onupdate=func.now()（SQL 表达式）生成，flush 后本地为过期状态；
            # 返回前必须重新加载，否则触发 async session 懒加载（MissingGreenlet）
            await self.tasks.session.refresh(task)
            return task_view(task)
        await self._refresh_from_upstream(task)
        # updated_at 由 onupdate=func.now()（SQL 表达式）生成，flush 后本地为过期状态；
        # 同步构建视图前必须重新加载，否则触发 async session 懒加载（MissingGreenlet）
        await self.tasks.session.refresh(task)
        return task_view(task)

    # ---------- 上游轮询 ----------

    async def _refresh_from_upstream(self, task: RagIntegrationTask) -> None:
        if not task.rag_task_id:
            return
        upstream = await self.import_client.get_task_status(
            task.rag_task_id, service_user=_admin_service_user()
        )
        if upstream is None:
            # 上游任务记录不存在（可能已清理）：无法确认成功，保守标记 failed
            now = utc_now_naive()
            await self.tasks.update_from_upstream(
                task,
                rag_status=task.rag_status,
                done_nodes=task.done_nodes_json or [],
                running_nodes=task.running_nodes_json or [],
                failed_node=task.failed_node,
                status=IntegrationTaskStatus.failed.value,
                error_code="RAG_UNAVAILABLE",
                error_message="上游任务记录不存在，无法确认结果",
                finished_at=now,
            )
            return

        rag_status_raw = str(upstream.get("status") or "")
        done = _node_list(upstream.get("done_list"))
        running = _node_list(upstream.get("running_list"))
        failed_node = str(upstream.get("failed_node") or "") or None
        platform_status, _kept = map_upstream_task_status(rag_status_raw)
        now = utc_now_naive()

        if platform_status == IntegrationTaskStatus.succeeded.value:
            await self._on_task_succeeded(task, rag_status_raw, done, running, now)
        elif platform_status == IntegrationTaskStatus.failed.value:
            await self._on_task_failed(
                task,
                rag_status_raw,
                done,
                running,
                failed_node,
                upstream,
                now,
            )
        else:
            await self.tasks.update_from_upstream(
                task,
                rag_status=rag_status_raw,
                done_nodes=done,
                running_nodes=running,
                failed_node=failed_node,
                status=platform_status,
            )

        if task.operation == IntegrationOperation.document_replace.value and task.status in (
            IntegrationTaskStatus.succeeded.value,
            IntegrationTaskStatus.failed.value,
        ):
            await self._drive_replace(task)

    async def _on_task_succeeded(
        self, task: RagIntegrationTask, rag_status_raw: str, done: list, running: list, now
    ) -> None:
        """上游 completed：必须再读真实 Document 快照，成功后 platform_status=active。

        done/running 使用本轮上游响应（completed 时 running 必为空），不沿用上一轮缓存。
        """
        rag_document_id = task.rag_document_id
        doc = await self.docs.get_by_rag_document_id(rag_document_id) if rag_document_id else None
        snapshot = None
        if rag_document_id:
            snapshot = await self.document_client.get_document(
                rag_document_id, service_user=_admin_service_user()
            )
        if snapshot is None:
            # 任务 completed 但文档快照缺失：不能伪造成功
            await self.tasks.update_from_upstream(
                task,
                rag_status=rag_status_raw,
                done_nodes=done,
                running_nodes=running,
                failed_node=None,
                status=IntegrationTaskStatus.failed.value,
                error_code="RAG_BAD_RESPONSE",
                error_message="上游任务已完成但文档快照不可用",
                finished_at=now,
            )
            return
        if doc is not None:
            doc.rag_status = str(snapshot.get("status") or "completed")
            doc.rag_parse_status = str(snapshot.get("parse_status") or "") or None
            doc.rag_index_status = str(snapshot.get("index_status") or "") or None
            doc.index_version = int(snapshot.get("index_version") or 0)
            doc.chunk_count = int(snapshot.get("chunk_count") or 0)
            doc.latest_rag_task_id = str(snapshot.get("latest_task_id") or "") or None
            doc.platform_status = ManagedDocumentStatus.active.value
            doc.error_code = None
            doc.error_message = None
            doc.updated_at = now
            await self.docs.update_snapshot(doc)
        await self.tasks.update_from_upstream(
            task,
            rag_status=rag_status_raw,
            done_nodes=done,
            running_nodes=running,
            failed_node=None,
            status=IntegrationTaskStatus.succeeded.value,
            error_code=None,
            error_message=None,
            finished_at=now,
        )

    async def _on_task_failed(
        self,
        task: RagIntegrationTask,
        rag_status_raw: str,
        done: list,
        running: list,
        failed_node: str | None,
        upstream: dict,
        now,
    ) -> None:
        """上游 failed：尽力读取 Document 快照，平台标 import_failed，不伪造成功。"""
        rag_document_id = task.rag_document_id
        doc = await self.docs.get_by_rag_document_id(rag_document_id) if rag_document_id else None
        snapshot = None
        if rag_document_id:
            try:
                snapshot = await self.document_client.get_document(
                    rag_document_id, service_user=_admin_service_user()
                )
            except RagError:
                snapshot = None
        if doc is not None:
            if snapshot:
                doc.rag_status = str(snapshot.get("status") or "failed")
                doc.rag_parse_status = str(snapshot.get("parse_status") or "") or None
                doc.rag_index_status = str(snapshot.get("index_status") or "") or None
                doc.index_version = int(snapshot.get("index_version") or 0)
                doc.chunk_count = int(snapshot.get("chunk_count") or 0)
                doc.latest_rag_task_id = str(snapshot.get("latest_task_id") or "") or None
            else:
                doc.rag_status = "failed"
            doc.platform_status = ManagedDocumentStatus.import_failed.value
            doc.error_code = safe_error_summary(
                str(upstream.get("error_code") or "") or None, limit=100
            )
            doc.error_message = safe_error_summary(str(upstream.get("error_message") or "") or None)
            doc.updated_at = now
            await self.docs.update_snapshot(doc)
        await self.tasks.update_from_upstream(
            task,
            rag_status=rag_status_raw,
            done_nodes=done,
            running_nodes=running,
            failed_node=failed_node,
            status=IntegrationTaskStatus.failed.value,
            error_code=safe_error_summary(str(upstream.get("error_code") or "") or None, limit=100),
            error_message=safe_error_summary(str(upstream.get("error_message") or "") or None),
            finished_at=now,
        )

    # ---------- replace 状态机 ----------

    async def _drive_replace(self, task: RagIntegrationTask) -> None:
        """replace 任务终态后驱动旧文档替换（删除旧文档只允许一个轮询进入）。"""
        replacement = await self.replacements.get_by_task_id(task.id)
        if replacement is None:
            return

        if task.status == IntegrationTaskStatus.failed.value:
            # 新文档导入失败：旧文档必须保持 active，绝不删除
            await self.replacements.mark_failed(
                replacement, error_message="新文档导入失败，旧文档保持有效"
            )
            return

        # 新文档 succeeded：加行锁，只有 status==pending 的那次轮询进入删除区
        locked = await self.replacements.get_by_task_id_for_update(task.id)
        if locked is None or locked.status != ReplacementStatus.pending.value:
            return  # 已被其他轮询处理（或异常），不重复删除
        old_doc = await self.docs.get_by_id(locked.old_managed_document_id)
        if old_doc is None:
            await self.replacements.mark_failed(locked, error_message="旧文档映射不存在")
            return
        try:
            await self.document_client.delete(
                old_doc.rag_document_id, service_user=_admin_service_user()
            )
        except RagError as exc:
            # 新文档成功但旧文档删除失败：new active、old active、replacement/task failed
            await self.replacements.mark_failed(
                locked, error_message=safe_error_summary(str(exc.message))
            )
            await self.tasks.update_from_upstream(
                task,
                rag_status=task.rag_status,
                done_nodes=task.done_nodes_json or [],
                running_nodes=task.running_nodes_json or [],
                failed_node=None,
                status=IntegrationTaskStatus.failed.value,
                error_code=exc.code,
                error_message="新文档已导入，但旧文档清理失败，请人工处理",
                finished_at=utc_now_naive(),
            )
            return
        # 删除成功：旧文档标记 replaced，replacement completed
        await self.docs.mark_replaced(old_doc)
        await self.replacements.mark_completed(locked, completed_at=utc_now_naive())


def _node_list(value) -> list:
    if not isinstance(value, list):
        return []
    items = []
    for node in value:
        if isinstance(node, dict):
            items.append(node)
        elif node is not None:
            items.append({"name": str(node)})
    return items
