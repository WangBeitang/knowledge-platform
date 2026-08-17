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


class TestContractTightening:
    """首轮复核决策 4：Adapter 契约校验收紧。"""

    @pytest.mark.asyncio
    async def test_submit_mismatched_session_id_is_bad_response(self, fake):
        fake.submit_session_id_override = "other-session"
        c = _client(fake)
        try:
            with pytest.raises(RagError) as exc_info:
                await c.submit_query(
                    query="q", session_id="request-session", dataset_ids=["d"], service_user="u"
                )
            assert exc_info.value.code == "RAG_BAD_RESPONSE"
        finally:
            await c.aclose()

    @pytest.mark.asyncio
    async def test_status_done_list_not_list_of_str_is_bad_response(self, fake):
        fake.seed_status("s1", {"status": "completed", "done_list": "bad", "running_list": []})
        c = _client(fake)
        try:
            with pytest.raises(RagError) as exc_info:
                await c.get_status("s1", service_user="u")
            assert exc_info.value.code == "RAG_BAD_RESPONSE"
        finally:
            await c.aclose()

    @pytest.mark.asyncio
    async def test_status_running_list_not_list_of_str_is_bad_response(self, fake):
        fake.seed_status("s1", {"status": "processing", "done_list": [], "running_list": [123]})
        c = _client(fake)
        try:
            with pytest.raises(RagError) as exc_info:
                await c.get_status("s1", service_user="u")
            assert exc_info.value.code == "RAG_BAD_RESPONSE"
        finally:
            await c.aclose()

    @pytest.mark.asyncio
    async def test_stream_delta_not_str_is_bad_response(self, fake):
        fake.seed_stream("s1", [("ready", {}), ("delta", {"delta": 123})])
        c = _client(fake)
        try:
            with pytest.raises(RagError) as exc_info:
                events = [item async for item in c.stream_events("s1", service_user="u")]
                _ = events
            assert exc_info.value.code == "RAG_BAD_RESPONSE"
        finally:
            await c.aclose()

    @pytest.mark.asyncio
    async def test_stream_final_missing_trace_id_is_bad_response(self, fake):
        fake.seed_stream(
            "s1",
            [("final", {"answer": "a", "citations": [], "terminal_reason_code": "completed"})],
        )
        c = _client(fake)
        try:
            with pytest.raises(RagError) as exc_info:
                events = [item async for item in c.stream_events("s1", service_user="u")]
                _ = events
            assert exc_info.value.code == "RAG_BAD_RESPONSE"
        finally:
            await c.aclose()

    @pytest.mark.asyncio
    async def test_stream_final_citations_not_list_is_bad_response(self, fake):
        fake.seed_stream(
            "s1",
            [
                (
                    "final",
                    {
                        "answer": "a",
                        "trace_id": "t",
                        "citations": "bad",
                        "terminal_reason_code": "c",
                    },
                )
            ],
        )
        c = _client(fake)
        try:
            with pytest.raises(RagError) as exc_info:
                events = [item async for item in c.stream_events("s1", service_user="u")]
                _ = events
            assert exc_info.value.code == "RAG_BAD_RESPONSE"
        finally:
            await c.aclose()

    @pytest.mark.asyncio
    async def test_stream_final_missing_terminal_reason_is_bad_response(self, fake):
        fake.seed_stream("s1", [("final", {"answer": "a", "trace_id": "t", "citations": []})])
        c = _client(fake)
        try:
            with pytest.raises(RagError) as exc_info:
                events = [item async for item in c.stream_events("s1", service_user="u")]
                _ = events
            assert exc_info.value.code == "RAG_BAD_RESPONSE"
        finally:
            await c.aclose()


class TestStatusStrictTypeValidation:
    """决策六：status Adapter 严格类型校验——禁止 value or [] 掩盖非法 falsey 类型。"""

    @pytest.mark.asyncio
    async def test_done_list_empty_string_is_bad_response(self, fake):
        fake.seed_status("s1", {"status": "completed", "done_list": "", "running_list": []})
        c = _client(fake)
        try:
            with pytest.raises(RagError) as exc_info:
                await c.get_status("s1", service_user="u")
            assert exc_info.value.code == "RAG_BAD_RESPONSE"
        finally:
            await c.aclose()

    @pytest.mark.asyncio
    async def test_running_list_empty_dict_is_bad_response(self, fake):
        fake.seed_status("s1", {"status": "processing", "done_list": [], "running_list": {}})
        c = _client(fake)
        try:
            with pytest.raises(RagError) as exc_info:
                await c.get_status("s1", service_user="u")
            assert exc_info.value.code == "RAG_BAD_RESPONSE"
        finally:
            await c.aclose()

    @pytest.mark.asyncio
    async def test_citations_false_is_bad_response(self, fake):
        fake.seed_status(
            "s1", {"status": "completed", "done_list": [], "running_list": [], "citations": False}
        )
        c = _client(fake)
        try:
            with pytest.raises(RagError) as exc_info:
                await c.get_status("s1", service_user="u")
            assert exc_info.value.code == "RAG_BAD_RESPONSE"
        finally:
            await c.aclose()

    @pytest.mark.asyncio
    async def test_image_urls_string_is_bad_response(self, fake):
        fake.seed_status(
            "s1", {"status": "completed", "done_list": [], "running_list": [], "image_urls": "abc"}
        )
        c = _client(fake)
        try:
            with pytest.raises(RagError) as exc_info:
                await c.get_status("s1", service_user="u")
            assert exc_info.value.code == "RAG_BAD_RESPONSE"
        finally:
            await c.aclose()

    @pytest.mark.asyncio
    async def test_trace_id_int_is_bad_response(self, fake):
        fake.seed_status(
            "s1", {"status": "completed", "done_list": [], "running_list": [], "trace_id": 123}
        )
        c = _client(fake)
        try:
            with pytest.raises(RagError) as exc_info:
                await c.get_status("s1", service_user="u")
            assert exc_info.value.code == "RAG_BAD_RESPONSE"
        finally:
            await c.aclose()

    @pytest.mark.asyncio
    async def test_terminal_reason_code_dict_is_bad_response(self, fake):
        fake.seed_status(
            "s1",
            {
                "status": "completed",
                "done_list": [],
                "running_list": [],
                "terminal_reason_code": {},
            },
        )
        c = _client(fake)
        try:
            with pytest.raises(RagError) as exc_info:
                await c.get_status("s1", service_user="u")
            assert exc_info.value.code == "RAG_BAD_RESPONSE"
        finally:
            await c.aclose()

    @pytest.mark.asyncio
    async def test_answer_int_is_bad_response(self, fake):
        fake.seed_status(
            "s1", {"status": "completed", "done_list": [], "running_list": [], "answer": 1}
        )
        c = _client(fake)
        try:
            with pytest.raises(RagError) as exc_info:
                await c.get_status("s1", service_user="u")
            assert exc_info.value.code == "RAG_BAD_RESPONSE"
        finally:
            await c.aclose()

    @pytest.mark.asyncio
    async def test_normal_empty_lists_and_strings_are_valid(self, fake):
        """正常空 list / 空 str / None 必须仍合法（按上游契约默认值处理）。"""
        fake.seed_status(
            "s1",
            {
                "status": "processing",
                "done_list": [],
                "running_list": [],
                "answer": "",
                "error": "",
                "image_urls": [],
                "trace_id": "",
                "citations": [],
                "terminal_reason_code": None,
            },
        )
        c = _client(fake)
        try:
            status = await c.get_status("s1", service_user="u")
            assert status is not None
            assert status.status == "processing"
            assert status.done_list == []
            assert status.running_list == []
            assert status.answer == ""
            assert status.image_urls == []
            assert status.trace_id == ""
            assert status.citations == []
            assert status.terminal_reason_code is None
        finally:
            await c.aclose()

    @pytest.mark.asyncio
    async def test_missing_optional_fields_default_to_contract_defaults(self, fake):
        """缺字段（非非法类型）→ 按上游 Pydantic 契约默认值，不报错。"""
        fake.seed_status("s1", {"status": "completed", "done_list": [], "running_list": []})
        c = _client(fake)
        try:
            status = await c.get_status("s1", service_user="u")
            assert status is not None
            assert status.answer == ""
            assert status.error == ""
            assert status.image_urls == []
            assert status.trace_id == ""
            assert status.citations == []
            assert status.terminal_reason_code is None
        finally:
            await c.aclose()
