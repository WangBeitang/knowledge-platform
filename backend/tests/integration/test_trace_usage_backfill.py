"""Stage 4 Trace Token 异步回填集成测试。

验证 FastAPI StreamingResponse + BackgroundTasks 的真实执行行为：
- RAG final 先正常发出，Trace 查询不阻塞 final；
- 回填成功：qa_access_logs.token 更新为真实值（包括真实 0）；
- available=false / Trace 404 / 网络失败：Token 保持 null，问答状态不受影响。
"""

import asyncio
import time

from sqlalchemy import select

from app.models.qa_access_log import QaAccessLog
from tests.integration.conftest import _unique, create_user_record
from tests.integration.fake_query_rag_server import FakeQueryRag
from tests.integration.test_chat_rag_stream import _rag_stream_events
from tests.integration.test_chat_session import (
    SSE_STREAM_PATH,
    _create_session,
    _login,
    parse_sse,
)


async def _make_employee(db_session, tracked_users, client) -> tuple[str, str]:
    username = _unique("it_tok_emp")
    password = "TestTok#2026"
    user = await create_user_record(
        db_session,
        username=username,
        display_name="Token 测试员工",
        role="employee",
        password=password,
    )
    user_id = user.id
    tracked_users.append(user_id)
    await db_session.commit()
    token = await _login(client, username, password)
    return user_id, token


async def _ask(client, token, session_id, fake) -> list:
    fake.seed_stream(session_id, _rag_stream_events())
    resp = await client.post(
        SSE_STREAM_PATH.format(sid=session_id),
        headers={"Authorization": f"Bearer {token}"},
        json={"question": "如何办理风险测评？"},
    )
    assert resp.status_code == 200
    events = parse_sse(resp.text)
    assert events[-1][0] == "final"
    return events


async def _fetch_log(session, turn_id: str) -> QaAccessLog | None:
    return await session.scalar(select(QaAccessLog).where(QaAccessLog.turn_id == turn_id))


class TestTraceBackfill:
    async def test_positive_tokens_backfilled(
        self, client, db_session, tracked_users, chat_rag_factory
    ):
        fake = chat_rag_factory(FakeQueryRag(), db_session)
        _, token = await _make_employee(db_session, tracked_users, client)
        session_id = await _create_session(client, token)
        fake.seed_trace(
            "trace-rag-1",
            {"available": True, "input_tokens": 123, "output_tokens": 45, "total_tokens": 168},
        )
        events = await _ask(client, token, session_id, fake)
        final = events[-1][1]
        assert final["answer_source"] == "rag"  # final 先正常交付

        log = await _wait_tokens(db_session, final["turn_id"])
        assert log.input_tokens == 123
        assert log.output_tokens == 45
        assert log.total_tokens == 168

    async def test_explicit_zero_backfilled_as_zero(
        self, client, db_session, tracked_users, chat_rag_factory
    ):
        fake = chat_rag_factory(FakeQueryRag(), db_session)
        _, token = await _make_employee(db_session, tracked_users, client)
        session_id = await _create_session(client, token)
        fake.seed_trace(
            "trace-rag-1",
            {"available": True, "input_tokens": 0, "output_tokens": 0, "total_tokens": 0},
        )
        events = await _ask(client, token, session_id, fake)
        log = await _wait_tokens(db_session, events[-1][1]["turn_id"])
        # 显式真实 0：平台保存 0，不是 null
        assert log.input_tokens == 0
        assert log.output_tokens == 0
        assert log.total_tokens == 0

    async def test_unavailable_keeps_null(
        self, client, db_session, tracked_users, chat_rag_factory
    ):
        fake = chat_rag_factory(FakeQueryRag(), db_session)
        _, token = await _make_employee(db_session, tracked_users, client)
        session_id = await _create_session(client, token)
        fake.seed_trace(
            "trace-rag-1",
            {"available": False, "input_tokens": 0, "output_tokens": 0, "total_tokens": 0},
        )
        events = await _ask(client, token, session_id, fake)
        await asyncio.sleep(0.5)  # 给 background 完成时间
        log = await _fetch_log(db_session, events[-1][1]["turn_id"])
        assert log.input_tokens is None
        assert log.output_tokens is None
        assert log.total_tokens is None
        assert log.status == "succeeded"  # 问答成功不受影响

    async def test_trace_404_does_not_fail_qa(
        self, client, db_session, tracked_users, chat_rag_factory
    ):
        fake = chat_rag_factory(FakeQueryRag(), db_session)
        _, token = await _make_employee(db_session, tracked_users, client)
        session_id = await _create_session(client, token)
        # 不 seed trace → 404
        events = await _ask(client, token, session_id, fake)
        await asyncio.sleep(0.5)
        log = await _fetch_log(db_session, events[-1][1]["turn_id"])
        assert log.input_tokens is None
        assert log.status == "succeeded"

    async def test_trace_contract_error_does_not_fail_qa(
        self, client, db_session, tracked_users, chat_rag_factory
    ):
        """Trace 契约错误（available 类型非法）不影响已成功的问答（Token 保持 null）。"""
        fake = chat_rag_factory(FakeQueryRag(), db_session)
        _, token = await _make_employee(db_session, tracked_users, client)
        session_id = await _create_session(client, token)
        fake.seed_trace(
            "trace-rag-1",
            {"available": "yes", "input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
        )
        events = await _ask(client, token, session_id, fake)
        assert events[-1][1]["answer_source"] == "rag"
        await asyncio.sleep(0.5)
        log = await _fetch_log(db_session, events[-1][1]["turn_id"])
        assert log.input_tokens is None
        assert log.status == "succeeded"


async def _wait_tokens(session, turn_id: str, wait_seconds: float = 5.0) -> QaAccessLog:
    """轮询等待 BackgroundTasks 完成 Token 回填（验证真实执行行为）。"""
    deadline = time.monotonic() + wait_seconds
    while time.monotonic() < deadline:
        log = await _fetch_log(session, turn_id)
        if log is not None and log.input_tokens is not None:
            return log
        await asyncio.sleep(0.1)
    log = await _fetch_log(session, turn_id)
    assert log is not None, "qa_access_log 未写入"
    return log
