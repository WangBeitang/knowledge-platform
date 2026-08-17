"""FAQ → 原 RAG 文档同步服务（冻结 §2.3 faq_sync_runs + SPEC 状态机）。

每个知识范围独立维护一份 FAQ Markdown 汇总文档，进入对应 Dataset：
- 三档范围分别生成三份文档，不能合成一份后依赖 Chunk 权限过滤；
- 每次正式态变化（发布/修改/下线/重发）只重建该范围 FAQ 文档；
- 请求内直接提交上游上传（无队列/Worker/MQ），记录 rag_task_id；
- 查询 faq-sync-runs 时刷新上游任务状态并驱动旧文档清理状态机；
- 幂等：同 scope + 同 content_hash 已 succeeded → 直接复用，不重复上传；
- 旧文档清理：新文档成功后才删除 previous_rag_document_id；删除成功才 succeeded；
  删除失败 → failed 且保留新旧文档 ID；retry 优先继续旧文档清理，不重复上传新文档；
  行锁（SELECT ... FOR UPDATE）保证删除动作只执行一次。

失败语义（冻结数据对象 §4.11）：MySQL 正式 FAQ 与 Redis 精确缓存继续可用。
"""

from __future__ import annotations

import hashlib
import logging
from datetime import datetime

from app.core.enums import KnowledgeScope, RagSyncStatus
from app.core.time import utc_now_naive
from app.models.faq_sync_run import FaqSyncRun
from app.rag.rag_document_client import RagDocumentClient, get_rag_document_client
from app.rag.rag_errors import RagError
from app.rag.rag_import_client import RagImportClient, get_rag_import_client
from app.rag.scope_policy import (
    dataset_id_for_scope,
    document_visibility_for_scope,
    service_user_for_role,
)
from app.repositories.faq_repository import FaqRepository
from app.repositories.faq_sync_run_repository import FaqSyncRunRepository
from app.schemas.faq import FaqSyncRunView
from app.services.task_service import safe_error_summary

logger = logging.getLogger("app.services.faq_sync_service")


# 固定范围文件名（数据对象 §4.11 generated_file_name：范围固定，不随内容变化）
def _faq_file_name(scope: str) -> str:
    return f"faq_{scope}.md"


_SCOPE_TITLES = {
    KnowledgeScope.external_public.value: "外部公开 FAQ",
    KnowledgeScope.internal_shared.value: "内部共享 FAQ",
    KnowledgeScope.admin_private.value: "管理员专属 FAQ",
}

# 上游任务状态（与 TaskService 共享映射，避免字符串魔法）
_TASK_STATUS_PENDING = {"pending", "processing"}


class FaqSyncService:
    def __init__(
        self,
        *,
        runs: FaqSyncRunRepository,
        faqs: FaqRepository,
        import_client: RagImportClient | None = None,
        document_client: RagDocumentClient | None = None,
    ) -> None:
        self.runs = runs
        self.faqs = faqs
        self.import_client = import_client or get_rag_import_client()
        self.document_client = document_client or get_rag_document_client()

    # ================= 提交同步 =================

    async def submit_faq_sync(self, *, knowledge_scope: str, operator_user_id: str) -> FaqSyncRun:
        """生成该范围 FAQ Markdown 并上传到对应 Dataset（请求内直接提交）。

        幂等：同 scope + 同 content_hash 已有 succeeded 记录 → 直接复用。
        单进行中（首轮复核）：scope 已有 pending/syncing run 时不启动第二次上传
        （当前 FAQ 正式态已保存；旧 run 完成后由 refresh 自动 catch-up）。
        上传失败：创建 failed 同步记录（旧文档不受影响，可重试）。
        """
        scope = _validate_scope(knowledge_scope)
        markdown = await self._generate_markdown(scope)
        content_hash = hashlib.sha256(markdown.encode("utf-8")).hexdigest()

        latest_succeeded = await self.runs.find_latest_succeeded(scope)
        if latest_succeeded is not None and latest_succeeded.content_hash == content_hash:
            return latest_succeeded  # 幂等复用，不重复上传

        # 单进行中防分叉：同一 scope 同时最多一个真实 RAG FAQ upload
        in_progress = await self.runs.find_latest_in_progress(scope)
        if in_progress is not None:
            return in_progress

        file_name = _faq_file_name(scope)
        dataset_id = dataset_id_for_scope(scope)
        if not dataset_id:
            raise RuntimeError(f"该知识范围的 Dataset 未配置: {scope}")
        previous_rag_document_id = (
            latest_succeeded.rag_document_id if latest_succeeded is not None else None
        )
        now = utc_now_naive()

        try:
            uploaded = await self.import_client.upload_file(
                file_name=file_name,
                file_bytes=markdown.encode("utf-8"),
                dataset_id=dataset_id,
                visibility=document_visibility_for_scope(scope),
                service_user=service_user_for_role("admin"),
                content_type="text/markdown",
            )
        except RagError as exc:
            run = await self.runs.create_run(
                knowledge_scope=scope,
                content_hash=content_hash,
                generated_file_name=file_name,
                status=RagSyncStatus.failed.value,
                rag_task_id=None,
                rag_document_id=None,
                previous_rag_document_id=previous_rag_document_id,
                requested_by_user_id=operator_user_id,
                created_at=now,
            )
            await self.runs.mark_failed(
                run,
                error_code=exc.code,
                error_message=safe_error_summary(exc.message),
                finished_at=utc_now_naive(),
            )
            await self._update_scope_status_if_latest(
                run,
                rag_sync_status=RagSyncStatus.failed.value,
                rag_sync_error=safe_error_summary(exc.message),
                updated_at=now,
            )
            return run

        run = await self.runs.create_run(
            knowledge_scope=scope,
            content_hash=content_hash,
            generated_file_name=file_name,
            status=RagSyncStatus.pending.value,
            rag_task_id=uploaded["rag_task_id"],
            rag_document_id=uploaded["rag_document_id"],
            previous_rag_document_id=previous_rag_document_id,
            requested_by_user_id=operator_user_id,
            created_at=now,
        )
        await self._update_scope_status_if_latest(
            run,
            rag_sync_status=RagSyncStatus.syncing.value,
            rag_sync_error=None,
            updated_at=now,
        )
        return run

    # ================= 查询列表（刷新上游） =================

    async def list_sync_runs(
        self,
        *,
        page: int,
        page_size: int,
        knowledge_scope: str | None,
        status: str | None,
        sort_by: str,
        sort_order: str,
        operator_user_id: str,
    ) -> tuple[list[FaqSyncRunView], int]:
        """查询时刷新所有 pending/syncing 记录的上游任务状态，再返回列表。

        operator_user_id 用于 catch-up 同步（旧 run 完成后若内容已变化，自动启动下一轮）。
        """
        await self.refresh_pending_runs(operator_user_id=operator_user_id)
        rows, total = await self.runs.list_page(
            page=page,
            page_size=page_size,
            knowledge_scope=knowledge_scope,
            status=status,
            sort_by=sort_by,
            sort_order=sort_order,
        )
        return [sync_run_view(row) for row in rows], total

    async def refresh_pending_runs(self, *, operator_user_id: str) -> None:
        """刷新全部进行中（pending/syncing）同步记录：上游轮询 + 旧文档清理状态机。

        进行中 run 完成后自动检测是否需要 catch-up（内容已变化则启动下一轮同步）。
        """
        pending = await self.runs.find_runs_needing_refresh()
        for run in pending:
            try:
                await self._refresh_one(run, operator_user_id=operator_user_id)
            except Exception:  # noqa: BLE001 单个 run 失败不影响其余
                logger.exception("FAQ 同步刷新失败 run=%s", run.id)

    async def _refresh_one(self, run: FaqSyncRun, *, operator_user_id: str) -> None:
        if not run.rag_task_id:
            # 无上游任务（理论上创建时必有；防御性处理）：标记失败可重试
            logger.warning("FAQ 同步 run=%s 缺少 rag_task_id，标记 failed", run.id)
            await self._mark_failed_with_scope(
                run,
                error_code="RAG_BAD_RESPONSE",
                error_message="同步记录缺少上游任务，请重试",
            )
            return
        upstream = await self.import_client.get_task_status(
            run.rag_task_id, service_user=service_user_for_role("admin")
        )
        if upstream is None:
            # 上游任务记录不存在（可能已清理）：无法确认成功，保守 failed
            logger.warning(
                "FAQ 同步 run=%s 上游任务 %s 不存在，标记 failed", run.id, run.rag_task_id
            )
            await self._mark_failed_with_scope(
                run,
                error_code="RAG_UNAVAILABLE",
                error_message="上游任务记录不存在，无法确认结果",
            )
            return
        rag_status = str(upstream.get("status") or "")
        if rag_status in _TASK_STATUS_PENDING:
            if run.status != RagSyncStatus.syncing.value:
                run.status = RagSyncStatus.syncing.value
                await self.runs.session.flush()
            return
        if rag_status == "completed":
            # 新 FAQ 文档必须真实存在，不能伪造成功
            doc = await self.document_client.get_document(
                run.rag_document_id or "", service_user=service_user_for_role("admin")
            )
            if doc is None:
                logger.warning(
                    "FAQ 同步 run=%s 任务 completed 但文档 %s 快照不可用",
                    run.id,
                    run.rag_document_id,
                )
                await self._mark_failed_with_scope(
                    run,
                    error_code="RAG_BAD_RESPONSE",
                    error_message="上游任务已完成但 FAQ 文档快照不可用",
                )
                return
            await self._drive_cleanup(run, operator_user_id=operator_user_id)
            return
        if rag_status == "failed":
            await self._mark_failed_with_scope(
                run,
                error_code=(
                    safe_error_summary(str(upstream.get("error_code") or "") or None, limit=100)
                    or "RAG_IMPORT_FAILED"
                ),
                error_message=(
                    safe_error_summary(str(upstream.get("error_message") or "") or None)
                    or "上游导入任务失败"
                ),
            )
            return
        # 未知状态：保守保持 syncing，绝不映射 succeeded
        if run.status != RagSyncStatus.syncing.value:
            run.status = RagSyncStatus.syncing.value
            await self.runs.session.flush()

    # ================= 旧文档清理状态机 =================

    async def _drive_cleanup(self, run: FaqSyncRun, *, operator_user_id: str) -> None:
        """新 FAQ 文档确认成功后清理旧文档（条件锁定，保证只执行一次）。

        删除成功（或上游 404 已不存在）→ succeeded；
        删除失败 → failed，保留新旧文档 ID，可重试。
        成功后若当前 MySQL 内容 hash 已变化，自动启动下一轮 catch-up 同步。
        """
        locked = await self.runs.claim_cleanup(run.id)
        if locked is None:
            return  # 已被其他请求认领（并发防重），不重复删除

        now = utc_now_naive()
        if locked.previous_rag_document_id:
            try:
                await self.document_client.delete(
                    locked.previous_rag_document_id,
                    service_user=service_user_for_role("admin"),
                )
            except RagError as exc:
                # 删除失败：sync failed，保留新旧文档 ID，可重试
                await self.runs.mark_failed(
                    locked,
                    error_code=exc.code,
                    error_message=safe_error_summary(exc.message) or "旧 FAQ 文档清理失败",
                    finished_at=now,
                )
                await self._update_scope_status_if_latest(
                    locked,
                    rag_sync_status=RagSyncStatus.failed.value,
                    rag_sync_error=safe_error_summary(exc.message) or "旧 FAQ 文档清理失败",
                    updated_at=now,
                )
                return
            # 删除成功（或 404 已不存在，delete 返回 None）
            # previous_rag_document_id 保留：作为本次替换的历史记录
            # （下次同步的 previous 取最新 succeeded run 的 rag_document_id，不受影响）

        await self.runs.mark_succeeded(locked, finished_at=now)
        await self._update_scope_status_if_latest(
            locked,
            rag_sync_status=RagSyncStatus.succeeded.value,
            rag_sync_error=None,
            updated_at=now,
        )
        # catch-up：run 成功后若 MySQL 内容 hash 已变化，启动下一轮同步
        await self._maybe_catch_up(locked.knowledge_scope, operator_user_id=operator_user_id)

    # ================= 重试 =================

    async def retry_sync(self, *, run_id: str, operator_user_id: str) -> FaqSyncRun:
        """重试同步（冻结：优先继续旧文档清理，不重复上传已经成功的新文档）。"""
        run = await self.runs.get_by_id(run_id)
        if run is None:
            from app.core.errors import not_found

            raise not_found("同步记录不存在")

        if run.status == RagSyncStatus.succeeded.value:
            return run  # 已成功，幂等返回

        if run.status in (RagSyncStatus.pending.value, RagSyncStatus.syncing.value):
            # 进行中：先刷新一次上游，返回最新状态
            await self._refresh_one(run, operator_user_id=operator_user_id)
            return run

        # failed：若新文档已确认成功且有待清理旧文档 → 只重试旧文档清理
        if run.rag_document_id and run.previous_rag_document_id:
            try:
                doc = await self.document_client.get_document(
                    run.rag_document_id, service_user=service_user_for_role("admin")
                )
            except RagError:
                doc = None
            if doc is not None and str(doc.get("status") or "") == "completed":
                await self.runs.mark_retrying(run)
                await self._drive_cleanup(run, operator_user_id=operator_user_id)
                return run

        # 否则重新生成 + 上传（新 run；同 hash 已 succeeded 时 submit 幂等复用）
        return await self.submit_faq_sync(
            knowledge_scope=run.knowledge_scope,
            operator_user_id=operator_user_id,
        )

    # ================= 内部 =================

    async def _generate_markdown(self, scope: str) -> str:
        """生成该范围 FAQ 汇总 Markdown（只含 published 正式 FAQ，MySQL 为事实源）。"""
        rows, _total = await self.faqs.list_page(
            page=1,
            page_size=10000,
            knowledge_scope=scope,
            status="published",
            sort_by="updated_at",
            sort_order="desc",
        )
        title = _SCOPE_TITLES.get(scope, "FAQ")
        lines = [f"# {title}", "", f"知识范围：{scope}", ""]
        for faq in rows:
            lines.append(f"## {faq.question}")
            lines.append("")
            lines.append(faq.answer)
            lines.append("")
        return "\n".join(lines).strip() + "\n"

    async def _mark_failed_with_scope(
        self, run: FaqSyncRun, *, error_code: str, error_message: str
    ) -> None:
        now = utc_now_naive()
        await self.runs.mark_failed(
            run, error_code=error_code, error_message=error_message, finished_at=now
        )
        # 旧 run 迟到失败不得覆盖更新 run 的 scope 展示状态
        await self._update_scope_status_if_latest(
            run,
            rag_sync_status=RagSyncStatus.failed.value,
            rag_sync_error=error_message,
            updated_at=now,
        )

    async def _maybe_catch_up(self, knowledge_scope: str, *, operator_user_id: str) -> None:
        """进行中 run 完成后：若当前 MySQL 内容 hash 与刚完成 run 不同，启动下一轮同步。

        - previous 指向刚成功的新文档（find_latest_succeeded.rag_document_id）；
        - 内容相同则 submit 幂等复用，不重复上传。
        """
        latest_succeeded = await self.runs.find_latest_succeeded(knowledge_scope)
        if latest_succeeded is None:
            return
        current_hash = await self._current_content_hash(knowledge_scope)
        if latest_succeeded.content_hash != current_hash:
            await self.submit_faq_sync(
                knowledge_scope=knowledge_scope,
                operator_user_id=operator_user_id,
            )

    async def _current_content_hash(self, knowledge_scope: str) -> str:
        """当前 MySQL published FAQ 汇总 Markdown 的 SHA-256（与 submit 计算一致）。"""
        markdown = await self._generate_markdown(knowledge_scope)
        return hashlib.sha256(markdown.encode("utf-8")).hexdigest()

    async def _update_scope_status_if_latest(
        self,
        run: FaqSyncRun,
        *,
        rag_sync_status: str,
        rag_sync_error: str | None,
        updated_at: datetime,
    ) -> None:
        """更新 scope 展示状态，但仅当该 run 仍是 scope 最新记录时。

        旧 run 迟到的 completed/failed 不得覆盖更新 run 的展示状态（首轮复核）。
        """
        latest = await self.runs.find_latest_by_scope(run.knowledge_scope)
        if latest is not None and latest.id != run.id:
            logger.warning(
                "FAQ scope 状态跳过：run=%s latest=%s scope=%s status=%s",
                run.id,
                latest.id,
                run.knowledge_scope,
                rag_sync_status,
            )
            return  # 已有更新的 run，跳过展示状态更新
        await self.faqs.update_scope_sync_status(
            knowledge_scope=run.knowledge_scope,
            rag_sync_status=rag_sync_status,
            rag_sync_error=rag_sync_error,
            updated_at=updated_at,
        )


def sync_run_view(run: FaqSyncRun) -> FaqSyncRunView:
    return FaqSyncRunView(
        id=run.id,
        knowledge_scope=run.knowledge_scope,
        content_hash=run.content_hash,
        generated_file_name=run.generated_file_name,
        status=run.status,
        rag_task_id=run.rag_task_id,
        rag_document_id=run.rag_document_id,
        previous_rag_document_id=run.previous_rag_document_id,
        error_code=run.error_code,
        error_message=run.error_message,
        requested_by_user_id=run.requested_by_user_id,
        created_at=run.created_at,
        finished_at=run.finished_at,
    )


def _validate_scope(scope: str) -> str:
    try:
        return KnowledgeScope(scope).value
    except ValueError:
        from app.core.errors import bad_request

        raise bad_request("非法知识范围") from None
