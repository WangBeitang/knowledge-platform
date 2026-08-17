"""RagTraceClient 契约测试（MockTransport）：Token 可用性解析与安全校验。"""

import httpx
import pytest

from app.rag.rag_trace_client import RagTraceClient


class _FakeTraceServer:
    def __init__(self) -> None:
        self.traces: dict[str, dict] = {}
        self.calls = 0
        self.network_fail = False

    async def handler(self, request: httpx.Request) -> httpx.Response:
        if not request.headers.get("X-User-Id", "").strip():
            return httpx.Response(400, json={"detail": "缺少 X-User-Id"})
        self.calls += 1
        if self.network_fail:
            raise httpx.ConnectError("connection refused", request=request)
        trace_id = request.url.path.rsplit("/", 1)[-1]
        trace = self.traces.get(trace_id)
        if trace is None:
            return httpx.Response(404, json={"detail": "trace 不存在"})
        return httpx.Response(200, json={"code": 200, **trace})


def _client(fake: _FakeTraceServer) -> RagTraceClient:
    return RagTraceClient(
        base_url="http://rag",
        transport=httpx.MockTransport(fake.handler),
        retry_attempts=2,
        retry_backoff_base=0.01,
    )


def _trace(token_usage: dict) -> dict:
    return {
        "trace_id": "trace-1",
        "session_id": "s",
        "owner_user_id": "svc_knowledge_employee",
        "dataset_ids": [],
        "original_query": "q",
        "status": "completed",
        "planner_type": "rule",
        "policy_version": "v1",
        "retrieval_config_version": "v1",
        "planner_steps": [],
        "channel_hits": [],
        "final_citations": [],
        "total_duration_ms": 1,
        "terminal_action": "answer",
        "terminal_reason_code": "completed",
        "execution_source": "chat",
        "token_usage": token_usage,
    }


@pytest.mark.asyncio
async def test_positive_tokens_accepted():
    fake = _FakeTraceServer()
    fake.traces["trace-1"] = _trace(
        {"available": True, "input_tokens": 123, "output_tokens": 45, "total_tokens": 168}
    )
    c = _client(fake)
    try:
        snapshot = await c.get_token_usage("trace-1", service_user="svc_knowledge_employee")
        assert snapshot is not None
        assert snapshot.available is True
        assert snapshot.input_tokens == 123
        assert snapshot.output_tokens == 45
        assert snapshot.total_tokens == 168
    finally:
        await c.aclose()


@pytest.mark.asyncio
async def test_explicit_zero_accepted():
    fake = _FakeTraceServer()
    fake.traces["trace-1"] = _trace(
        {"available": True, "input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
    )
    c = _client(fake)
    try:
        snapshot = await c.get_token_usage("trace-1", service_user="u")
        assert snapshot is not None
        assert snapshot.available is True
        assert snapshot.input_tokens == 0
        assert snapshot.output_tokens == 0
        assert snapshot.total_tokens == 0
    finally:
        await c.aclose()


@pytest.mark.asyncio
async def test_unavailable_returns_nulls():
    fake = _FakeTraceServer()
    fake.traces["trace-1"] = _trace(
        {"available": False, "input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
    )
    c = _client(fake)
    try:
        snapshot = await c.get_token_usage("trace-1", service_user="u")
        assert snapshot is not None
        assert snapshot.available is False
        assert snapshot.input_tokens is None
        assert snapshot.output_tokens is None
        assert snapshot.total_tokens is None
    finally:
        await c.aclose()


@pytest.mark.asyncio
async def test_missing_token_usage_is_compatible():
    """旧 Trace 没有 token_usage 字段：按不可用兼容，绝不把默认 0 当真 0。"""
    fake = _FakeTraceServer()
    trace = _trace({"available": True, "input_tokens": 1, "output_tokens": 1, "total_tokens": 2})
    trace.pop("token_usage")
    fake.traces["trace-1"] = trace
    c = _client(fake)
    try:
        snapshot = await c.get_token_usage("trace-1", service_user="u")
        assert snapshot is not None
        assert snapshot.available is False
        assert snapshot.input_tokens is None
    finally:
        await c.aclose()


@pytest.mark.asyncio
async def test_negative_tokens_treated_as_contract_error():
    fake = _FakeTraceServer()
    fake.traces["trace-1"] = _trace(
        {"available": True, "input_tokens": -1, "output_tokens": 0, "total_tokens": 0}
    )
    c = _client(fake)
    try:
        snapshot = await c.get_token_usage("trace-1", service_user="u")
        assert snapshot is None
    finally:
        await c.aclose()


@pytest.mark.asyncio
async def test_wrong_token_type_treated_as_contract_error():
    fake = _FakeTraceServer()
    fake.traces["trace-1"] = _trace(
        {"available": True, "input_tokens": "abc", "output_tokens": 0, "total_tokens": 0}
    )
    c = _client(fake)
    try:
        snapshot = await c.get_token_usage("trace-1", service_user="u")
        assert snapshot is None
    finally:
        await c.aclose()


@pytest.mark.asyncio
async def test_missing_available_treated_as_contract_error():
    fake = _FakeTraceServer()
    fake.traces["trace-1"] = _trace({"input_tokens": 1, "output_tokens": 1, "total_tokens": 2})
    c = _client(fake)
    try:
        snapshot = await c.get_token_usage("trace-1", service_user="u")
        assert snapshot is None
    finally:
        await c.aclose()


@pytest.mark.asyncio
async def test_trace_404_returns_none():
    fake = _FakeTraceServer()
    c = _client(fake)
    try:
        snapshot = await c.get_token_usage("not-exist", service_user="u")
        assert snapshot is None
    finally:
        await c.aclose()


@pytest.mark.asyncio
async def test_network_failure_returns_none_and_retries():
    """网络错误最多重试 2 次（指数退避），最终返回 None（不影响已成功的问答）。"""
    fake = _FakeTraceServer()
    fake.network_fail = True
    c = _client(fake)
    try:
        snapshot = await c.get_token_usage("trace-1", service_user="u")
        assert snapshot is None
        assert fake.calls == 2  # 1 次 + 1 次重试（retry_attempts=2）
    finally:
        await c.aclose()
