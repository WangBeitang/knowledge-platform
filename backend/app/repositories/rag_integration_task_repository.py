"""rag_integration_tasks 数据访问（只负责 SQL，禁止调用 RAG）。"""

from datetime import datetime

from sqlalchemy import select

from app.models.rag_integration_task import RagIntegrationTask
from app.repositories.base import BaseRepository


class RagIntegrationTaskRepository(BaseRepository[RagIntegrationTask]):
    model = RagIntegrationTask

    async def get_by_rag_task_id(self, rag_task_id: str) -> RagIntegrationTask | None:
        stmt = select(RagIntegrationTask).where(RagIntegrationTask.rag_task_id == rag_task_id)
        return await self.session.scalar(stmt)

    async def update_from_upstream(
        self,
        task: RagIntegrationTask,
        *,
        rag_status: str | None,
        done_nodes: list,
        running_nodes: list,
        failed_node: str | None,
        status: str | None = None,
        error_code: str | None = None,
        error_message: str | None = None,
        finished_at: datetime | None = None,
    ) -> None:
        """用上游轮询结果刷新任务（节点/状态/错误摘要）。"""
        task.rag_status = rag_status
        task.done_nodes_json = done_nodes
        task.running_nodes_json = running_nodes
        task.failed_node = failed_node
        if status is not None:
            task.status = status
        if error_code is not None:
            task.error_code = error_code
        if error_message is not None:
            task.error_message = error_message
        if finished_at is not None:
            task.finished_at = finished_at
        await self.session.flush()
