"""原 RAG 导入/管理客户端：Dataset 与 members 相关接口（阶段 2 最小集）。

契约依据（已核查原 RAG 实际源码，`E:\\develop\\projects\ai_knowledge_base_after_class`）：
- `POST /datasets`：创建 Dataset，body `{dataset_id?, name, description?, visibility?}`，
  dataset_id 是客户端可显式指定的稳定 ID（1~120，仅字母数字_-），非展示名；
- `GET /datasets/{dataset_id}`：详情，不存在/不可见 → 404；
- `GET /datasets/{dataset_id}/members?limit=`：成员列表 `{code, dataset_id, items}`；
- `POST /datasets/{dataset_id}/members`：新增/更新成员（upsert 幂等），body `{user_id, role}`，
  role ∈ viewer|editor|admin；owner 不允许通过 members API 修改；
- 所有请求必须携带 `X-User-Id`（固定服务身份），缺失 → 400。

重试策略（SPEC §5.4 冻结）：GET 类读取（列表/详情）网络错误最多重试 2 次（首次 + 2，
总 3 次），指数退避；POST 创建/upsert **不自动重试**——连接断开时无法确定写操作是否
已被上游接收，重试可能造成重复副作用。

Base URL 只从 Settings 读取；route/service 禁止拼上游 URL。
"""

import asyncio
from urllib.parse import quote

import httpx

from app.core.config import get_settings
from app.rag.rag_errors import (
    map_http_error,
    parse_json_response,
    rag_bad_response,
)

# 创建/更新 Dataset 时的可见性：三档券商 Dataset 统一 private，
# 读取权限通过显式成员（固定服务身份）控制，避免 public 扩大暴露面。
DATASET_VISIBILITY_PRIVATE = "private"

# GET 网络类错误重试：总尝试 3 次（首次 + 2 次重试），指数退避基数（秒）
GET_MAX_ATTEMPTS = 3
GET_RETRY_BACKOFF_BASE = 0.1


class RagImportClient:
    """原 RAG 导入服务适配客户端（HTTPX，含超时、连接池与 GET 有限重试）。"""

    def __init__(
        self,
        *,
        base_url: str | None = None,
        timeout: float = 10.0,
        transport: httpx.AsyncBaseTransport | None = None,
        retry_attempts: int = GET_MAX_ATTEMPTS,
        retry_backoff_base: float = GET_RETRY_BACKOFF_BASE,
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

    # ---------- 通用：GET 有限重试 ----------

    async def _get_with_retry(self, url: str, *, service_user: str) -> httpx.Response:
        """GET 请求：网络类错误（连接/读取超时等）指数退避重试，最多 `retry_attempts` 次。

        状态码异常不在此重试（由调用方按业务语义处理 404 / 非预期状态）。
        """
        headers = {"X-User-Id": service_user}
        last_exc: httpx.HTTPError | None = None
        for attempt in range(self.retry_attempts):
            try:
                return await self._client.get(url, headers=headers)
            except httpx.HTTPError as exc:
                last_exc = exc
                if attempt < self.retry_attempts - 1:
                    await asyncio.sleep(self.retry_backoff_base * (2**attempt))
        # 全部重试仍失败：按最终异常类型映射稳定错误码
        assert last_exc is not None
        raise map_http_error(last_exc) from last_exc

    # ---------- Dataset ----------

    async def get_dataset(self, dataset_id: str, *, service_user: str) -> dict | None:
        """GET /datasets/{id}：存在返回响应体，不存在（404）返回 None，异常映射。"""
        resp = await self._get_with_retry(
            f"{self.base_url}/datasets/{quote(dataset_id, safe='')}",
            service_user=service_user,
        )
        if resp.status_code == 404:
            return None
        if resp.status_code != 200:
            raise self._unexpected_status(resp)
        payload = parse_json_response(resp)
        if not str(payload.get("dataset_id", "")).strip():
            raise rag_bad_response("上游 Dataset 响应缺少 dataset_id")
        return payload

    async def create_dataset(
        self,
        *,
        dataset_id: str,
        name: str,
        description: str = "",
        visibility: str = DATASET_VISIBILITY_PRIVATE,
        service_user: str,
    ) -> dict:
        """POST /datasets：创建 Dataset。

        不自动重试：连接断开时无法确定创建是否已被上游接收，重试可能重复创建；
        重复 dataset_id 由上游抛错，不静默当作成功。
        """
        try:
            resp = await self._client.post(
                f"{self.base_url}/datasets",
                headers={"X-User-Id": service_user},
                json={
                    "dataset_id": dataset_id,
                    "name": name,
                    "description": description,
                    "visibility": visibility,
                },
            )
        except httpx.HTTPError as exc:
            raise map_http_error(exc) from exc
        if resp.status_code not in (200, 201):
            raise self._unexpected_status(resp)
        payload = parse_json_response(resp)
        if not str(payload.get("dataset_id", "")).strip():
            raise rag_bad_response("上游创建 Dataset 响应缺少 dataset_id")
        return payload

    # ---------- Dataset members ----------

    async def list_dataset_members(self, dataset_id: str, *, service_user: str) -> list[dict]:
        """GET /datasets/{id}/members：返回成员列表（空列表表示无显式成员）。"""
        resp = await self._get_with_retry(
            f"{self.base_url}/datasets/{quote(dataset_id, safe='')}/members",
            service_user=service_user,
        )
        if resp.status_code != 200:
            raise self._unexpected_status(resp)
        payload = parse_json_response(resp)
        items = payload.get("items")
        if not isinstance(items, list):
            raise rag_bad_response("上游成员列表响应缺少 items 数组")
        return items

    async def upsert_member(
        self,
        *,
        dataset_id: str,
        member_user_id: str,
        role: str,
        operator_service_user: str,
    ) -> dict:
        """POST /datasets/{id}/members：新增/更新成员（上游 upsert，幂等）。

        写操作不自动重试（保守原则）：即使失败也由调用方显式重试/校验。
        """
        try:
            resp = await self._client.post(
                f"{self.base_url}/datasets/{quote(dataset_id, safe='')}/members",
                headers={"X-User-Id": operator_service_user},
                json={"user_id": member_user_id, "role": role},
            )
        except httpx.HTTPError as exc:
            raise map_http_error(exc) from exc
        if resp.status_code not in (200, 201):
            raise self._unexpected_status(resp)
        payload = parse_json_response(resp)
        if not str(payload.get("user_id", "")).strip():
            raise rag_bad_response("上游成员响应缺少 user_id")
        return payload

    # ---------- 上传与任务状态（Stage 3）----------

    async def upload_file(
        self,
        *,
        file_name: str,
        file_bytes: bytes,
        dataset_id: str,
        visibility: str,
        service_user: str,
        content_type: str = "application/pdf",
    ) -> dict:
        """POST /upload：单文件上传，创建原 RAG 导入任务。

        契约校验（§十四）：task_ids/document_ids 必须各恰有一个非空 ID；
        dataset_id 必须与请求一致；index_version 必须为整数。
        不自动重试：断线时无法确定上游是否已接收，重试可能重复创建。
        返回 {rag_task_id, rag_document_id, index_version}。

        原 RAG 只按文件名后缀（.pdf/.md）识别文件类型，不校验 Content-Type；
        FAQ 文档同步复用本接口上传 Markdown（content_type 传 text/markdown）。
        """
        files = {"files": (file_name, file_bytes, content_type)}
        data = {"dataset_id": dataset_id, "visibility": visibility}
        try:
            resp = await self._client.post(
                f"{self.base_url}/upload",
                headers={"X-User-Id": service_user},
                files=files,
                data=data,
            )
        except httpx.HTTPError as exc:
            raise map_http_error(exc) from exc
        if resp.status_code == 404:
            raise rag_bad_response("上游 Dataset 不存在或不可写")
        if resp.status_code != 200:
            raise self._unexpected_status(resp)
        payload = parse_json_response(resp)

        task_ids = payload.get("task_ids")
        document_ids = payload.get("document_ids")
        if not (isinstance(task_ids, list) and len(task_ids) == 1 and str(task_ids[0]).strip()):
            raise rag_bad_response("上游上传响应缺少唯一 task_id")
        if not (
            isinstance(document_ids, list)
            and len(document_ids) == 1
            and str(document_ids[0]).strip()
        ):
            raise rag_bad_response("上游上传响应缺少唯一 document_id")
        if str(payload.get("dataset_id") or "") != dataset_id:
            raise rag_bad_response("上游上传返回的 dataset_id 与请求不一致")
        index_version = payload.get("index_version")
        if not isinstance(index_version, int) or index_version < 0:
            raise rag_bad_response("上游上传响应缺少有效的 index_version")
        return {
            "rag_task_id": str(task_ids[0]),
            "rag_document_id": str(document_ids[0]),
            "index_version": index_version,
        }

    async def get_task_status(self, rag_task_id: str, *, service_user: str) -> dict | None:
        """GET /status/{rag_task_id}：查询导入任务状态（GET 读取规则：网络错误重试）。

        404 表示上游任务不存在（可能已清理）→ 返回 None，由服务层按平台语义处理。
        返回上游原始字段：status/done_list/running_list/failed_node/error_code/error_message。
        """
        resp = await self._get_with_retry(
            f"{self.base_url}/status/{quote(rag_task_id, safe='')}",
            service_user=service_user,
        )
        if resp.status_code == 404:
            return None
        if resp.status_code != 200:
            raise self._unexpected_status(resp)
        payload = parse_json_response(resp)
        if not str(payload.get("task_id") or "").strip():
            raise rag_bad_response("上游任务状态响应缺少 task_id")
        return payload

    # ---------- 通用 ----------

    def _unexpected_status(self, resp: httpx.Response) -> rag_bad_response.__class__:
        """非预期状态码：不静默当作成功，映射 RAG_BAD_RESPONSE 并保留状态细节。"""
        return rag_bad_response(f"上游返回异常状态码 {resp.status_code}")


_shared_client: RagImportClient | None = None


def get_rag_import_client() -> RagImportClient:
    """共享客户端（应用内复用连接池；lifespan shutdown 统一关闭）。"""
    global _shared_client
    if _shared_client is None:
        _shared_client = RagImportClient()
    return _shared_client


async def close_rag_import_client() -> None:
    global _shared_client
    if _shared_client is not None:
        await _shared_client.aclose()
        _shared_client = None
