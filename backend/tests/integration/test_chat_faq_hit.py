"""Stage 4 FAQ 精确短路集成测试（真实 DB + Redis + 进程内 FakeQueryRag）。"""

import json

import pytest
from sqlalchemy import select

from app.core.normalizer import normalize_question, question_hash
from app.models.chat_message import ChatMessage
from app.models.chat_session import ChatSession
from app.models.faq import Faq
from app.models.qa_access_log import QaAccessLog
from app.repositories.faq_repository import FaqRepository
from app.services.faq_service import faq_cache_key
from tests.integration.conftest import (
    _unique,
    bearer_headers,
    cleanup_faqs,
    create_faq_record,
    create_user_record,
)
from tests.integration.fake_query_rag_server import FakeQueryRag
from tests.integration.test_chat_session import (
    SSE_STREAM_PATH,
    _create_session,
    _login,
    parse_sse,
)

STD_QUESTION = "客户如何办理风险测评"
# 归一化后与 STD_QUESTION 相同的各种写法
STD_QUESTION_FULL = "客户如何办理风险测评？"
STD_QUESTION_EXTRA_SPACE = "  客户如何办理风险测评  "
STD_QUESTION_PERIOD = "客户如何办理风险测评。"


async def _make_user(db_session, tracked_users, client, *, role: str) -> tuple[str, str]:
    username = _unique(f"it_{role}")
    password = "TestPwd#2026"
    user = await create_user_record(
        db_session,
        username=username,
        display_name=f"测试{role}",
        role=role,
        password=password,
    )
    user_id = user.id
    tracked_users.append(user_id)
    await db_session.commit()
    token = await _login(client, username, password)
    return user_id, token


async def _redis_delete(scope: str, normalized: str) -> None:
    from app.core.redis import get_redis

    redis = await get_redis()
    if redis is not None:
        await redis.delete(faq_cache_key(scope, question_hash(normalize_question(normalized))))


class TestFaqHit:
    @pytest.fixture(autouse=True)
    async def _cleanup_redis(self):
        yield
        # 清理三个 scope 的缓存（避免过期 key 干扰其他测试 / 复用 hash）
        for scope in ("admin_private", "internal_shared", "external_public"):
            await _redis_delete(scope, STD_QUESTION_FULL)

    async def test_faq_exact_hit_full_chain(
        self, client, admin_user, db_session, tracked_users, chat_rag_factory
    ):
        fake = chat_rag_factory(FakeQueryRag(), db_session)
        normalized = normalize_question(STD_QUESTION_FULL)
        digest = question_hash(normalized)
        faq = await create_faq_record(
            db_session,
            knowledge_scope="internal_shared",
            question=STD_QUESTION,
            normalized_question=normalized,
            normalized_question_hash=digest,
            answer="请通过柜台或 App 完成风险测评。",
            created_by_user_id=admin_user["user_id"],
        )
        faq_id = faq.id
        await db_session.commit()
        try:
            _, token = await _make_user(db_session, tracked_users, client, role="employee")
            session_id = await _create_session(client, token)

            resp = await client.post(
                SSE_STREAM_PATH.format(sid=session_id),
                headers=await bearer_headers(token),
                json={"question": STD_QUESTION_FULL},
            )
            assert resp.status_code == 200
            events = parse_sse(resp.text)
            # ready → progress(faq_lookup) → final（无 delta/error）
            names = [name for name, _ in events]
            assert names == ["ready", "progress", "final"]
            assert events[1][1]["stage"] == "faq_lookup"

            final = events[-1][1]
            assert final["answer"] == "请通过柜台或 App 完成风险测评。"
            assert final["answer_source"] == "faq_cache"
            assert final["trace_id"] is None
            assert final["citations"] == []
            assert final["terminal_reason_code"] is None
            assert final["turn_id"] == events[0][1]["turn_id"]
            assert final["request_id"] == events[0][1]["request_id"]
            assert events[0][1]["session_id"] == session_id

            # FAQ 命中：RAG 一次都没被调用
            assert fake.query_calls == []

            # 持久化：2 条消息 + 1 条日志
            rows = list(
                (
                    await db_session.scalars(
                        select(ChatMessage).where(ChatMessage.session_id == session_id)
                    )
                ).all()
            )
            assert len(rows) == 2
            user_msg = next(m for m in rows if m.role == "user")
            assistant_msg = next(m for m in rows if m.role == "assistant")
            assert user_msg.content == STD_QUESTION_FULL
            assert user_msg.status == "completed"
            assert assistant_msg.content == "请通过柜台或 App 完成风险测评。"
            assert assistant_msg.status == "completed"
            assert assistant_msg.answer_source == "faq_cache"
            assert assistant_msg.rag_trace_id is None
            assert assistant_msg.citations_json == []
            assert assistant_msg.error_code is None
            assert user_msg.turn_id == assistant_msg.turn_id
            assert assistant_msg.seq_no == user_msg.seq_no + 1

            log = await db_session.scalar(
                select(QaAccessLog).where(QaAccessLog.turn_id == user_msg.turn_id)
            )
            assert log is not None
            assert log.channel == "internal_web"
            assert log.answer_source == "faq_cache"
            assert log.faq_id == faq_id
            assert log.rag_trace_id is None
            assert log.terminal_reason_code is None
            assert log.citation_count == 0
            assert log.citation_document_ids_json == []
            assert log.input_tokens is None
            assert log.output_tokens is None
            assert log.total_tokens is None
            assert log.status == "succeeded"
            assert log.error_code is None
            assert log.normalized_question_hash == digest

            # hit_count 原子自增
            faq_db = await db_session.get(Faq, faq_id)
            assert faq_db.hit_count == 1

            # 标题自动生成（默认标题 → 归一化问题截断）
            chat_session = await db_session.get(ChatSession, session_id)
            assert chat_session.title == STD_QUESTION
        finally:
            await cleanup_faqs(db_session, [faq_id])

    async def test_normalized_variants_hit_same_faq(
        self, client, admin_user, db_session, tracked_users, chat_rag_factory
    ):
        fake = chat_rag_factory(FakeQueryRag(), db_session)
        normalized = normalize_question(STD_QUESTION_FULL)
        digest = question_hash(normalized)
        faq = await create_faq_record(
            db_session,
            knowledge_scope="internal_shared",
            question=STD_QUESTION,
            normalized_question=normalized,
            normalized_question_hash=digest,
            answer="标准答案内容",
            created_by_user_id=admin_user["user_id"],
        )
        faq_id = faq.id
        await db_session.commit()
        try:
            _, token = await _make_user(db_session, tracked_users, client, role="employee")
            session_id = await _create_session(client, token)

            for variant in (STD_QUESTION_EXTRA_SPACE, STD_QUESTION_PERIOD):
                resp = await client.post(
                    SSE_STREAM_PATH.format(sid=session_id),
                    headers=await bearer_headers(token),
                    json={"question": variant},
                )
                assert resp.status_code == 200, variant
                final = parse_sse(resp.text)[-1][1]
                assert final["answer_source"] == "faq_cache", variant
                assert final["answer"] == "标准答案内容", variant
            # 归一化等价问题全部命中 FAQ，RAG 未被调用
            assert fake.query_calls == []
        finally:
            await cleanup_faqs(db_session, [faq_id])

    async def test_redis_miss_mysql_hit_and_backfill(
        self, client, admin_user, db_session, tracked_users, chat_rag_factory, monkeypatch
    ):
        normalized = normalize_question(STD_QUESTION_FULL)
        digest = question_hash(normalized)
        faq = await create_faq_record(
            db_session,
            knowledge_scope="internal_shared",
            question=STD_QUESTION,
            normalized_question=normalized,
            normalized_question_hash=digest,
            answer="缓存回填测试答案",
            created_by_user_id=admin_user["user_id"],
        )
        faq_id = faq.id
        await db_session.commit()

        calls = {"mysql": 0}
        original = FaqRepository.find_published_by_scope_hash

        async def counting(*args, **kwargs):
            calls["mysql"] += 1
            return await original(*args, **kwargs)

        monkeypatch.setattr(FaqRepository, "find_published_by_scope_hash", counting)
        try:
            _, token = await _make_user(db_session, tracked_users, client, role="employee")
            session_id = await _create_session(client, token)

            # 第一次：Redis miss → MySQL hit → 回填 Redis
            resp = await client.post(
                SSE_STREAM_PATH.format(sid=session_id),
                headers=await bearer_headers(token),
                json={"question": STD_QUESTION_FULL},
            )
            assert resp.status_code == 200
            assert parse_sse(resp.text)[-1][1]["answer_source"] == "faq_cache"
            assert calls["mysql"] == 1

            # 第二次：应命中 Redis 缓存（MySQL 不再被查询）
            resp = await client.post(
                SSE_STREAM_PATH.format(sid=session_id),
                headers=await bearer_headers(token),
                json={"question": STD_QUESTION_FULL},
            )
            assert resp.status_code == 200
            assert parse_sse(resp.text)[-1][1]["answer"] == "缓存回填测试答案"
            assert calls["mysql"] == 1  # 没有新增 MySQL 查询 → 走了 Redis

            # Redis 中确实有回填值
            from app.core.redis import get_redis

            redis = await get_redis()
            if redis is not None:
                raw = await redis.get(faq_cache_key("internal_shared", digest))
                assert raw is not None
                payload = json.loads(raw)
                assert payload["faq_id"] == faq_id
                assert payload["answer"] == "缓存回填测试答案"
        finally:
            await cleanup_faqs(db_session, [faq_id])

    async def test_redis_unavailable_falls_back_to_mysql(
        self, client, admin_user, db_session, tracked_users, chat_rag_factory, monkeypatch
    ):
        normalized = normalize_question(STD_QUESTION_FULL)
        digest = question_hash(normalized)
        faq = await create_faq_record(
            db_session,
            knowledge_scope="internal_shared",
            question=STD_QUESTION,
            normalized_question=normalized,
            normalized_question_hash=digest,
            answer="Redis 降级答案",
            created_by_user_id=admin_user["user_id"],
        )
        faq_id = faq.id
        await db_session.commit()

        fake = chat_rag_factory(FakeQueryRag(), db_session)

        async def redis_unavailable():
            return None

        monkeypatch.setattr("app.services.faq_service.get_redis", redis_unavailable)
        try:
            _, token = await _make_user(db_session, tracked_users, client, role="employee")
            session_id = await _create_session(client, token)
            resp = await client.post(
                SSE_STREAM_PATH.format(sid=session_id),
                headers=await bearer_headers(token),
                json={"question": STD_QUESTION_FULL},
            )
            assert resp.status_code == 200
            final = parse_sse(resp.text)[-1][1]
            assert final["answer_source"] == "faq_cache"
            assert final["answer"] == "Redis 降级答案"
            assert fake.query_calls == []
        finally:
            await cleanup_faqs(db_session, [faq_id])

    async def test_employee_scope_priority_internal_over_external(
        self, client, admin_user, db_session, tracked_users, chat_rag_factory
    ):
        normalized = normalize_question(STD_QUESTION_FULL)
        digest = question_hash(normalized)
        faq_internal = await create_faq_record(
            db_session,
            knowledge_scope="internal_shared",
            question=STD_QUESTION,
            normalized_question=normalized,
            normalized_question_hash=digest,
            answer="内部共享答案",
            created_by_user_id=admin_user["user_id"],
        )
        faq_external = await create_faq_record(
            db_session,
            knowledge_scope="external_public",
            question=STD_QUESTION,
            normalized_question=normalized,
            normalized_question_hash=digest,
            answer="外部公开答案",
            created_by_user_id=admin_user["user_id"],
        )
        await db_session.commit()
        try:
            _, token = await _make_user(db_session, tracked_users, client, role="employee")
            session_id = await _create_session(client, token)
            resp = await client.post(
                SSE_STREAM_PATH.format(sid=session_id),
                headers=await bearer_headers(token),
                json={"question": STD_QUESTION_FULL},
            )
            assert resp.status_code == 200
            final = parse_sse(resp.text)[-1][1]
            assert final["answer"] == "内部共享答案"
            log = await db_session.scalar(
                select(QaAccessLog).where(QaAccessLog.session_id == session_id)
            )
            assert log.faq_id == faq_internal.id
            assert log.allowed_scopes_json == ["internal_shared", "external_public"]
        finally:
            await cleanup_faqs(db_session, [faq_internal.id, faq_external.id])

    async def test_admin_scope_priority_admin_private(
        self, client, admin_user, db_session, tracked_users, chat_rag_factory
    ):
        normalized = normalize_question(STD_QUESTION_FULL)
        digest = question_hash(normalized)
        faq_admin = await create_faq_record(
            db_session,
            knowledge_scope="admin_private",
            question=STD_QUESTION,
            normalized_question=normalized,
            normalized_question_hash=digest,
            answer="管理员专属答案",
            created_by_user_id=admin_user["user_id"],
        )
        faq_internal = await create_faq_record(
            db_session,
            knowledge_scope="internal_shared",
            question=STD_QUESTION,
            normalized_question=normalized,
            normalized_question_hash=digest,
            answer="内部共享答案",
            created_by_user_id=admin_user["user_id"],
        )
        await db_session.commit()
        try:
            _, token = await _make_user(db_session, tracked_users, client, role="admin")
            session_id = await _create_session(client, token)
            resp = await client.post(
                SSE_STREAM_PATH.format(sid=session_id),
                headers=await bearer_headers(token),
                json={"question": STD_QUESTION_FULL},
            )
            assert resp.status_code == 200
            final = parse_sse(resp.text)[-1][1]
            assert final["answer"] == "管理员专属答案"
            log = await db_session.scalar(
                select(QaAccessLog).where(QaAccessLog.session_id == session_id)
            )
            assert log.faq_id == faq_admin.id
            assert log.allowed_scopes_json == [
                "admin_private",
                "internal_shared",
                "external_public",
            ]
        finally:
            await cleanup_faqs(db_session, [faq_admin.id, faq_internal.id])
