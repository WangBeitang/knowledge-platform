"""原 RAG 文档管理客户端（Stage 3）：文档/Chunk 管理接口统一适配。

契约依据（已核查原 RAG `app/api/http/import_server.py`）：
- `GET /documents/{id}` → DocumentStatusSchema（含 file_path/local_dir 等内部路径，
  平台只取安全字段）
- `POST /documents/{id}/rebuild` → {task_id, document_id, dataset_id, index_version}
- `DELETE /documents/{id}` → {message, document_id, status, deleted_at}
  （404=不存在 / 409=状态不允许）
- `GET /documents/{id}/chunks?enabled=&offset=&limit=` → ChunkListSchema
  （items 含 chunk_index/content_preview/effective_enabled/latest_event）
- `GET /documents/{id}/chunks/{chunk_id}` → ChunkDetailSchema（含 content 全文）
- `PATCH /documents/{id}/chunks/{chunk_id}/enabled`
  body {enabled, expected_index_version, reason_type, reason_detail, trace_id}
  409 版本不一致 detail 形如 "expected_index_version=X 与当前 index_version=Y 不一致"

HTTP 状态处理（冻结 API §15）：
- 网络连接 → RAG_UNAVAILABLE；超时 → RAG_TIMEOUT（GET 重试 2 次）
- 上游 404 → 返回 None，服务层转 RESOURCE_NOT_FOUND
- rebuild/delete 409 → RESOURCE_CONFLICT
- chunk PATCH 409 且为 index_version 不一致 → INDEX_VERSION_CONFLICT；否则 RESOURCE_CONFLICT
- 其余 4xx/5xx/非法 JSON → RAG_BAD_RESPONSE
写操作（rebuild/delete/PATCH）不自动业务重发。
"""

from urllib.parse import quote

import httpx

from app.core.config import get_settings
from app.rag.rag_errors import (
    RagError,
    map_http_error,
    parse_json_response,
    rag_bad_response,
)

INDEX_VERSION_CONFLICT_CODE = "INDEX_VERSION_CONFLICT"
RESOURCE_CONFLICT_CODE = "RESOURCE_CONFLICT"


def _conflict(message: str, *, code: str) -> RagError:
    return RagError(code, message, status_code=409, retryable=False)


def _safe_quote(value: str) -> str:
    return quote(value, safe="")


class RagDocumentClient:
    """原 RAG 文档/Chunk 管理客户端（Base URL 仅来自 RAG_IMPORT_BASE_URL）。"""

    def __init__(
        self,
        *,
        base_url: str | None = None,
        timeout: float = 10.0,
        transport: httpx.AsyncBaseTransport | None = None,
        retry_attempts: int = 3,
        retry_backoff_base: float = 0.1,
    ) -> None:
        self.base_url = (base_url or get_settings().rag_import_base_url).rstrip("/")
        self.retry_attempts = retry_attempts
        self.retry_backoff_base = retry_backoff_base
        self._client = httpx.AsyncClient(
            transport=transport,
            timeout=httpx.Timeout(timeout=timeout, connect=5.0),
            limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
            headers={"Content-Type": "application/json"},
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    # ---------- GET（网络错误重试）----------

    async def _get_with_retry(self, url: str, *, service_user: str) -> httpx.Response:
        headers = {"X-User-Id": service_user}
        last_exc: httpx.HTTPError | None = None
        for attempt in range(self.retry_attempts):
            try:
                return await self._client.get(url, headers=headers)
            except httpx.HTTPError as exc:
                last_exc = exc
                if attempt < self.retry_attempts - 1:
                    import asyncio

                    await asyncio.sleep(self.retry_backoff_base * (2**attempt))
        assert last_exc is not None
        raise map_http_error(last_exc) from last_exc

    async def get_document(self, rag_document_id: str, *, service_user: str) -> dict | None:
        """GET /documents/{id}：存在返回响应体，不存在（404）返回 None。"""
        resp = await self._get_with_retry(
            f"{self.base_url}/documents/{_safe_quote(rag_document_id)}",
            service_user=service_user,
        )
        if resp.status_code == 404:
            return None
        if resp.status_code != 200:
            raise self._unexpected_status(resp)
        payload = parse_json_response(resp)
        if not str(payload.get("document_id") or "").strip():
            raise rag_bad_response("上游文档响应缺少 document_id")
        return payload

    async def list_chunks(
        self,
        rag_document_id: str,
        *,
        offset: int,
        limit: int,
        enabled: str = "all",
        service_user: str,
    ) -> dict:
        """GET /documents/{id}/chunks?enabled=&offset=&limit=：当前版本分页列表。"""
        url = (
            f"{self.base_url}/documents/{_safe_quote(rag_document_id)}/chunks"
            f"?enabled={enabled}&offset={int(offset)}&limit={int(limit)}"
        )
        resp = await self._get_with_retry(url, service_user=service_user)
        if resp.status_code == 404:
            raise RagError(
                "RESOURCE_NOT_FOUND",
                "文档不存在",
                status_code=404,
                retryable=False,
            )
        if resp.status_code != 200:
            raise self._unexpected_status(resp)
        payload = parse_json_response(resp)
        if not isinstance(payload.get("items"), list):
            raise rag_bad_response("上游 Chunk 列表响应缺少 items 数组")
        return payload

    async def get_chunk(
        self, rag_document_id: str, chunk_id: str, *, service_user: str
    ) -> dict | None:
        """GET /documents/{id}/chunks/{chunk_id}：详情（含 content 全文）。404 → None。"""
        resp = await self._get_with_retry(
            f"{self.base_url}/documents/{_safe_quote(rag_document_id)}/chunks/{_safe_quote(chunk_id)}",
            service_user=service_user,
        )
        if resp.status_code == 404:
            return None
        if resp.status_code != 200:
            raise self._unexpected_status(resp)
        return parse_json_response(resp)

    # ---------- 写操作（不自动重发）----------

    async def rebuild(self, rag_document_id: str, *, service_user: str) -> dict | None:
        """POST /documents/{id}/rebuild：创建重建索引任务。404 → None；409 → RESOURCE_CONFLICT。"""
        try:
            resp = await self._client.post(
                f"{self.base_url}/documents/{_safe_quote(rag_document_id)}/rebuild",
                headers={"X-User-Id": service_user},
            )
        except httpx.HTTPError as exc:
            raise map_http_error(exc) from exc
        if resp.status_code == 404:
            return None
        if resp.status_code == 409:
            raise _conflict("上游拒绝重建该文档", code=RESOURCE_CONFLICT_CODE)
        if resp.status_code != 200:
            raise self._unexpected_status(resp)
        payload = parse_json_response(resp)
        if not str(payload.get("task_id") or "").strip():
            raise rag_bad_response("上游重建响应缺少 task_id")
        return payload

    async def delete(self, rag_document_id: str, *, service_user: str) -> dict | None:
        """DELETE /documents/{id}：删除文档（上游确认成功才返回）。

        404 → None（已不存在）；409 → RESOURCE_CONFLICT。
        """
        try:
            resp = await self._client.request(
                "DELETE",
                f"{self.base_url}/documents/{_safe_quote(rag_document_id)}",
                headers={"X-User-Id": service_user},
            )
        except httpx.HTTPError as exc:
            raise map_http_error(exc) from exc
        if resp.status_code == 404:
            return None
        if resp.status_code == 409:
            raise _conflict("上游拒绝删除该文档", code=RESOURCE_CONFLICT_CODE)
        if resp.status_code != 200:
            raise self._unexpected_status(resp)
        payload = parse_json_response(resp)
        if not str(payload.get("document_id") or "").strip():
            raise rag_bad_response("上游删除响应缺少 document_id")
        return payload

    async def set_chunk_enabled(
        self,
        rag_document_id: str,
        chunk_id: str,
        *,
        enabled: bool,
        expected_index_version: int,
        reason_type: str,
        reason_detail: str,
        service_user: str,
    ) -> dict:
        """PATCH /documents/{id}/chunks/{chunk_id}/enabled：人工启停（上游覆盖层）。"""
        body = {
            "enabled": enabled,
            "expected_index_version": int(expected_index_version),
            "reason_type": reason_type,
            "reason_detail": reason_detail,
            "trace_id": None,
        }
        try:
            resp = await self._client.patch(
                f"{self.base_url}/documents/{_safe_quote(rag_document_id)}/chunks/"
                f"{_safe_quote(chunk_id)}/enabled",
                headers={"X-User-Id": service_user},
                json=body,
            )
        except httpx.HTTPError as exc:
            raise map_http_error(exc) from exc
        if resp.status_code == 409:
            detail = (
                str(resp.json().get("detail", ""))
                if resp.headers.get("content-type", "").startswith("application/json")
                else ""
            )
            # 上游版本冲突 detail 形如 "expected_index_version=X 与当前 index_version=Y 不一致"
            if "index_version" in detail or "版本" in detail:
                raise _conflict("文档已重新索引，请刷新后再操作", code=INDEX_VERSION_CONFLICT_CODE)
            raise _conflict("Chunk 状态冲突", code=RESOURCE_CONFLICT_CODE)
        if resp.status_code == 404:
            raise RagError(
                "RESOURCE_NOT_FOUND",
                "Chunk 不存在",
                status_code=404,
                retryable=False,
            )
        if resp.status_code != 200:
            raise self._unexpected_status(resp)
        payload = parse_json_response(resp)
        if not str(payload.get("chunk_id") or "").strip():
            raise rag_bad_response("上游 Chunk 启停响应缺少 chunk_id")
        return payload

    # ---------- 通用 ----------

    def _unexpected_status(self, resp: httpx.Response) -> RagError:
        return rag_bad_response(f"上游返回异常状态码 {resp.status_code}")


_shared_document_client: RagDocumentClient | None = None


def get_rag_document_client() -> RagDocumentClient:
    """共享文档管理客户端（连接池由 lifespan 统一关闭）。"""
    global _shared_document_client
    if _shared_document_client is None:
        _shared_document_client = RagDocumentClient()
    return _shared_document_client


async def close_rag_document_client() -> None:
    global _shared_document_client
    if _shared_document_client is not None:
        await _shared_document_client.aclose()
        _shared_document_client = None
