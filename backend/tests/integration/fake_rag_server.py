"""进程内原 RAG Fake（MockTransport）：Stage 3 文档/Chunk 管理契约模拟。

模拟语义与真实原 RAG（import_server.py）一致，绝不比真实接口更宽松：
- 所有请求必须携带 X-User-Id；
- POST /upload → {task_ids:[1], document_ids:[1], dataset_id, owner_user_id, index_version}
- GET /status/{task_id} → TaskStatusSchema（status/done_list/running_list/failed_node）
- GET/POST/DELETE /documents/{id}；rebuild 递增 index_version 并返回新 task_id
- GET /documents/{id}/chunks?enabled=&offset=&limit= 支持 offset/limit 分页
- GET /documents/{id}/chunks/{chunk_id}；PATCH .../enabled 版本不一致 → 409

测试通过 set_task_status / seed_document / seed_chunks 控制上游状态。
"""

import json
import re

import httpx

from app.rag.rag_import_client import DATASET_VISIBILITY_PRIVATE


def _extract_form_field(content: bytes, name: str) -> str:
    """从 multipart body 中提取表单字段（测试用简化解析）。"""
    text = content.decode("utf-8", errors="replace")
    pattern = rf'name="{re.escape(name)}"\r\n\r\n(.*?)\r\n'
    m = re.search(pattern, text, re.DOTALL)
    return m.group(1).strip() if m else ""


def _extract_filename(content: bytes) -> str:
    text = content.decode("utf-8", errors="replace")
    m = re.search(r'filename="([^"]+)"', text)
    return m.group(1) if m else ""


class FakeRag:
    """原 RAG 导入/文档管理服务的最小契约 Fake。"""

    def __init__(self) -> None:
        self.documents: dict[str, dict] = {}
        self.tasks: dict[str, dict] = {}
        self.chunks: dict[tuple[str, int], dict] = {}
        self.upload_calls = 0
        self.delete_calls = 0
        self.rebuild_calls = 0
        self.enabled_calls = 0
        self._seq = 0
        # 测试控制：模拟上游删除失败（409）/ 未知任务状态
        self.fail_delete = False
        self.unknown_task_status: str | None = None

    # ---------- 测试控制 ----------

    def _new_id(self, prefix: str) -> str:
        self._seq += 1
        return f"{prefix}_{self._seq}"

    def seed_document(
        self,
        rag_document_id: str,
        *,
        dataset_id: str = "securities_internal_shared",
        visibility: str = "shared",
        index_version: int = 1,
        chunk_count: int = 0,
        status: str = "completed",
        owner: str = "svc_knowledge_admin",
    ) -> None:
        self.documents[rag_document_id] = {
            "document_id": rag_document_id,
            "dataset_id": dataset_id,
            "owner_user_id": owner,
            "tenant_id": "tenant_default",
            "visibility": visibility,
            "latest_task_id": "",
            "file_name": f"{rag_document_id}.pdf",
            "index_version": index_version,
            "status": status,
            "parse_status": "completed" if status == "completed" else status,
            "index_status": "completed" if status == "completed" else status,
            "chunk_count": chunk_count,
        }

    def seed_chunks(
        self,
        rag_document_id: str,
        count: int,
        *,
        start_index: int = 0,
        index_version: int | None = None,
    ) -> None:
        doc = self.documents.get(rag_document_id)
        assert doc is not None, "请先 seed_document"
        version = index_version if index_version is not None else doc["index_version"]
        for i in range(start_index, start_index + count):
            self.chunks[(rag_document_id, i)] = {
                "chunk_id": f"chunk_{rag_document_id}_{i}",
                "document_id": rag_document_id,
                "dataset_id": doc["dataset_id"],
                "owner_user_id": doc["owner_user_id"],
                "tenant_id": "tenant_default",
                "visibility": doc["visibility"],
                "index_version": version,
                "chunk_index": i,
                "enabled": True,
                "content": f"第 {i} 段正文内容，用于验证 Chunk 分页与详情。",
                "content_preview": f"第 {i} 段正文内容…",
                "title": f"标题 {i}",
                "source_title": f"来源 {i}",
                "effective_enabled": True,
                "manual_status": "none",
                "latest_event": None,
            }

    def set_task_status(
        self,
        rag_task_id: str,
        status: str,
        *,
        done: list | None = None,
        running: list | None = None,
        failed_node: str | None = None,
        error_code: str | None = None,
        error_message: str | None = None,
    ) -> None:
        task = self.tasks.get(rag_task_id)
        assert task is not None, f"task {rag_task_id} 不存在"
        task["status"] = status
        if done is not None:
            task["done_list"] = done
        if running is not None:
            task["running_list"] = running
        elif status in ("completed", "failed", "cancelled"):
            # 真实上游终态 running_list 必为空（任务结束无 running 节点）
            task["running_list"] = []
        if failed_node is not None:
            task["failed_node"] = failed_node
        if error_code is not None:
            task["error_code"] = error_code
        if error_message is not None:
            task["error_message"] = error_message

    def document(self, rag_document_id: str) -> dict | None:
        return self.documents.get(rag_document_id)

    # ---------- HTTP handler ----------

    async def handler(self, request: httpx.Request) -> httpx.Response:
        if not request.headers.get("X-User-Id", "").strip():
            return httpx.Response(400, json={"detail": "缺少 X-User-Id 请求头"})
        path = request.url.path
        method = request.method
        parts = [p for p in path.split("/") if p]

        if method == "POST" and path == "/upload":
            return self._handle_upload(request)
        if method == "GET" and len(parts) == 2 and parts[0] == "status":
            return self._handle_status(parts[1])
        if method == "GET" and len(parts) == 2 and parts[0] == "documents":
            return self._handle_get_document(parts[1])
        if (
            method == "POST"
            and len(parts) == 3
            and parts[0] == "documents"
            and parts[2] == "rebuild"
        ):
            return self._handle_rebuild(parts[1])
        if method == "DELETE" and len(parts) == 2 and parts[0] == "documents":
            return self._handle_delete(parts[1])
        if method == "GET" and len(parts) == 3 and parts[0] == "documents" and parts[2] == "chunks":
            return self._handle_list_chunks(parts[1], request)
        if method == "GET" and len(parts) == 4 and parts[0] == "documents" and parts[2] == "chunks":
            return self._handle_get_chunk(parts[1], parts[3])
        if (
            method == "PATCH"
            and len(parts) == 5
            and parts[0] == "documents"
            and parts[2] == "chunks"
            and parts[4] == "enabled"
        ):
            return self._handle_set_chunk_enabled(parts[1], parts[3], request)
        return httpx.Response(404, json={"detail": "not found"})

    def _handle_upload(self, request: httpx.Request) -> httpx.Response:
        self.upload_calls += 1
        dataset_id = (
            _extract_form_field(request.content, "dataset_id") or "securities_internal_shared"
        )
        visibility = (
            _extract_form_field(request.content, "visibility") or DATASET_VISIBILITY_PRIVATE
        )
        file_name = _extract_filename(request.content) or "upload.pdf"
        owner = request.headers["X-User-Id"]
        task_id = self._new_id("task")
        document_id = self._new_id("doc")
        self.documents[document_id] = {
            "document_id": document_id,
            "dataset_id": dataset_id,
            "owner_user_id": owner,
            "tenant_id": "tenant_default",
            "visibility": visibility,
            "latest_task_id": task_id,
            "file_name": file_name,
            "index_version": 1,
            "status": "processing",
            "parse_status": "pending",
            "index_status": "pending",
            "chunk_count": 0,
        }
        self.tasks[task_id] = {
            "task_id": task_id,
            "task_type": "import",
            "status": "pending",
            "done_list": [],
            "running_list": ["upload_file"],
            "document_id": document_id,
            "dataset_id": dataset_id,
            "owner_user_id": owner,
            "failed_node": "",
            "error_code": "",
            "error_message": "",
        }
        return httpx.Response(
            200,
            json={
                "code": 200,
                "message": "上传成功，正在处理中...",
                "task_ids": [task_id],
                "document_ids": [document_id],
                "dataset_id": dataset_id,
                "owner_user_id": owner,
                "index_version": 1,
            },
        )

    def _handle_status(self, rag_task_id: str) -> httpx.Response:
        task = self.tasks.get(rag_task_id)
        if task is None:
            return httpx.Response(404, json={"detail": f"task_id={rag_task_id} 不存在"})
        if self.unknown_task_status is not None:
            task = {**task, "status": self.unknown_task_status}
        return httpx.Response(200, json={"code": 200, **task})

    def _handle_get_document(self, rag_document_id: str) -> httpx.Response:
        doc = self.documents.get(rag_document_id)
        if doc is None:
            return httpx.Response(404, json={"detail": "文档不存在"})
        return httpx.Response(200, json={"code": 200, **doc})

    def _handle_rebuild(self, rag_document_id: str) -> httpx.Response:
        doc = self.documents.get(rag_document_id)
        if doc is None:
            return httpx.Response(404, json={"detail": "文档不存在"})
        self.rebuild_calls += 1
        task_id = self._new_id("task")
        doc["index_version"] = int(doc.get("index_version", 0)) + 1
        doc["latest_task_id"] = task_id
        doc["status"] = "processing"
        doc["parse_status"] = "pending"
        doc["index_status"] = "pending"
        self.tasks[task_id] = {
            "task_id": task_id,
            "task_type": "rebuild",
            "status": "pending",
            "done_list": [],
            "running_list": ["rebuild"],
            "document_id": rag_document_id,
            "dataset_id": doc["dataset_id"],
            "owner_user_id": doc["owner_user_id"],
            "failed_node": "",
            "error_code": "",
            "error_message": "",
        }
        return httpx.Response(
            200,
            json={
                "message": "重建索引任务已创建",
                "task_id": task_id,
                "document_id": rag_document_id,
                "dataset_id": doc["dataset_id"],
                "index_version": doc["index_version"],
            },
        )

    def _handle_delete(self, rag_document_id: str) -> httpx.Response:
        doc = self.documents.get(rag_document_id)
        if doc is None:
            return httpx.Response(404, json={"detail": "文档不存在"})
        if doc.get("status") in ("deleted", "import_failed"):
            return httpx.Response(409, json={"detail": "文档状态不允许删除"})
        self.delete_calls += 1  # 删除尝试次数（含失败分支，供"只尝试一次"断言）
        if self.fail_delete:
            return httpx.Response(409, json={"detail": "模拟上游删除失败"})
        doc["status"] = "deleted"
        return httpx.Response(
            200,
            json={
                "message": "文档删除成功",
                "document_id": rag_document_id,
                "status": "deleted",
                "deleted_at": "2026-08-16T00:00:00",
            },
        )

    def _handle_list_chunks(self, rag_document_id: str, request: httpx.Request) -> httpx.Response:
        doc = self.documents.get(rag_document_id)
        if doc is None:
            return httpx.Response(404, json={"detail": "文档不存在"})
        offset = int(request.url.params.get("offset", 0))
        limit = int(request.url.params.get("limit", 100))
        version = int(doc.get("index_version", 0))
        items = []
        for (d_id, idx), chunk in sorted(self.chunks.items(), key=lambda kv: (kv[0][0], kv[0][1])):
            if d_id != rag_document_id:
                continue
            if int(chunk.get("index_version")) != version:
                continue
            if not (offset <= idx < offset + limit):
                continue
            items.append(dict(chunk))
        return httpx.Response(200, json={"code": 200, "items": items})

    def _handle_get_chunk(self, rag_document_id: str, chunk_id: str) -> httpx.Response:
        doc = self.documents.get(rag_document_id)
        if doc is None:
            return httpx.Response(404, json={"detail": "文档不存在"})
        version = int(doc.get("index_version", 0))
        for (d_id, _idx), chunk in self.chunks.items():
            if (
                d_id == rag_document_id
                and chunk["chunk_id"] == chunk_id
                and int(chunk["index_version"]) == version
            ):
                return httpx.Response(200, json={"code": 200, **chunk})
        return httpx.Response(404, json={"detail": "chunk 不存在"})

    def _handle_set_chunk_enabled(
        self, rag_document_id: str, chunk_id: str, request: httpx.Request
    ) -> httpx.Response:
        doc = self.documents.get(rag_document_id)
        if doc is None:
            return httpx.Response(404, json={"detail": "文档不存在"})
        body = json.loads(request.content)
        expected = int(body.get("expected_index_version", -1))
        current = int(doc.get("index_version", 0))
        if expected != current:
            return httpx.Response(
                409,
                json={
                    "detail": (
                        f"expected_index_version={expected} 与当前 index_version={current} 不一致"
                    )
                },
            )
        self.enabled_calls += 1
        for (d_id, _idx), chunk in self.chunks.items():
            if d_id == rag_document_id and chunk["chunk_id"] == chunk_id:
                enabled = bool(body.get("enabled", True))
                chunk["effective_enabled"] = enabled
                chunk["manual_status"] = "none" if enabled else "disabled"
                chunk["latest_event"] = {
                    "reason_type": body.get("reason_type"),
                    "reason_detail": body.get("reason_detail"),
                }
                return httpx.Response(
                    200,
                    json={
                        "code": 200,
                        "message": "chunk 已更新",
                        "changed": True,
                        "document_id": rag_document_id,
                        "chunk_id": chunk_id,
                        "index_version": current,
                        "enabled": enabled,
                        "effective_enabled": enabled,
                        "manual_status": chunk["manual_status"],
                        "latest_event": chunk["latest_event"],
                    },
                )
        return httpx.Response(404, json={"detail": "chunk 不存在"})
