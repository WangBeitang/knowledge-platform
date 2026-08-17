"""faq_sync_runs 数据访问：每个知识范围 FAQ Markdown 文档的同步执行记录。

只负责 SQL。业务状态机（上传/轮询/旧文档清理/幂等）在 FaqSyncService。
"""

from datetime import datetime

from sqlalchemy import func, select, update

from app.models.faq_sync_run import FaqSyncRun
from app.repositories.base import BaseRepository

# 同步记录列表可排序白名单（冻结契约：禁止任意列排序）
FAQ_SYNC_SORT_WHITELIST = {"created_at", "finished_at", "knowledge_scope", "status"}


class FaqSyncRunRepository(BaseRepository[FaqSyncRun]):
    model = FaqSyncRun

    async def get_by_id_for_update(self, run_id: str) -> FaqSyncRun | None:
        """SELECT ... FOR UPDATE：旧文档清理区只允许一个请求进入（行锁防重删）。

        注意：aiomysql 连接下 SELECT 锁可能不跨请求生效，
        实际防重由 `claim_cleanup`（原子条件 UPDATE）保证，本方法仅保留兜底。
        """
        stmt = select(FaqSyncRun).where(FaqSyncRun.id == run_id).with_for_update()
        return await self.session.scalar(stmt)

    async def claim_cleanup(self, run_id: str) -> FaqSyncRun | None:
        """原子认领旧文档清理权（条件锁定，SPEC §2.3）。

        仅 status ∈ (pending, syncing) 时执行 UPDATE 并返回该行；否则返回 None。
        并发请求中只有一个能认领成功（rowcount == 1），
        认领后 status=cleaning，清理结果（succeeded/failed）在同一事务提交；
        请求中断则整事务回滚，cleaning 不落库，下次轮询可重新认领。
        """
        result = await self.session.execute(
            update(FaqSyncRun)
            .where(
                FaqSyncRun.id == run_id,
                FaqSyncRun.status.in_(["pending", "syncing"]),
            )
            .values(status="cleaning", finished_at=None)
        )
        if result.rowcount != 1:
            return None
        stmt = select(FaqSyncRun).where(FaqSyncRun.id == run_id)
        return await self.session.scalar(stmt)

    async def create_run(
        self,
        *,
        knowledge_scope: str,
        content_hash: str,
        generated_file_name: str,
        status: str,
        rag_task_id: str | None,
        rag_document_id: str | None,
        previous_rag_document_id: str | None,
        requested_by_user_id: str,
        created_at: datetime,
    ) -> FaqSyncRun:
        run = FaqSyncRun(
            knowledge_scope=knowledge_scope,
            content_hash=content_hash,
            generated_file_name=generated_file_name,
            status=status,
            rag_task_id=rag_task_id,
            rag_document_id=rag_document_id,
            previous_rag_document_id=previous_rag_document_id,
            error_code=None,
            error_message=None,
            requested_by_user_id=requested_by_user_id,
            created_at=created_at,
            finished_at=None,
        )
        self.session.add(run)
        await self.session.flush()
        return run

    async def list_page(
        self,
        *,
        page: int = 1,
        page_size: int = 20,
        knowledge_scope: str | None = None,
        status: str | None = None,
        sort_by: str = "created_at",
        sort_order: str = "desc",
    ) -> tuple[list[FaqSyncRun], int]:
        if sort_by not in FAQ_SYNC_SORT_WHITELIST:
            sort_by = "created_at"
        column = getattr(self.model, sort_by)
        order = column.desc() if sort_order == "desc" else column.asc()

        conditions = []
        if knowledge_scope:
            conditions.append(FaqSyncRun.knowledge_scope == knowledge_scope)
        if status:
            conditions.append(FaqSyncRun.status == status)

        count_stmt = select(func.count()).select_from(FaqSyncRun).where(*conditions)
        total = await self.session.scalar(count_stmt) or 0
        stmt = (
            select(FaqSyncRun)
            .where(*conditions)
            .order_by(order)
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
        rows = list((await self.session.scalars(stmt)).all())
        return rows, int(total)

    async def find_latest_succeeded(self, knowledge_scope: str) -> FaqSyncRun | None:
        """该范围最近一条 succeeded 的同步记录（幂等复用 + previous 文档来源）。"""
        stmt = (
            select(FaqSyncRun)
            .where(
                FaqSyncRun.knowledge_scope == knowledge_scope,
                FaqSyncRun.status == "succeeded",
            )
            .order_by(FaqSyncRun.created_at.desc())
            .limit(1)
        )
        return await self.session.scalar(stmt)

    async def find_latest_by_scope(self, knowledge_scope: str) -> FaqSyncRun | None:
        """该范围最近一条同步记录（任意状态，用于判断进行中 run）。"""
        stmt = (
            select(FaqSyncRun)
            .where(FaqSyncRun.knowledge_scope == knowledge_scope)
            .order_by(FaqSyncRun.created_at.desc())
            .limit(1)
        )
        return await self.session.scalar(stmt)

    async def find_latest_in_progress(self, knowledge_scope: str) -> FaqSyncRun | None:
        """该范围最新一条进行中（pending/syncing）同步记录（单进行中防分叉）。

        首轮复核：同一 scope 同时最多一个真实 RAG FAQ upload——
        提交前检查本方法，存在进行中 run 时不再启动第二次上传。
        """
        stmt = (
            select(FaqSyncRun)
            .where(
                FaqSyncRun.knowledge_scope == knowledge_scope,
                FaqSyncRun.status.in_(["pending", "syncing"]),
            )
            .order_by(FaqSyncRun.created_at.desc())
            .limit(1)
        )
        return await self.session.scalar(stmt)

    async def find_runs_needing_refresh(self) -> list[FaqSyncRun]:
        """所有 pending/syncing 的同步记录（查询列表时刷新上游任务状态）。"""
        stmt = (
            select(FaqSyncRun)
            .where(FaqSyncRun.status.in_(["pending", "syncing"]))
            .order_by(FaqSyncRun.created_at.asc())
        )
        return list((await self.session.scalars(stmt)).all())

    async def set_uploaded(
        self,
        run: FaqSyncRun,
        *,
        rag_task_id: str,
        rag_document_id: str,
        status: str = "syncing",
    ) -> None:
        """上传成功：记录上游任务与文档 ID，进入 syncing。"""
        run.rag_task_id = rag_task_id
        run.rag_document_id = rag_document_id
        run.status = status
        run.error_code = None
        run.error_message = None
        await self.session.flush()

    async def mark_succeeded(self, run: FaqSyncRun, *, finished_at: datetime) -> None:
        """旧文档清理成功（或无旧文档）：标记 succeeded。"""
        run.status = "succeeded"
        run.error_code = None
        run.error_message = None
        run.finished_at = finished_at
        await self.session.flush()

    async def mark_failed(
        self,
        run: FaqSyncRun,
        *,
        error_code: str,
        error_message: str,
        finished_at: datetime,
    ) -> None:
        """同步失败：保留新旧文档 ID（previous_rag_document_id 不清除），可重试。"""
        run.status = "failed"
        run.error_code = error_code[:100]
        run.error_message = (error_message or "")[:1000]
        run.finished_at = finished_at
        await self.session.flush()

    async def mark_retrying(self, run: FaqSyncRun) -> None:
        """重试开始：回到进行中状态（等待下次查询刷新上游）。"""
        run.status = "syncing"
        run.error_code = None
        run.error_message = None
        run.finished_at = None
        await self.session.flush()
