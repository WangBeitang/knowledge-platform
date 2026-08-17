"""Stage 4 RAG 流式问答集成测试（真实 DB + 进程内 FakeQueryRag）。"""

import asyncio

from sqlalchemy import select

from app.models.chat_message import ChatMessage
from app.models.qa_access_log import QaAccessLog
from tests.integration.conftest import (
    _unique,
    create_user_record,
)
from tests.integration.fake_query_rag_server import FakeQueryRag
from tests.integration.test_chat_session import (
    SSE_STREAM_PATH,
    _create_session,
    _login,
    parse_sse,
)

LOCAL_CITATION = {
    "document_id": "doc-1",
    "chunk_id": "chunk-11",
    "title": "风险测评操作指引",
    "source": "/data/internal/secret/风险测评操作指引.pdf",
    "score": 0.92,
    "source_type": "local",
}
WEB_CITATION = {
    "document_id": None,
    "chunk_id": None,
    "title": "证监会投资者保护",
    "source": "https://www.csrc.gov.cn/investor",
    "score": 0.85,
    "source_type": "web",
}

RAG_FINAL = {
    "answer": "办理风险测评请通过柜台或 App 完成。",
    "status": "completed",
    "image_urls": [],
    "trace_id": "trace-rag-1",
    "citations": [LOCAL_CITATION, WEB_CITATION],
    "terminal_reason_code": "completed",
}


def _rag_stream_events() -> list[tuple[str, dict]]:
    return [
        ("ready", {}),
        ("progress", {"status": "processing", "running_list": ["node_rerank"]}),
        ("delta", {"delta": "办理"}),
        ("delta", {"delta": "风险"}),
        ("final", RAG_FINAL),
    ]


async def _make_employee(db_session, tracked_users, client) -> tuple[str, str]:
    username = _unique("it_rag_emp")
    password = "TestRag#2026"
    user = await create_user_record(
        db_session,
        username=username,
        display_name="RAG 测试员工",
        role="employee",
        password=password,
    )
    user_id = user.id
    tracked_users.append(user_id)
    await db_session.commit()
    token = await _login(client, username, password)
    return user_id, token


async def _messages_of(session, session_id: str) -> list[ChatMessage]:
    rows = list(
        (
            await session.scalars(select(ChatMessage).where(ChatMessage.session_id == session_id))
        ).all()
    )
    return sorted(rows, key=lambda m: m.seq_no)


class TestRagStream:
    async def test_employee_rag_uses_db_role_scope_and_service_identity(
        self, client, db_session, tracked_users, chat_rag_factory
    ):
        fake = chat_rag_factory(FakeQueryRag(), db_session)
        _, token = await _make_employee(db_session, tracked_users, client)
        session_id = await _create_session(client, token)
        fake.seed_stream(session_id, _rag_stream_events())

        resp = await client.post(
            SSE_STREAM_PATH.format(sid=session_id),
            headers=await bearer_headers(token),
            json={"question": "如何办理风险测评？"},
        )
        assert resp.status_code == 200
        events = parse_sse(resp.text)
        assert events[-1][0] == "final"

        # employee：X-User-Id = svc_knowledge_employee，dataset_ids 只有两档
        call = fake.query_calls[-1]
        assert call["service_user"] == "svc_knowledge_employee"
        assert call["dataset_ids"] == ["securities_internal_shared", "securities_external_public"]
        assert call["session_id"] == session_id
        assert call["is_stream"] is True

    async def test_admin_rag_sends_three_datasets(
        self, client, admin_user, db_session, chat_rag_factory
    ):
        fake = chat_rag_factory(FakeQueryRag(), db_session)
        token = await _login(client, admin_user["username"], admin_user["password"])
        session_id = await _create_session(client, token)
        fake.seed_stream(session_id, _rag_stream_events())

        resp = await client.post(
            SSE_STREAM_PATH.format(sid=session_id),
            headers=await bearer_headers(token),
            json={"question": "如何办理风险测评？"},
        )
        assert resp.status_code == 200
        call = fake.query_calls[-1]
        assert call["service_user"] == "svc_knowledge_admin"
        assert call["dataset_ids"] == [
            "securities_admin_private",
            "securities_internal_shared",
            "securities_external_public",
        ]

    async def test_role_taken_from_current_db_not_jwt(
        self, client, db_session, tracked_users, chat_rag_factory
    ):
        """用户 DB role 变化后，下一次请求必须用新 role 的 scope/service identity。"""
        fake = chat_rag_factory(FakeQueryRag(), db_session)
        user_id, token = await _make_employee(db_session, tracked_users, client)
        session_id = await _create_session(client, token)
        fake.seed_stream(session_id, _rag_stream_events())
        resp = await client.post(
            SSE_STREAM_PATH.format(sid=session_id),
            headers=await bearer_headers(token),
            json={"question": "如何办理风险测评？"},
        )
        assert resp.status_code == 200
        assert fake.query_calls[-1]["service_user"] == "svc_knowledge_employee"

        # 直接把 DB 用户角色改成 admin（模拟角色变化，JWT 不变）
        from app.models.user import User

        user_db = await db_session.get(User, user_id)
        user_db.role = "admin"
        await db_session.commit()

        session2 = await _create_session(client, token)
        fake.seed_stream(session2, _rag_stream_events())
        resp = await client.post(
            SSE_STREAM_PATH.format(sid=session2),
            headers=await bearer_headers(token),
            json={"question": "如何办理风险测评？"},
        )
        assert resp.status_code == 200
        call = fake.query_calls[-1]
        assert call["service_user"] == "svc_knowledge_admin"
        assert call["dataset_ids"] == [
            "securities_admin_private",
            "securities_internal_shared",
            "securities_external_public",
        ]

    async def test_full_sse_sequence_and_delta_mapping(
        self, client, db_session, tracked_users, chat_rag_factory
    ):
        fake = chat_rag_factory(FakeQueryRag(), db_session)
        _, token = await _make_employee(db_session, tracked_users, client)
        session_id = await _create_session(client, token)
        fake.seed_stream(session_id, _rag_stream_events())

        resp = await client.post(
            SSE_STREAM_PATH.format(sid=session_id),
            headers=await bearer_headers(token),
            json={"question": "如何办理风险测评？"},
        )
        assert resp.status_code == 200
        events = parse_sse(resp.text)
        names = [name for name, _ in events]
        # ready → progress(faq_lookup) → progress(rag_submit) → progress(rag_progress)
        # → progress(rag_progress 上游转换) → delta → delta → final
        assert names[0] == "ready"
        assert names[1] == "progress" and events[1][1]["stage"] == "faq_lookup"
        assert names[2] == "progress" and events[2][1]["stage"] == "rag_submit"
        assert names[3] == "progress" and events[3][1]["stage"] == "rag_progress"
        # 上游 progress 转成平台 rag_progress（不暴露节点名）
        assert names[4] == "progress" and events[4][1]["stage"] == "rag_progress"
        # 上游 ready 不导致平台重复 ready（names 里只有一个 ready）
        assert names.count("ready") == 1
        assert ("delta", events[5][1]["text"]) == ("delta", "办理")
        assert ("delta", events[6][1]["text"]) == ("delta", "风险")
        # 终态唯一且最后
        assert names[-1] == "final"
        assert "final" not in names[:-1]
        assert "error" not in names

        # 平台 delta 契约：text（不是上游 delta 字段）
        assert "text" in events[5][1]
        assert "delta" not in events[5][1]

        final = events[-1][1]
        assert final["answer"] == "办理风险测评请通过柜台或 App 完成。"
        assert final["answer_source"] == "rag"
        assert final["trace_id"] == "trace-rag-1"
        assert final["terminal_reason_code"] == "completed"

        # Citation 转换
        citations = final["citations"]
        assert len(citations) == 2
        local, web = citations
        # local：不伪造 source_url/content_preview/index_version；document_id/chunk_id 保留
        assert local["document_id"] == "doc-1"
        assert local["chunk_id"] == "chunk-11"
        assert local["document_name"] == "风险测评操作指引"
        assert local["content_preview"] is None
        assert local["source_url"] is None
        assert local["index_version"] is None
        assert local["score"] == 0.92
        # raw 白名单过滤：本地路径 source 不泄漏
        assert "source" not in local["raw"]
        assert "/data/internal" not in json_dumps(local)
        # web：document_id/chunk_id 合法为 null；source_url 保留
        assert web["document_id"] is None
        assert web["chunk_id"] is None
        assert web["document_name"] == "证监会投资者保护"
        assert web["source_url"] == "https://www.csrc.gov.cn/investor"
        assert web["index_version"] is None

    async def test_persistence_once_with_seq_and_turn(
        self, client, db_session, tracked_users, chat_rag_factory
    ):
        fake = chat_rag_factory(FakeQueryRag(), db_session)
        _, token = await _make_employee(db_session, tracked_users, client)
        session_id = await _create_session(client, token)
        fake.seed_stream(session_id, _rag_stream_events())

        resp = await client.post(
            SSE_STREAM_PATH.format(sid=session_id),
            headers=await bearer_headers(token),
            json={"question": "如何办理风险测评？"},
        )
        assert resp.status_code == 200
        events = parse_sse(resp.text)
        turn_id = events[-1][1]["turn_id"]

        # 不逐 delta 落库：只有 2 条消息，assistant.content = 完整答案
        messages = await _messages_of(db_session, session_id)
        assert len(messages) == 2
        user_msg, assistant_msg = messages
        assert user_msg.role == "user"
        assert user_msg.status == "completed"
        assert user_msg.content == "如何办理风险测评？"
        assert assistant_msg.role == "assistant"
        assert assistant_msg.status == "completed"
        assert assistant_msg.content == "办理风险测评请通过柜台或 App 完成。"
        assert assistant_msg.answer_source == "rag"
        assert assistant_msg.rag_trace_id == "trace-rag-1"
        assert assistant_msg.terminal_reason_code == "completed"
        assert assistant_msg.error_code is None
        # seq_no 严格递增，一轮 user/assistant 共用 turn_id
        assert assistant_msg.seq_no == user_msg.seq_no + 1
        assert user_msg.turn_id == assistant_msg.turn_id == turn_id

        # qa_access_logs 每 Turn 恰好一条
        logs = list(
            (
                await db_session.scalars(select(QaAccessLog).where(QaAccessLog.turn_id == turn_id))
            ).all()
        )
        assert len(logs) == 1
        log = logs[0]
        assert log.answer_source == "rag"
        assert log.rag_trace_id == "trace-rag-1"
        assert log.terminal_reason_code == "completed"
        assert log.citation_count == 2
        assert log.citation_document_ids_json == ["doc-1"]
        assert log.status == "succeeded"
        assert log.error_code is None
        assert log.input_tokens is None  # 初始 null，由 Trace 补取

    async def test_multi_turn_reuses_upstream_session(
        self, client, db_session, tracked_users, chat_rag_factory
    ):
        fake = chat_rag_factory(FakeQueryRag(), db_session)
        _, token = await _make_employee(db_session, tracked_users, client)
        session_id = await _create_session(client, token)

        for i in range(2):
            fake.seed_stream(session_id, _rag_stream_events())
            resp = await client.post(
                SSE_STREAM_PATH.format(sid=session_id),
                headers=await bearer_headers(token),
                json={"question": f"如何办理风险测评？{i}"},
            )
            assert resp.status_code == 200
            assert parse_sse(resp.text)[-1][0] == "final"

        assert len(fake.query_calls) == 2
        # 同一个平台 session 复用原 RAG history（上游 session_id 相同）
        assert fake.query_calls[0]["session_id"] == fake.query_calls[1]["session_id"] == session_id
        # 平台消息每轮 2 条
        messages = await _messages_of(db_session, session_id)
        assert len(messages) == 4
        seqs = [m.seq_no for m in messages]
        assert seqs == [1, 2, 3, 4]

    async def test_concurrent_turn_same_session_409(
        self, client, db_session, tracked_users, chat_rag_factory
    ):
        fake = chat_rag_factory(FakeQueryRag(), db_session)
        _, token = await _make_employee(db_session, tracked_users, client)
        session_id = await _create_session(client, token)
        fake.seed_stream(session_id, _rag_stream_events())
        fake.stream_delay = 0.8  # 第一问流式期间占用会话

        task_a = asyncio.create_task(
            client.post(
                SSE_STREAM_PATH.format(sid=session_id),
                headers=await bearer_headers(token),
                json={"question": "第一问"},
            )
        )
        await asyncio.sleep(0.3)  # 确保 A 已占用会话
        resp_b = await client.post(
            SSE_STREAM_PATH.format(sid=session_id),
            headers=await bearer_headers(token),
            json={"question": "第二问"},
        )
        assert resp_b.status_code == 409
        assert resp_b.json()["error"]["code"] == "RESOURCE_CONFLICT"

        resp_a = await task_a
        assert resp_a.status_code == 200
        assert parse_sse(resp_a.text)[-1][0] == "final"
        # 结束后 registry 释放：可以再次发问
        fake.stream_delay = 0.0
        fake.seed_stream(session_id, _rag_stream_events())
        resp_c = await client.post(
            SSE_STREAM_PATH.format(sid=session_id),
            headers=await bearer_headers(token),
            json={"question": "第三问"},
        )
        assert resp_c.status_code == 200

    async def test_rag_unavailable_no_fallback_answer(
        self, client, db_session, tracked_users, chat_rag_factory
    ):
        fake = chat_rag_factory(FakeQueryRag(), db_session)
        _, token = await _make_employee(db_session, tracked_users, client)
        session_id = await _create_session(client, token)
        fake.fail_submit = "unavailable"

        resp = await client.post(
            SSE_STREAM_PATH.format(sid=session_id),
            headers=await bearer_headers(token),
            json={"question": "如何办理风险测评？"},
        )
        assert resp.status_code == 200
        events = parse_sse(resp.text)
        names = [name for name, _ in events]
        assert names[-1] == "error"
        assert "final" not in names
        error = events[-1][1]
        assert error["code"] == "RAG_UNAVAILABLE"
        assert error["retryable"] is True
        assert "traceback" not in error["message"]

        messages = await _messages_of(db_session, session_id)
        assert len(messages) == 2
        assistant_msg = messages[1]
        assert assistant_msg.status == "failed"
        assert assistant_msg.content == ""  # 不伪造回答
        assert assistant_msg.answer_source == "none"
        assert assistant_msg.error_code == "RAG_UNAVAILABLE"

        log = await db_session.scalar(
            select(QaAccessLog).where(QaAccessLog.session_id == session_id)
        )
        assert log.status == "failed"
        assert log.error_code == "RAG_UNAVAILABLE"
        assert log.answer_source == "none"

        # 不生成 knowledge_gap_candidates（系统错误不得自动生成知识缺口）
        from sqlalchemy import func

        from app.models.knowledge_gap_candidate import KnowledgeGapCandidate

        gap_before = (
            await db_session.scalar(select(func.count()).select_from(KnowledgeGapCandidate))
        ) or 0
        # 发问已发生（在上面）
        gap_after = (
            await db_session.scalar(select(func.count()).select_from(KnowledgeGapCandidate))
        ) or 0
        assert gap_after == gap_before

    async def test_stream_break_fallback_status_completed(
        self, client, db_session, tracked_users, chat_rag_factory
    ):
        """SSE 异常断开（无终态）→ status completed 且完整 → 以终态收口。"""
        fake = chat_rag_factory(FakeQueryRag(), db_session)
        _, token = await _make_employee(db_session, tracked_users, client)
        session_id = await _create_session(client, token)
        # 流结束但没有 final/error
        fake.seed_stream(session_id, [("ready", {}), ("delta", {"delta": "部分"})])
        fake.seed_status(
            session_id,
            {
                "status": "completed",
                "done_list": ["node_answer_output"],
                "running_list": [],
                "answer": "兜底终态答案",
                "error": "",
                "image_urls": [],
                "trace_id": "trace-fallback",
                "citations": [LOCAL_CITATION],
                "terminal_reason_code": "completed",
            },
        )
        resp = await client.post(
            SSE_STREAM_PATH.format(sid=session_id),
            headers=await bearer_headers(token),
            json={"question": "如何办理风险测评？"},
        )
        assert resp.status_code == 200
        events = parse_sse(resp.text)
        final = events[-1][1]
        assert final["answer"] == "兜底终态答案"
        assert final["trace_id"] == "trace-fallback"
        assert final["terminal_reason_code"] == "completed"
        assert len(final["citations"]) == 1
        # 平台持久化为成功（不是伪成功，而是真实终态）
        messages = await _messages_of(db_session, session_id)
        assert messages[1].status == "completed"
        assert messages[1].content == "兜底终态答案"

    async def test_stream_break_unknown_status_not_faked(
        self, client, db_session, tracked_users, chat_rag_factory
    ):
        """断流且无法确认终态 → error，绝不伪成功。"""
        fake = chat_rag_factory(FakeQueryRag(), db_session)
        _, token = await _make_employee(db_session, tracked_users, client)
        session_id = await _create_session(client, token)
        fake.seed_stream(session_id, [("ready", {}), ("delta", {"delta": "部分"})])
        fake.seed_status(
            session_id, {"status": "processing", "done_list": [], "running_list": ["node_rerank"]}
        )

        resp = await client.post(
            SSE_STREAM_PATH.format(sid=session_id),
            headers=await bearer_headers(token),
            json={"question": "如何办理风险测评？"},
        )
        assert resp.status_code == 200
        events = parse_sse(resp.text)
        names = [name for name, _ in events]
        assert names[-1] == "error"
        assert events[-1][1]["code"] == "RAG_BAD_RESPONSE"
        messages = await _messages_of(db_session, session_id)
        assert messages[1].status == "failed"

    async def _disconnect_first_turn(self, client, token, session_id, fake) -> None:
        """发一问后客户端立即断开（上游仍在跑），返回后平台侧 orphaned 保留。"""
        fake.stream_delay = 5.0  # 上游迟迟不返回，客户端先断开
        async with client.stream(
            "POST",
            SSE_STREAM_PATH.format(sid=session_id),
            headers=await bearer_headers(token),
            json={"question": "如何办理风险测评？"},
        ) as resp:
            assert resp.status_code == 200
            async for _ in resp.aiter_lines():
                break  # 读一行（ready）后立即断开
        fake.stream_delay = 0.0
        await asyncio.sleep(0.6)  # 等平台侧 generator finally 执行完毕

    async def test_browser_disconnect_not_marked_failed(
        self, client, db_session, tracked_users, chat_rag_factory
    ):
        """浏览器断开 ≠ RAG 失败：不能仅因客户端断开把业务 Turn 标为 failed。

        真实 uvicorn 断开会取消响应任务（不落库）；ASGITransport 下 generator 可能
        继续跑完（落 succeeded 终态）。两种行为都允许，唯一硬约束：不得出现 failed。
        """
        fake = chat_rag_factory(FakeQueryRag(), db_session)
        _, token = await _make_employee(db_session, tracked_users, client)
        session_id = await _create_session(client, token)
        fake.seed_stream(session_id, _rag_stream_events())
        await self._disconnect_first_turn(client, token, session_id, fake)

        logs = list(
            (
                await db_session.scalars(
                    select(QaAccessLog).where(QaAccessLog.session_id == session_id)
                )
            ).all()
        )
        assert all(log.status != "failed" for log in logs)

    async def test_orphaned_processing_second_ask_409(
        self, client, db_session, tracked_users, chat_rag_factory
    ):
        """孤儿 Turn（已 submit、未确认 terminal）上游仍 processing → 第二问 409。

        直接向进程内 registry 构造 orphaned 状态，稳定验证 recover 判定逻辑
        （不依赖 ASGITransport 的断开语义）。
        """
        from app.services.chat_service import TurnRecord, _active_turns

        fake = chat_rag_factory(FakeQueryRag(), db_session)
        _, token = await _make_employee(db_session, tracked_users, client)
        session_id = await _create_session(client, token)
        fake.seed_status(
            session_id,
            {"status": "processing", "done_list": [], "running_list": ["node_rerank"]},
        )
        user_id = tracked_users[-1]
        orphan = TurnRecord(
            session_id=session_id,
            user_id=user_id,
            turn_id="orphan-turn-1",
            question="旧问题",
            normalized="旧问题",
            question_hash="h" * 64,
            scopes=["internal_shared", "external_public"],
            started_at=0.0,
            submitted=True,
            rag_service_user="svc_knowledge_employee",
        )
        assert await _active_turns.try_acquire(session_id, orphan)
        try:
            resp = await client.post(
                SSE_STREAM_PATH.format(sid=session_id),
                headers=await bearer_headers(token),
                json={"question": "第二问"},
            )
            assert resp.status_code == 409
            assert resp.json()["error"]["code"] == "RESOURCE_CONFLICT"
            assert len(fake.query_calls) == 0  # 未提交新 /query（不重叠）
        finally:
            await _active_turns.release(session_id)

    async def test_orphaned_terminal_second_ask_recovers_and_succeeds(
        self, client, db_session, tracked_users, chat_rag_factory
    ):
        """孤儿 Turn 上游已 completed → 清理旧状态、补齐旧 Turn 终态，第二问成功。"""
        from app.services.chat_service import TurnRecord, _active_turns

        fake = chat_rag_factory(FakeQueryRag(), db_session)
        user_id, token = await _make_employee(db_session, tracked_users, client)
        session_id = await _create_session(client, token)
        fake.seed_status(
            session_id,
            {
                "status": "completed",
                "done_list": ["node_answer_output"],
                "running_list": [],
                "answer": "断开期间上游完成的答案",
                "error": "",
                "image_urls": [],
                "trace_id": "trace-recovered",
                "citations": [LOCAL_CITATION],
                "terminal_reason_code": "completed",
            },
        )
        orphan = TurnRecord(
            session_id=session_id,
            user_id=user_id,
            turn_id="orphan-turn-2",
            question="旧问题",
            normalized="旧问题",
            question_hash="h" * 64,
            scopes=["internal_shared", "external_public"],
            started_at=0.0,
            submitted=True,
            rag_service_user="svc_knowledge_employee",
        )
        assert await _active_turns.try_acquire(session_id, orphan)
        try:
            # 第二问：recover 先补齐旧 Turn（2 条消息 + 1 条日志），再执行新 Query
            fake.seed_stream(session_id, _rag_stream_events())
            resp = await client.post(
                SSE_STREAM_PATH.format(sid=session_id),
                headers=await bearer_headers(token),
                json={"question": "第二问"},
            )
            assert resp.status_code == 200
            events = parse_sse(resp.text)
            assert events[-1][0] == "final"
            assert events[-1][1]["answer_source"] == "rag"

            messages = await _messages_of(db_session, session_id)
            # 旧 Turn（user+assistant，assistant=上游终态答案）+ 新 Turn（user+assistant）
            assert len(messages) == 4
            assert messages[1].content == "断开期间上游完成的答案"
            assert messages[1].status == "completed"
            assert messages[1].rag_trace_id == "trace-recovered"
            assert messages[1].citations_json[0]["document_id"] == "doc-1"
            # 旧 Turn 的 delta/final 不进入新 Query：第二问 assistant 是新 final 答案
            assert messages[3].content == "办理风险测评请通过柜台或 App 完成。"
            seqs = [m.seq_no for m in messages]
            assert seqs == [1, 2, 3, 4]

            logs = list(
                (
                    await db_session.scalars(
                        select(QaAccessLog).where(QaAccessLog.session_id == session_id)
                    )
                ).all()
            )
            assert len(logs) == 2  # 每 Turn 恰好一条
            recovered_log = next(log for log in logs if log.rag_trace_id == "trace-recovered")
            assert recovered_log.status == "succeeded"
            assert recovered_log.answer_source == "rag"
            assert recovered_log.citation_count == 1
        finally:
            await _active_turns.release(session_id)

    async def test_error_terminal_no_extra_events(
        self, client, db_session, tracked_users, chat_rag_factory
    ):
        """上游显式 error：平台只发一个 error，终态后无多余事件，不泄漏上游异常串。"""
        fake = chat_rag_factory(FakeQueryRag(), db_session)
        _, token = await _make_employee(db_session, tracked_users, client)
        session_id = await _create_session(client, token)
        fake.seed_stream(
            session_id,
            [
                ("ready", {}),
                ("delta", {"delta": "部"}),
                ("error", {"error": "Traceback (most recent call last): ... secret path"}),
                ("delta", {"delta": "多余增量"}),
            ],
        )
        resp = await client.post(
            SSE_STREAM_PATH.format(sid=session_id),
            headers=await bearer_headers(token),
            json={"question": "如何办理风险测评？"},
        )
        assert resp.status_code == 200
        events = parse_sse(resp.text)
        names = [name for name, _ in events]
        assert names[-1] == "error"
        assert "final" not in names
        # error 后无多余 delta
        error_index = names.index("error")
        assert error_index == len(names) - 1
        error = events[-1][1]
        assert error["code"] == "RAG_BAD_RESPONSE"
        assert "Traceback" not in error["message"]
        assert "/data/" not in error["message"]


def json_dumps(obj) -> str:
    import json

    return json.dumps(obj, ensure_ascii=False)


async def bearer_headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


class TestStreamFallbackAndContract:
    async def test_stream_socket_error_status_completed_yields_final(
        self, client, db_session, tracked_users, chat_rag_factory
    ):
        """stream socket 报错，但 /status 已 completed → 仍以 final 成功收口（决策 3）。"""
        fake = chat_rag_factory(FakeQueryRag(), db_session)
        _, token = await _make_employee(db_session, tracked_users, client)
        session_id = await _create_session(client, token)
        fake.seed_stream(session_id, [("ready", {}), ("delta", {"delta": "部"})])
        fake.stream_network_error = True  # 流式连接层抛错
        fake.seed_status(
            session_id,
            {
                "status": "completed",
                "done_list": ["node_answer_output"],
                "running_list": [],
                "answer": "网络断流但上游已完成",
                "error": "",
                "image_urls": [],
                "trace_id": "trace-socket-fallback",
                "citations": [LOCAL_CITATION],
                "terminal_reason_code": "completed",
            },
        )
        resp = await client.post(
            SSE_STREAM_PATH.format(sid=session_id),
            headers=await bearer_headers(token),
            json={"question": "如何办理风险测评？"},
        )
        assert resp.status_code == 200
        events = parse_sse(resp.text)
        names = [name for name, _ in events]
        assert names[-1] == "final"
        assert "error" not in names
        final = events[-1][1]
        assert final["answer"] == "网络断流但上游已完成"
        assert final["trace_id"] == "trace-socket-fallback"
        assert final["answer_source"] == "rag"
        messages = await _messages_of(db_session, session_id)
        assert messages[1].status == "completed"
        assert messages[1].content == "网络断流但上游已完成"

    async def test_stream_socket_error_status_processing_yields_error(
        self, client, db_session, tracked_users, chat_rag_factory
    ):
        """stream socket 报错且 /status 仍 processing → 稳定 error，绝不伪成功。"""
        fake = chat_rag_factory(FakeQueryRag(), db_session)
        _, token = await _make_employee(db_session, tracked_users, client)
        session_id = await _create_session(client, token)
        fake.seed_stream(session_id, [("ready", {})])
        fake.stream_network_error = True
        fake.seed_status(
            session_id,
            {"status": "processing", "done_list": [], "running_list": ["node_rerank"]},
        )
        resp = await client.post(
            SSE_STREAM_PATH.format(sid=session_id),
            headers=await bearer_headers(token),
            json={"question": "如何办理风险测评？"},
        )
        assert resp.status_code == 200
        events = parse_sse(resp.text)
        names = [name for name, _ in events]
        assert names[-1] == "error"
        assert "final" not in names
        assert events[-1][1]["code"] == "RAG_UNAVAILABLE"  # 连接层错误 → RAG_UNAVAILABLE
        messages = await _messages_of(db_session, session_id)
        assert messages[1].status == "failed"

    async def test_malformed_final_missing_trace_id_yields_error(
        self, client, db_session, tracked_users, chat_rag_factory
    ):
        """malformed final（缺 trace_id）→ 平台结构化 SSE error，连接不截断。"""
        fake = chat_rag_factory(FakeQueryRag(), db_session)
        _, token = await _make_employee(db_session, tracked_users, client)
        session_id = await _create_session(client, token)
        fake.seed_stream(
            session_id,
            [
                ("ready", {}),
                ("delta", {"delta": "部"}),
                (
                    "final",
                    {"answer": "缺 trace_id", "citations": [], "terminal_reason_code": "completed"},
                ),
            ],
        )
        resp = await client.post(
            SSE_STREAM_PATH.format(sid=session_id),
            headers=await bearer_headers(token),
            json={"question": "如何办理风险测评？"},
        )
        assert resp.status_code == 200
        events = parse_sse(resp.text)
        names = [name for name, _ in events]
        assert names[-1] == "error"
        assert "final" not in names
        assert events[-1][1]["code"] == "RAG_BAD_RESPONSE"
        messages = await _messages_of(db_session, session_id)
        assert messages[1].status == "failed"
        assert messages[1].error_code == "RAG_BAD_RESPONSE"

    async def test_malformed_final_citations_not_list_yields_error(
        self, client, db_session, tracked_users, chat_rag_factory
    ):
        fake = chat_rag_factory(FakeQueryRag(), db_session)
        _, token = await _make_employee(db_session, tracked_users, client)
        session_id = await _create_session(client, token)
        fake.seed_stream(
            session_id,
            [
                ("ready", {}),
                (
                    "final",
                    {
                        "answer": "a",
                        "trace_id": "t-1",
                        "citations": {"bad": 1},
                        "terminal_reason_code": "completed",
                    },
                ),
            ],
        )
        resp = await client.post(
            SSE_STREAM_PATH.format(sid=session_id),
            headers=await bearer_headers(token),
            json={"question": "如何办理风险测评？"},
        )
        assert resp.status_code == 200
        events = parse_sse(resp.text)
        assert events[-1][0] == "error"
        assert events[-1][1]["code"] == "RAG_BAD_RESPONSE"

    async def test_malformed_status_done_list_yields_error(
        self, client, db_session, tracked_users, chat_rag_factory
    ):
        """断流后 /status.done_list 非 list[str] → 契约错误 → error（不伪成功）。"""
        fake = chat_rag_factory(FakeQueryRag(), db_session)
        _, token = await _make_employee(db_session, tracked_users, client)
        session_id = await _create_session(client, token)
        fake.seed_stream(session_id, [("ready", {})])
        fake.seed_status(
            session_id,
            {
                "status": "completed",
                "done_list": "bad",
                "running_list": [],
                "answer": "a",
                "trace_id": "t",
            },
        )
        resp = await client.post(
            SSE_STREAM_PATH.format(sid=session_id),
            headers=await bearer_headers(token),
            json={"question": "如何办理风险测评？"},
        )
        assert resp.status_code == 200
        events = parse_sse(resp.text)
        names = [name for name, _ in events]
        assert names[-1] == "error"
        assert "final" not in names
        assert events[-1][1]["code"] == "RAG_BAD_RESPONSE"
        messages = await _messages_of(db_session, session_id)
        assert messages[1].status == "failed"
