"""RagQueryClient 契约测试（MockTransport，不触碰真实网络）。"""

import httpx
import pytest

from app.rag.rag_errors import RagError
from app.rag.rag_query_client import RagQueryClient


@pytest.fixture
def fake():
    from tests.integration.fake_query_rag_server import FakeQueryRag

    return FakeQueryRag()


def _client(fake) -> RagQueryClient:
    return RagQueryClient(base_url="http://rag", transport=httpx.MockTransport(fake.handler))


@pytest.mark.asyncio
async def test_submit_query_sends_correct_body_and_identity(fake):
    c = _client(fake)
    try:
        upstream_session = await c.submit_query(
            query="客户如何办理风险测评？",
            session_id="session-1",
            dataset_ids=["securities_internal_shared", "securities_external_public"],
            service_user="svc_knowledge_employee",
        )
        assert upstream_session == "session-1"
        call = fake.query_calls[-1]
        assert call["query"] == "客户如何办理风险测评？"
        assert call["session_id"] == "session-1"
        assert call["is_stream"] is True
        assert call["dataset_ids"] == ["securities_internal_shared", "securities_external_public"]
        assert call["service_user"] == "svc_knowledge_employee"
        assert fake.query_calls[0] == call  # 只调用一次，不自动重试
    finally:
        await c.aclose()


@pytest.mark.asyncio
async def test_submit_query_unavailable_maps_to_503(fake):
    fake.fail_submit = "unavailable"
    c = _client(fake)
    try:
        with pytest.raises(RagError) as exc_info:
            await c.submit_query(query="q", session_id="s", dataset_ids=["d"], service_user="u")
        assert exc_info.value.code == "RAG_UNAVAILABLE"
        assert exc_info.value.status_code == 503
        assert len(fake.query_calls) == 1  # 网络断开语义：不自动重试
    finally:
        await c.aclose()


@pytest.mark.asyncio
async def test_submit_query_timeout_maps_to_504(fake):
    fake.fail_submit = "timeout"
    c = _client(fake)
    try:
        with pytest.raises(RagError) as exc_info:
            await c.submit_query(query="q", session_id="s", dataset_ids=["d"], service_user="u")
        assert exc_info.value.code == "RAG_TIMEOUT"
    finally:
        await c.aclose()


@pytest.mark.asyncio
async def test_submit_query_bad_json_maps_to_502(fake):
    fake.fail_submit = "bad"
    c = _client(fake)
    try:
        with pytest.raises(RagError) as exc_info:
            await c.submit_query(query="q", session_id="s", dataset_ids=["d"], service_user="u")
        assert exc_info.value.code == "RAG_BAD_RESPONSE"
        assert exc_info.value.status_code == 502
    finally:
        await c.aclose()


@pytest.mark.asyncio
async def test_stream_events_parses_multiple_events(fake):
    fake.seed_stream(
        "s1",
        [
            ("ready", {}),
            ("progress", {"status": "processing"}),
            ("delta", {"delta": "客户"}),
            ("delta", {"delta": "可"}),
            (
                "final",
                {
                    "answer": "客户可",
                    "trace_id": "trace-1",
                    "citations": [],
                    "terminal_reason_code": "completed",
                },
            ),
        ],
    )
    c = _client(fake)
    try:
        events = [item async for item in c.stream_events("s1", service_user="u")]
        names = [name for name, _ in events]
        assert names == ["ready", "progress", "delta", "delta", "final"]
        assert events[2][1]["delta"] == "客户"
        assert events[4][1]["answer"] == "客户可"
    finally:
        await c.aclose()


@pytest.mark.asyncio
async def test_stream_ready_not_duplicated_at_platform_layer(fake):
    """平台层由 ChatService 负责不重复发 ready；client 只忠实解析上游事件。"""
    fake.seed_stream(
        "s2",
        [
            ("ready", {}),
            (
                "final",
                {"answer": "a", "trace_id": "t", "citations": [], "terminal_reason_code": "c"},
            ),
        ],
    )
    c = _client(fake)
    try:
        events = [item async for item in c.stream_events("s2", service_user="u")]
        assert [n for n, _ in events] == ["ready", "final"]
    finally:
        await c.aclose()


@pytest.mark.asyncio
async def test_get_status_completed(fake):
    fake.seed_status(
        "s3",
        {
            "status": "completed",
            "done_list": ["node_answer_output"],
            "running_list": [],
            "answer": "答案",
            "error": "",
            "image_urls": [],
            "trace_id": "trace-3",
            "citations": [],
            "terminal_reason_code": "completed",
        },
    )
    c = _client(fake)
    try:
        status = await c.get_status("s3", service_user="u")
        assert status is not None
        assert status.status == "completed"
        assert status.answer == "答案"
        assert status.trace_id == "trace-3"
        assert status.terminal_reason_code == "completed"
        assert status.is_terminal is True
    finally:
        await c.aclose()


@pytest.mark.asyncio
async def test_get_status_404_returns_none(fake):
    c = _client(fake)
    try:
        assert await c.get_status("not-exist", service_user="u") is None
    finally:
        await c.aclose()


@pytest.mark.asyncio
async def test_get_status_unknown_status_is_bad_response(fake):
    fake.seed_status("s4", {"status": "succeeded", "done_list": [], "running_list": []})
    c = _client(fake)
    try:
        with pytest.raises(RagError) as exc_info:
            await c.get_status("s4", service_user="u")
        assert exc_info.value.code == "RAG_BAD_RESPONSE"
    finally:
        await c.aclose()
