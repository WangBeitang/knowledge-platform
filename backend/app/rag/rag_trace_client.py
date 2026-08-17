"""原 RAG Trace 客户端（Stage 4）：GET /traces/{trace_id} → 安全 Token 快照。

职责仅限：
- 用与本次 RAG 查询相同的 `X-User-Id`（rag_service_user）读取 Trace（原 RAG 按
  owner_user_id 校验可见性，不能换用当前平台用户身份）；
- 校验安全 `token_usage` 契约 → TokenUsageSnapshot；
- 绝不估算 token。

契约（已核实原 RAG `trace_feedback_service.project_trace_summary`）：
```json
{
  "trace_id": "...",
  "token_usage": {
    "available": true,
    "input_tokens": 123,
    "output_tokens": 45,
    "total_tokens": 168
  }
}
```
- available=true：接受真实非负整数，包括 0；字段缺失/负数/类型错误视为上游契约
  错误，记录安全日志并返回 None（本次问答已成功，不回滚，Token 保持 null）。
- available=false：input/output/total 全部为 null。
- 旧 Trace 没有 availability 字段：按 available=false 兼容，不得把默认 0 当真 0。
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from urllib.parse import quote

import httpx

from app.core.config import get_settings
from app.rag.rag_errors import map_http_error, parse_json_response

logger = logging.getLogger("app.rag.rag_trace_client")


@dataclass
class TokenUsageSnapshot:
    """平台可安全落库的 Token 快照；available=false 时三个字段全为 None。"""

    available: bool
    input_tokens: int | None
    output_tokens: int | None
    total_tokens: int | None


class RagTraceClient:
    """原 RAG Trace 读取客户端（Base URL 仅来自 RAG_QUERY_BASE_URL）。"""

    def __init__(
        self,
        *,
        base_url: str | None = None,
        timeout: float = 5.0,
        transport: httpx.AsyncBaseTransport | None = None,
        retry_attempts: int = 3,
        retry_backoff_base: float = 0.1,
    ) -> None:
        self.base_url = (base_url or get_settings().rag_query_base_url).rstrip("/")
        self.retry_attempts = retry_attempts
        self.retry_backoff_base = retry_backoff_base
        self._client = httpx.AsyncClient(
            transport=transport,
            timeout=httpx.Timeout(timeout=timeout, connect=5.0),
            limits=httpx.Limits(max_connections=10, max_keepalive_connections=5),
            headers={"Content-Type": "application/json"},
        )

    async def aclose(self) -> None:
        await self._client.aclose()

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

    async def get_token_usage(
        self,
        trace_id: str,
        *,
        service_user: str,
    ) -> TokenUsageSnapshot | None:
        """GET /traces/{trace_id}：解析安全 token_usage。

        返回 None 表示无可用 Token 数据（Trace 不存在 / 上游契约错误），
        调用方保持 Token 为 null 并记录 warning，不影响已成功的问答。
        """
        url = f"{self.base_url}/traces/{quote(trace_id, safe='')}"
        try:
            resp = await self._get_with_retry(url, service_user=service_user)
        except httpx.HTTPError as exc:
            logger.warning("Trace 读取网络失败 trace_id=%s err=%s", trace_id, type(exc).__name__)
            return None
        except Exception as exc:  # noqa: BLE001
            logger.warning("Trace 读取失败 trace_id=%s err=%s", trace_id, type(exc).__name__)
            return None

        if resp.status_code == 404:
            logger.warning("Trace 不存在或不可见 trace_id=%s", trace_id)
            return None
        if resp.status_code != 200:
            logger.warning("Trace 接口异常状态码 trace_id=%s status=%s", trace_id, resp.status_code)
            return None
        try:
            payload = parse_json_response(resp)
        except Exception:  # noqa: BLE001 响应契约异常一律视为无可信 Token
            logger.warning("Trace 响应契约异常 trace_id=%s", trace_id)
            return None

        usage = payload.get("token_usage")
        if usage is None:
            # 旧 Trace 没有 token_usage 字段：按不可用兼容
            logger.warning("Trace 无 token_usage 字段 trace_id=%s", trace_id)
            return TokenUsageSnapshot(False, None, None, None)
        if not isinstance(usage, dict):
            logger.warning("Trace token_usage 结构异常 trace_id=%s", trace_id)
            return None

        available = usage.get("available")
        if not isinstance(available, bool):
            logger.warning("Trace token_usage.available 类型异常 trace_id=%s", trace_id)
            return None
        if not available:
            return TokenUsageSnapshot(False, None, None, None)

        parsed = self._parse_non_negative(trace_id, usage)
        if parsed is None:
            return None
        input_tokens, output_tokens, total_tokens = parsed
        return TokenUsageSnapshot(
            available=True,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            total_tokens=total_tokens,
        )

    @staticmethod
    def _parse_non_negative(trace_id: str, usage: dict) -> tuple[int, int, int] | None:
        """available=true 时要求三个字段都是真实非负整数（0 合法）。"""
        values: list[int] = []
        for key in ("input_tokens", "output_tokens", "total_tokens"):
            raw = usage.get(key)
            if isinstance(raw, bool) or not isinstance(raw, int):
                logger.warning("Trace token_usage.%s 类型异常 trace_id=%s", key, trace_id)
                return None
            if raw < 0:
                logger.warning("Trace token_usage.%s 为负数 trace_id=%s", key, trace_id)
                return None
            values.append(raw)
        return values[0], values[1], values[2]


_shared_trace_client: RagTraceClient | None = None


def get_rag_trace_client() -> RagTraceClient:
    global _shared_trace_client
    if _shared_trace_client is None:
        _shared_trace_client = RagTraceClient()
    return _shared_trace_client


async def close_rag_trace_client() -> None:
    global _shared_trace_client
    if _shared_trace_client is not None:
        await _shared_trace_client.aclose()
        _shared_trace_client = None
