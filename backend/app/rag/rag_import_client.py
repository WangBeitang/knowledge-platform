"""原 RAG 导入/管理客户端：Dataset 与 members 相关接口（阶段 2 最小集）。

契约依据（已核查原 RAG 实际源码，`E:\\develop\\projects\ai_knowledge_base_after_class`）：
- `POST /datasets`：创建 Dataset，body `{dataset_id?, name, description?, visibility?}`，
  dataset_id 是客户端可显式指定的稳定 ID（1~120，仅字母数字_-），非展示名；
- `GET /datasets/{dataset_id}`：详情，不存在/不可见 → 404；
- `GET /datasets/{dataset_id}/members?limit=`：成员列表 `{code, dataset_id, items}`；
- `POST /datasets/{dataset_id}/members`：新增/更新成员（upsert 幂等），body `{user_id, role}`，
  role ∈ viewer|editor|admin；owner 不允许通过 members API 修改；
- 所有请求必须携带 `X-User-Id`（固定服务身份），缺失 → 400。

Base URL 只从 Settings 读取；route/service 禁止拼上游 URL。
"""

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

# Dataset 成员角色（原 RAG 契约）
MEMBER_ROLE_VIEWER = "viewer"


class RagImportClient:
    """原 RAG 导入服务适配客户端（HTTPX，含超时与连接池）。"""

    def __init__(
        self,
        *,
        base_url: str | None = None,
        timeout: float = 10.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.base_url = (base_url or get_settings().rag_import_base_url).rstrip("/")
        self._client = httpx.AsyncClient(
            transport=transport,
            timeout=httpx.Timeout(timeout=timeout, connect=5.0),
            limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
            headers={"Content-Type": "application/json"},
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    # ---------- Dataset ----------

    async def get_dataset(self, dataset_id: str, *, service_user: str) -> dict | None:
        """GET /datasets/{id}：存在返回响应体，不存在（404）返回 None，异常映射。"""
        try:
            resp = await self._client.get(
                f"{self.base_url}/datasets/{quote(dataset_id, safe='')}",
                headers={"X-User-Id": service_user},
            )
        except httpx.HTTPError as exc:
            raise map_http_error(exc) from exc
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
        """POST /datasets：创建 Dataset。重复 dataset_id 由上游抛错，不静默当作成功。"""
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
        try:
            resp = await self._client.get(
                f"{self.base_url}/datasets/{quote(dataset_id, safe='')}/members",
                headers={"X-User-Id": service_user},
            )
        except httpx.HTTPError as exc:
            raise map_http_error(exc) from exc
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
        """POST /datasets/{id}/members：新增/更新成员（上游 upsert，幂等）。"""
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

    # ---------- 通用 ----------

    def _unexpected_status(self, resp: httpx.Response) -> rag_bad_response.__class__:
        """非预期状态码：不静默当作成功，映射 RAG_BAD_RESPONSE 并保留状态细节。"""
        return rag_bad_response(f"上游返回异常状态码 {resp.status_code}")


_shared_client: RagImportClient | None = None


def get_rag_import_client() -> RagImportClient:
    """共享客户端（应用内复用连接池）。"""
    global _shared_client
    if _shared_client is None:
        _shared_client = RagImportClient()
    return _shared_client


async def close_rag_import_client() -> None:
    global _shared_client
    if _shared_client is not None:
        await _shared_client.aclose()
        _shared_client = None
