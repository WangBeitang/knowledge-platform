"""原 RAG 查询客户端（Stage 4）：/query、/status、/stream 统一适配。

契约依据（已独立核实原 RAG `app/api/http/query_server.py`）：
- `POST /query` body {query, session_id, is_stream, dataset_ids}，身份头 `X-User-Id`
  → 返回 {session_id, message}；
- `GET /stream/{session_id}` → SSE（event: ready/progress/delta/final/error，
  data 均为 JSON）；
- `GET /status/{session_id}` → QueryTaskStatusResponse
  {status, done_list, running_list, answer, error, image_urls, trace_id,
  citations, terminal_reason_code}。

超时/重试（冻结 API §15）：
- POST /query：15 秒，确认接收后不重复提交（不自动重试）；
- GET /status：网络错误最多重试 2 次，指数退避；
- GET /stream：不自动重连，异常断开由服务层查 /status 做有限兜底。

未知状态（非 pending/processing/completed/failed）不得静默当成功。
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote

import httpx

from app.core.config import get_settings
from app.rag.rag_errors import (
    map_http_error,
    parse_json_response,
    rag_bad_response,
    rag_timeout,
    rag_unavailable,
)

# 平台 TASK_STATUS_MAP 只认这四种上游状态（"succeeded" 等未知状态视为契约错误）
KNOWN_TASK_STATUSES = {"pending", "processing", "completed", "failed"}


@dataclass
class RagQueryStatus:
    """/status 的严格校验后快照（终态完整才可用于兜底收口）。"""

    status: str
    done_list: list[str] = field(default_factory=list)
    running_list: list[str] = field(default_factory=list)
    answer: str = ""
    error: str = ""
    image_urls: list[str] = field(default_factory=list)
    trace_id: str = ""
    citations: list[dict[str, Any]] = field(default_factory=list)
    terminal_reason_code: str | None = None

    @property
    def is_terminal(self) -> bool:
        return self.status in ("completed", "failed")


class RagQueryClient:
    """原 RAG 查询客户端（Base URL 仅来自 RAG_QUERY_BASE_URL）。"""

    def __init__(
        self,
        *,
        base_url: str | None = None,
        timeout: float = 15.0,
        stream_timeout: float = 180.0,
        transport: httpx.AsyncBaseTransport | None = None,
        retry_attempts: int = 3,
        retry_backoff_base: float = 0.1,
    ) -> None:
        self.base_url = (base_url or get_settings().rag_query_base_url).rstrip("/")
        self.stream_timeout = stream_timeout
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

    # ---------- POST /query（不自动重试）----------

    async def submit_query(
        self,
        *,
        query: str,
        session_id: str,
        dataset_ids: list[str],
        service_user: str,
    ) -> str:
        """提交查询，返回上游确认的 session_id。

        网络断开时无法确定上游是否已接受查询，因此不盲目自动重试，
        避免重复执行（冻结 API §15）。
        """
        body = {
            "query": query,
            "session_id": session_id,
            "is_stream": True,
            "dataset_ids": list(dataset_ids),
        }
        try:
            resp = await self._client.post(
                f"{self.base_url}/query",
                headers={"X-User-Id": service_user},
                json=body,
            )
        except httpx.TimeoutException as exc:
            # 网络层超时：上游是否已接受本请求存在不确定性（可能已接受并开始执行）
            err = map_http_error(exc)
            err.acceptance_ambiguous = True
            raise err from exc
        except httpx.HTTPError as exc:
            # 连接层断开：同上，接受与否不确定
            err = map_http_error(exc)
            err.acceptance_ambiguous = True
            raise err from exc
        # 上游显式服务不可用/网关超时 → 稳定错误码（冻结 API §15）
        if resp.status_code == 503:
            # 上游明确拒绝（本请求未接受）：非歧义
            raise rag_unavailable()
        if resp.status_code == 504:
            # 网关超时：请求可能已到达上游并被接受 → 保守按歧义处理
            err = rag_timeout()
            err.acceptance_ambiguous = True
            raise err
        if resp.status_code != 200:
            raise rag_bad_response(f"上游查询提交异常状态码 {resp.status_code}")
        payload = parse_json_response(resp)
        upstream_session_id = str(payload.get("session_id") or "").strip()
        if not upstream_session_id:
            raise rag_bad_response("上游查询提交响应缺少 session_id")
        if upstream_session_id != session_id:
            raise rag_bad_response("上游查询提交返回的 session_id 与请求不一致")
        return upstream_session_id

    # ---------- GET /status（网络错误重试）----------

    async def _get_with_retry(self, url: str, *, service_user: str) -> httpx.Response:
        headers = {"X-User-Id": service_user}
        last_exc: httpx.HTTPError | None = None
        for attempt in range(self.retry_attempts):
            try:
                return await self._client.get(url, headers=headers)
            except httpx.HTTPError as exc:
                last_exc = exc
                if attempt < self.retry_attempts - 1:
                    await asyncio.sleep(self.retry_backoff_base * (2**attempt))
        assert last_exc is not None
        raise map_http_error(last_exc) from last_exc

    async def get_status(self, session_id: str, *, service_user: str) -> RagQueryStatus | None:
        """GET /status/{session_id}：不存在（404）返回 None，未知状态视为契约错误。"""
        resp = await self._get_with_retry(
            f"{self.base_url}/status/{quote(session_id, safe='')}",
            service_user=service_user,
        )
        if resp.status_code == 404:
            return None
        if resp.status_code != 200:
            raise rag_bad_response(f"上游状态接口异常状态码 {resp.status_code}")
        payload = parse_json_response(resp)
        return self._validate_status_payload(payload)

    @staticmethod
    def _validate_status_payload(payload: dict[str, Any]) -> RagQueryStatus:
        """严格按上游 QueryTaskStatusResponse 契约校验。

        缺字段允许上游 Pydantic 契约的默认值（status/done_list/running_list 必填，
        answer/error/trace_id 默认 ""，image_urls/citations 默认 []，
        terminal_reason_code 默认 None）；但“字段存在且类型错误”必须 RAG_BAD_RESPONSE，
        禁止用 ``value or []`` 掩盖非法 falsey 类型（如 ""、{}、False、"abc"）。
        """
        status = payload.get("status")
        if not isinstance(status, str) or not status:
            raise rag_bad_response("上游状态响应缺少 status")
        if status not in KNOWN_TASK_STATUSES:
            raise rag_bad_response(f"上游状态未知: {status}")

        done_list = payload.get("done_list")
        if not isinstance(done_list, list) or not all(isinstance(v, str) for v in done_list):
            raise rag_bad_response("上游状态 done_list 结构异常")
        running_list = payload.get("running_list")
        if not isinstance(running_list, list) or not all(isinstance(v, str) for v in running_list):
            raise rag_bad_response("上游状态 running_list 结构异常")

        answer = payload.get("answer", "")
        if not isinstance(answer, str):
            raise rag_bad_response("上游状态 answer 类型异常")
        error = payload.get("error", "")
        if not isinstance(error, str):
            raise rag_bad_response("上游状态 error 类型异常")
        image_urls = payload.get("image_urls", [])
        if not isinstance(image_urls, list) or not all(isinstance(v, str) for v in image_urls):
            raise rag_bad_response("上游状态 image_urls 结构异常")
        trace_id = payload.get("trace_id", "")
        if not isinstance(trace_id, str):
            raise rag_bad_response("上游状态 trace_id 类型异常")
        citations = payload.get("citations", [])
        if not isinstance(citations, list) or not all(isinstance(c, dict) for c in citations):
            raise rag_bad_response("上游状态 citations 结构异常")
        terminal_reason = payload.get("terminal_reason_code")
        if terminal_reason is not None and not isinstance(terminal_reason, str):
            raise rag_bad_response("上游状态 terminal_reason_code 类型异常")

        return RagQueryStatus(
            status=status,
            done_list=done_list,
            running_list=running_list,
            answer=answer,
            error=error,
            image_urls=image_urls,
            trace_id=trace_id,
            citations=citations,
            terminal_reason_code=terminal_reason,
        )

    # ---------- GET /stream（SSE，不自动重连）----------

    async def stream_events(
        self,
        session_id: str,
        *,
        service_user: str,
    ) -> AsyncIterator[tuple[str, dict[str, Any]]]:
        """读取上游 SSE 事件流，产出 (event, data) 原始对（未做平台映射）。

        - 一个网络 chunk 可能包含多个事件、一个事件可能跨多个 chunk：逐行按
          空行边界解析，data 多行合并；
        - 网络错误直接抛 RagError，不无限重连；
        - 流提前结束（无 final/error）由服务层查 /status 兜底。
        """
        url = f"{self.base_url}/stream/{quote(session_id, safe='')}"
        headers = {"X-User-Id": service_user}
        try:
            async with self._client.stream(
                "GET",
                url,
                headers=headers,
                timeout=httpx.Timeout(timeout=self.stream_timeout, connect=5.0),
            ) as resp:
                if resp.status_code != 200:
                    raise rag_bad_response(f"上游流式接口异常状态码 {resp.status_code}")
                event: str | None = None
                data_lines: list[str] = []
                async for line in resp.aiter_lines():
                    stripped = line.strip()
                    if not stripped:
                        if event is not None or data_lines:
                            yield self._parse_and_validate_event(event, data_lines)
                            event = None
                            data_lines = []
                        continue
                    if stripped.startswith("event:"):
                        event = stripped[len("event:") :].strip()
                    elif stripped.startswith("data:"):
                        data_lines.append(stripped[len("data:") :].strip())
                # 流结束但事件未闭合：尽力解析，不静默丢弃
                if event is not None or data_lines:
                    yield self._parse_and_validate_event(event, data_lines)
        except httpx.HTTPError as exc:
            raise map_http_error(exc) from exc

    @staticmethod
    def _parse_and_validate_event(
        event: str | None, data_lines: list[str]
    ) -> tuple[str, dict[str, Any]]:
        """解析单个 SSE 事件并做上游契约校验（delta/final 字段）。

        任何契约异常统一抛 RAG_BAD_RESPONSE，由服务层走稳定 error / status 兜底，
        绝不让 AttributeError/TypeError 直接截断下游连接。
        """
        event_name = event or "message"
        raw = "\n".join(data_lines).strip()
        if not raw:
            raise rag_bad_response("上游 SSE 事件缺少 data")
        try:
            data = json.loads(raw)
        except ValueError as exc:
            raise rag_bad_response("上游 SSE data 不是合法 JSON") from exc
        if not isinstance(data, dict):
            raise rag_bad_response("上游 SSE data 不是 JSON 对象")

        if event_name == "delta":
            delta = data.get("delta")
            if not isinstance(delta, str):
                raise rag_bad_response("上游 delta 事件缺少 delta 字符串")
        elif event_name == "final":
            answer = data.get("answer")
            if not isinstance(answer, str):
                raise rag_bad_response("上游 final 缺少 answer")
            trace_id = data.get("trace_id")
            if not isinstance(trace_id, str) or not trace_id.strip():
                raise rag_bad_response("上游 final 缺少 trace_id")
            citations = data.get("citations")
            if not isinstance(citations, list) or not all(isinstance(c, dict) for c in citations):
                raise rag_bad_response("上游 final citations 结构异常")
            if "terminal_reason_code" not in data:
                raise rag_bad_response("上游 final 缺少 terminal_reason_code")
            terminal_reason = data.get("terminal_reason_code")
            if terminal_reason is not None and not isinstance(terminal_reason, str):
                raise rag_bad_response("上游 final terminal_reason_code 类型异常")
        return event_name, data


_shared_query_client: RagQueryClient | None = None


def get_rag_query_client() -> RagQueryClient:
    """共享查询客户端（连接池由 lifespan 统一关闭）。"""
    global _shared_query_client
    if _shared_query_client is None:
        _shared_query_client = RagQueryClient()
    return _shared_query_client


async def close_rag_query_client() -> None:
    global _shared_query_client
    if _shared_query_client is not None:
        await _shared_query_client.aclose()
        _shared_query_client = None
