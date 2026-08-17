"""Stage 6 外部知识 API 契约测试（真实 DB + 进程内 FakeQueryRag）。

覆盖《API 接口设计》§10 / §11 / §16 / §17.1 冻结契约：
1. 合法 Service Key 可访问（含外部会话复用）；
2. 缺少/错误 Service Key → 401 SERVICE_AUTH_FAILED；
3. dataset_ids 等越权字段 → 422；
4. dataset_id / knowledge_scope / role 等越权字段 → 422；
5. 外部请求最终固定 allowed_scopes=[external_public]；
6. 上游只收到 RAG_EXTERNAL_DATASET_ID；
7. 上游身份只使用 RAG_SERVICE_USER_EXTERNAL；
8. external_public FAQ 精确命中不调用 RAG；
9. internal_shared / admin_private FAQ 不得被外部命中；
10. RAG 路径 SSE 契约正确，final/error 互斥且为最后业务事件；
11. external_user_id 原文不进库，只保存加盐哈希；
12. qa_access_logs.channel=external_api、user_id=null；
13. 非法/超长/归一化后空问题按契约拒绝；
14. 响应不得出现内部 Dataset ID / 服务身份 / Service Key。

测试使用现有 stub/mock 体系（FakeQueryRag），不依赖问问小安源码。
"""

import json

import httpx
import pytest
from sqlalchemy import select

from app.core.config import get_settings
from app.core.normalizer import normalize_question, question_hash
from app.core.security import external_subject_hash
from app.models.chat_message import ChatMessage
from app.models.chat_session import ChatSession
from app.models.qa_access_log import QaAccessLog
from tests.integration.conftest import _unique, create_faq_record
from tests.integration.fake_query_rag_server import FakeQueryRag

EXTERNAL_STREAM_PATH = "/api/v1/external/knowledge/messages:stream"

SETTINGS = get_settings()
SERVICE_KEY = SETTINGS.service_api_key
EXTERNAL_DATASET_ID = SETTINGS.rag_external_dataset_id
EXTERNAL_SERVICE_USER = SETTINGS.rag_service_user_external


def parse_sse(text: str) -> list[tuple[str, dict]]:
    """把平台 SSE 响应解析为 [(event, data)]。"""
    events: list[tuple[str, dict]] = []
    for block in text.split("\n\n"):
        block = block.strip()
        if not block:
            continue
        event = "message"
        data_lines: list[str] = []
        for line in block.splitlines():
            if line.startswith("event:"):
                event = line[len("event:") :].strip()
            elif line.startswith("data:"):
                data_lines.append(line[len("data:") :].strip())
        if data_lines:
            events.append((event, json.loads("\n".join(data_lines))))
    return events


def _headers(key: str | None = None) -> dict[str, str]:
    headers = {"Accept": "text/event-stream"}
    if key is not None:
        headers["X-Service-Key"] = key
    return headers


def _body(
    external_session_id: str = "ext_sess_001",
    external_user_id: str = "opaque_user_001",
    question: str = "如何查看账户的风险等级？",
) -> dict:
    return {
        "external_session_id": external_session_id,
        "external_user_id": external_user_id,
        "question": question,
    }


def _rag_stream_events() -> list[tuple[str, dict]]:
    return [
        ("ready", {}),
        ("progress", {"status": "processing"}),
        ("delta", {"delta": "您的风险等级为"}),
        ("delta", {"delta": "C3 稳健型。"}),
        (
            "final",
            {
                "answer": "您的风险等级为C3 稳健型。",
                "status": "completed",
                "image_urls": [],
                "trace_id": "trace-ext-1",
                "citations": [
                    {
                        "document_id": "doc-ext-1",
                        "chunk_id": "chunk-ext-11",
                        "title": "外部公开资料",
                        "score": 0.9,
                        "source_type": "local",
                    }
                ],
                "terminal_reason_code": "completed",
            },
        ),
    ]


async def _cleanup_external(session, session_ids: list[str], faq_ids: list[str]) -> None:
    from sqlalchemy import delete

    from app.models.faq import Faq

    if session_ids:
        await session.execute(delete(ChatMessage).where(ChatMessage.session_id.in_(session_ids)))
        await session.execute(delete(QaAccessLog).where(QaAccessLog.session_id.in_(session_ids)))
        await session.execute(delete(ChatSession).where(ChatSession.id.in_(session_ids)))
    if faq_ids:
        await session.execute(delete(Faq).where(Faq.id.in_(faq_ids)))
    await session.commit()


@pytest.fixture
async def external_scope(db_session):
    """跟踪本测试创建的外部会话与 FAQ，teardown 统一清理。"""
    session_ids: list[str] = []
    faq_ids: list[str] = []
    yield {"session_ids": session_ids, "faq_ids": faq_ids}
    await _cleanup_external(db_session, session_ids, faq_ids)


@pytest.fixture
def external_rag_factory(monkeypatch):
    """注入进程内 FakeQueryRag 到外部路由（与内部 chat_rag_factory 同一模式）。"""
    import app.api.v1.external as external_mod
    from app.rag.rag_query_client import RagQueryClient
    from app.rag.rag_trace_client import RagTraceClient
    from app.repositories.audit_log_repository import AuditLogRepository
    from app.repositories.chat_message_repository import ChatMessageRepository
    from app.repositories.chat_session_repository import ChatSessionRepository
    from app.repositories.faq_repository import FaqRepository
    from app.repositories.qa_access_log_repository import QaAccessLogRepository
    from app.services.audit_service import AuditService
    from app.services.chat_service import ChatService

    def install(fake: FakeQueryRag, session) -> FakeQueryRag:
        service = ChatService(
            sessions=ChatSessionRepository(session),
            messages=ChatMessageRepository(session),
            logs=QaAccessLogRepository(session),
            faq_repository=FaqRepository(session),
            audit=AuditService(AuditLogRepository(session)),
            query_client=RagQueryClient(
                base_url="http://rag", transport=httpx.MockTransport(fake.handler)
            ),
            trace_client=RagTraceClient(
                base_url="http://rag", transport=httpx.MockTransport(fake.handler)
            ),
        )
        monkeypatch.setattr(external_mod, "_external_service", lambda s: service)
        return fake

    return install


def _sse_session_id(resp) -> str:
    events = parse_sse(resp.text)
    return events[0][1]["session_id"]


class TestServiceKey:
    async def test_valid_service_key_streams_ok(
        self, client, db_session, external_rag_factory, external_scope
    ):
        """1. 合法 Service Key 可访问；两轮问答复用同一外部会话与上游 session。"""
        fake = external_rag_factory(FakeQueryRag(), db_session)
        body = _body()
        fake.seed_default_stream(_rag_stream_events())  # session 由路由动态生成，走兜底流

        resp1 = await client.post(EXTERNAL_STREAM_PATH, headers=_headers(SERVICE_KEY), json=body)
        assert resp1.status_code == 200, resp1.text
        events1 = parse_sse(resp1.text)
        assert events1[-1][0] == "final"
        session_id1 = events1[0][1]["session_id"]
        external_scope["session_ids"].append(session_id1)
        # 平台会话映射落库：channel=external_api、external_session_id 保留、user_id 为空
        chat_session = await db_session.get(ChatSession, session_id1)
        assert chat_session.channel == "external_api"
        assert chat_session.user_id is None
        assert chat_session.external_session_id == body["external_session_id"]
        assert chat_session.external_subject_hash == external_subject_hash(body["external_user_id"])

        # 第二轮：同一外部会话 → 同一平台 session → 复用上游多轮上下文
        fake.seed_stream(session_id1, _rag_stream_events())
        resp2 = await client.post(EXTERNAL_STREAM_PATH, headers=_headers(SERVICE_KEY), json=body)
        assert resp2.status_code == 200, resp2.text
        events2 = parse_sse(resp2.text)
        session_id2 = events2[0][1]["session_id"]
        assert session_id2 == session_id1
        assert len(fake.query_calls) == 2
        assert fake.query_calls[0]["session_id"] == fake.query_calls[1]["session_id"] == session_id1

    async def test_missing_service_key_rejected(self, client, db_session, external_scope):
        """2a. 缺少 Service Key → 401 SERVICE_AUTH_FAILED。"""
        resp = await client.post(EXTERNAL_STREAM_PATH, headers=_headers(None), json=_body())
        assert resp.status_code == 401
        assert resp.json()["error"]["code"] == "SERVICE_AUTH_FAILED"

    async def test_wrong_service_key_rejected(self, client, db_session, external_scope):
        """2b. 错误 Service Key → 401 SERVICE_AUTH_FAILED。"""
        resp = await client.post(
            EXTERNAL_STREAM_PATH, headers=_headers("wrong-key-value"), json=_body()
        )
        assert resp.status_code == 401
        assert resp.json()["error"]["code"] == "SERVICE_AUTH_FAILED"
        # 失败响应不得泄漏 Service Key / Dataset / 服务身份
        text = resp.text
        assert SERVICE_KEY not in text
        assert EXTERNAL_DATASET_ID not in text
        assert EXTERNAL_SERVICE_USER not in text


class TestForbiddenFields:
    async def _assert_422(self, client, payload: dict) -> None:
        resp = await client.post(EXTERNAL_STREAM_PATH, headers=_headers(SERVICE_KEY), json=payload)
        assert resp.status_code == 422, resp.text
        assert resp.json()["error"]["code"] == "VALIDATION_ERROR"

    async def test_dataset_ids_rejected(self, client, db_session, external_scope):
        """3. dataset_ids → 422。"""
        await self._assert_422(client, {**_body(), "dataset_ids": ["securities_internal_shared"]})

    async def test_dataset_id_rejected(self, client, db_session, external_scope):
        """4a. dataset_id → 422。"""
        await self._assert_422(client, {**_body(), "dataset_id": "securities_admin_private"})

    async def test_knowledge_scope_rejected(self, client, db_session, external_scope):
        """4b. knowledge_scope → 422。"""
        await self._assert_422(client, {**_body(), "knowledge_scope": "internal_shared"})

    async def test_allowed_scopes_rejected(self, client, db_session, external_scope):
        """4c. allowed_scopes → 422。"""
        await self._assert_422(client, {**_body(), "allowed_scopes": ["internal_shared"]})

    async def test_role_rejected(self, client, db_session, external_scope):
        """4d. role → 422。"""
        await self._assert_422(client, {**_body(), "role": "admin"})


class TestExternalScoping:
    async def test_fixed_external_scope_dataset_and_service_user(
        self, client, db_session, external_rag_factory, external_scope
    ):
        """5/6/7. 外部请求固定 external_public / 单 external Dataset / 外部服务身份。"""
        fake = external_rag_factory(FakeQueryRag(), db_session)
        fake.seed_default_stream(_rag_stream_events())
        resp = await client.post(EXTERNAL_STREAM_PATH, headers=_headers(SERVICE_KEY), json=_body())
        assert resp.status_code == 200, resp.text
        session_id = _sse_session_id(resp)
        external_scope["session_ids"].append(session_id)
        assert parse_sse(resp.text)[-1][0] == "final"

        # 6. 上游只收到 external Dataset；7. 上游身份固定 external service user
        call = fake.query_calls[-1]
        assert call["dataset_ids"] == [EXTERNAL_DATASET_ID]
        assert call["service_user"] == EXTERNAL_SERVICE_USER
        assert call["is_stream"] is True

        # 5. 访问日志 allowed_scopes_json 固定为 [external_public]
        log = await db_session.scalar(
            select(QaAccessLog).where(QaAccessLog.session_id == session_id)
        )
        assert log.allowed_scopes_json == ["external_public"]

    async def test_external_public_faq_hit_skips_rag(
        self, client, db_session, external_rag_factory, external_scope, admin_user
    ):
        """8. external_public FAQ 精确命中：不调用 RAG，final 直接交付 faq_cache 答案。"""
        fake = external_rag_factory(FakeQueryRag(), db_session)
        question = f"{_unique('外部公开FAQ')}如何开通两融权限？"
        normalized = normalize_question(question)
        faq = await create_faq_record(
            db_session,
            knowledge_scope="external_public",
            question=question,
            normalized_question=normalized,
            normalized_question_hash=question_hash(normalized),
            answer="外部公开答案：满足 50 万资产可开通。",
            created_by_user_id=admin_user["user_id"],
        )
        external_scope["faq_ids"].append(faq.id)
        await db_session.commit()

        resp = await client.post(
            EXTERNAL_STREAM_PATH,
            headers=_headers(SERVICE_KEY),
            json=_body(question=question),
        )
        assert resp.status_code == 200, resp.text
        events = parse_sse(resp.text)
        names = [name for name, _ in events]
        assert names[-1] == "final"
        final = events[-1][1]
        assert final["answer"] == "外部公开答案：满足 50 万资产可开通。"
        assert final["answer_source"] == "faq_cache"
        assert final["trace_id"] is None
        assert final["citations"] == []
        # FAQ 命中禁止调用原 RAG
        assert len(fake.query_calls) == 0
        session_id = events[0][1]["session_id"]
        external_scope["session_ids"].append(session_id)
        log = await db_session.scalar(
            select(QaAccessLog).where(QaAccessLog.session_id == session_id)
        )
        assert log.answer_source == "faq_cache"
        assert log.rag_trace_id is None
        assert log.citation_count == 0

    async def test_internal_and_admin_faq_not_hit_externally(
        self, client, db_session, external_rag_factory, external_scope, admin_user
    ):
        """9. internal_shared / admin_private FAQ 不得被外部命中（外部仍走 RAG）。"""
        fake = external_rag_factory(FakeQueryRag(), db_session)
        question = f"{_unique('内部专属FAQ')}内部费率如何查询？"
        normalized = normalize_question(question)
        for scope in ("internal_shared", "admin_private"):
            faq = await create_faq_record(
                db_session,
                knowledge_scope=scope,
                question=question,
                normalized_question=normalized,
                normalized_question_hash=question_hash(normalized),
                answer=f"{scope} 专属答案",
                created_by_user_id=admin_user["user_id"],
            )
            external_scope["faq_ids"].append(faq.id)
        await db_session.commit()

        fake.seed_default_stream(_rag_stream_events())
        resp = await client.post(
            EXTERNAL_STREAM_PATH,
            headers=_headers(SERVICE_KEY),
            json=_body(question=question),
        )
        assert resp.status_code == 200, resp.text
        session_id = _sse_session_id(resp)
        external_scope["session_ids"].append(session_id)
        events = parse_sse(resp.text)
        final = events[-1][1]
        # 未命中内部 FAQ → RAG 路径
        assert final["answer_source"] == "rag"
        assert len(fake.query_calls) == 1
        # 即使命中也不可能：日志里 answer_source 只能是 rag
        log = await db_session.scalar(
            select(QaAccessLog).where(QaAccessLog.session_id == session_id)
        )
        assert log.answer_source == "rag"
        assert log.faq_id is None


class TestSseContract:
    async def test_rag_sse_contract_and_terminal_exclusive(
        self, client, db_session, external_rag_factory, external_scope
    ):
        """10. RAG 路径 SSE 契约：事件序列正确、final/error 互斥且为最后业务事件。"""
        fake = external_rag_factory(FakeQueryRag(), db_session)
        fake.seed_default_stream(_rag_stream_events())
        resp = await client.post(EXTERNAL_STREAM_PATH, headers=_headers(SERVICE_KEY), json=_body())
        assert resp.status_code == 200, resp.text
        events = parse_sse(resp.text)
        names = [name for name, _ in events]

        # ready 是第一个事件，携带 request_id/turn_id/session_id
        assert names[0] == "ready"
        ready = events[0][1]
        assert ready["request_id"]
        assert ready["turn_id"]
        assert ready["session_id"]
        external_scope["session_ids"].append(ready["session_id"])
        request_id = ready["request_id"]

        # progress 阶段合法（faq_lookup → rag_submit → rag_progress）
        stages = [data["stage"] for name, data in events if name == "progress"]
        assert stages[0] == "faq_lookup"
        assert "rag_submit" in stages
        assert "rag_progress" in stages
        assert all(s in {"faq_lookup", "rag_submit", "rag_progress", "finalizing"} for s in stages)

        # delta 平台契约 {text}（不是上游 {delta}）
        deltas = [data for name, data in events if name == "delta"]
        assert len(deltas) == 2
        assert all("text" in d and "delta" not in d for d in deltas)

        # 终态唯一且最后：final/error 二选一
        assert names[-1] == "final"
        assert "final" not in names[:-1]
        assert "error" not in names
        assert names.count("final") + names.count("error") == 1

        final = events[-1][1]
        assert final["request_id"] == request_id
        assert final["turn_id"] == ready["turn_id"]
        assert final["answer_source"] == "rag"
        assert final["trace_id"] == "trace-ext-1"
        assert final["terminal_reason_code"] == "completed"
        assert len(final["citations"]) == 1
        assert final["citations"][0]["document_id"] == "doc-ext-1"

    async def test_rag_error_terminal_also_exclusive(
        self, client, db_session, external_rag_factory, external_scope
    ):
        """10b. RAG 不可用 → 单一 error 终态（无 final），不泄漏上游异常。"""
        fake = external_rag_factory(FakeQueryRag(), db_session)
        fake.fail_submit = "unavailable"
        resp = await client.post(EXTERNAL_STREAM_PATH, headers=_headers(SERVICE_KEY), json=_body())
        assert resp.status_code == 200, resp.text
        events = parse_sse(resp.text)
        names = [name for name, _ in events]
        assert names[-1] == "error"
        assert "final" not in names
        assert names.count("final") + names.count("error") == 1
        error = events[-1][1]
        assert error["code"] == "RAG_UNAVAILABLE"
        assert "Traceback" not in error["message"]
        session_id = events[0][1]["session_id"]
        external_scope["session_ids"].append(session_id)
        # 失败 Turn 仍写访问日志（channel=external_api）
        log = await db_session.scalar(
            select(QaAccessLog).where(QaAccessLog.session_id == session_id)
        )
        assert log.status == "failed"
        assert log.error_code == "RAG_UNAVAILABLE"


class TestPersistenceAndPrivacy:
    async def test_external_user_id_never_persisted_raw(
        self, client, db_session, external_rag_factory, external_scope
    ):
        """11. external_user_id 原文不进入数据库，只保存加盐哈希。"""
        fake = external_rag_factory(FakeQueryRag(), db_session)
        fake.seed_default_stream(_rag_stream_events())
        raw_user_id = "customer_opaque_id_隐私原文_9988"
        body = _body(external_user_id=raw_user_id)
        resp = await client.post(EXTERNAL_STREAM_PATH, headers=_headers(SERVICE_KEY), json=body)
        assert resp.status_code == 200, resp.text
        session_id = _sse_session_id(resp)
        external_scope["session_ids"].append(session_id)
        expected_hash = external_subject_hash(raw_user_id)

        chat_session = await db_session.get(ChatSession, session_id)
        assert chat_session.external_subject_hash == expected_hash
        assert chat_session.external_subject_hash != raw_user_id

        log = await db_session.scalar(
            select(QaAccessLog).where(QaAccessLog.session_id == session_id)
        )
        assert log.external_subject_hash == expected_hash
        assert log.external_subject_hash != raw_user_id
        # 原始用户 ID 不得出现在消息/日志内容中
        rows = (
            await db_session.scalars(
                select(ChatMessage).where(ChatMessage.session_id == session_id)
            )
        ).all()
        for row in rows:
            assert raw_user_id not in (row.content or "")
        assert raw_user_id not in (log.question or "")

    async def test_access_log_channel_and_null_user(
        self, client, db_session, external_rag_factory, external_scope
    ):
        """12. qa_access_logs.channel=external_api、user_id=null、external_subject_hash 非空。"""
        fake = external_rag_factory(FakeQueryRag(), db_session)
        fake.seed_default_stream(_rag_stream_events())
        resp = await client.post(EXTERNAL_STREAM_PATH, headers=_headers(SERVICE_KEY), json=_body())
        assert resp.status_code == 200, resp.text
        session_id = _sse_session_id(resp)
        external_scope["session_ids"].append(session_id)
        log = await db_session.scalar(
            select(QaAccessLog).where(QaAccessLog.session_id == session_id)
        )
        assert log.channel == "external_api"
        assert log.user_id is None
        assert log.external_subject_hash
        # 外部会话本身 user_id 为空
        chat_session = await db_session.get(ChatSession, session_id)
        assert chat_session.channel == "external_api"
        assert chat_session.user_id is None


class TestQuestionValidation:
    async def _post(self, client, payload: dict):
        return await client.post(EXTERNAL_STREAM_PATH, headers=_headers(SERVICE_KEY), json=payload)

    async def test_overlong_fields_422(self, client, db_session, external_scope):
        """13a. 超长字段 → 422。"""
        base = _body()
        cases = [
            {**base, "external_session_id": "x" * 121},
            {**base, "external_user_id": "x" * 201},
            {**base, "question": "x" * 4001},
        ]
        for payload in cases:
            resp = await self._post(client, payload)
            assert resp.status_code == 422, payload

    async def test_illegal_field_types_422(self, client, db_session, external_scope):
        """13b. 非法类型/空必填 → 422。"""
        base = _body()
        cases = [
            {**base, "question": 123},
            {**base, "external_session_id": ""},
            {**base, "external_user_id": ""},
            {**base, "question": ""},
            {
                "external_session_id": base["external_session_id"],
                "question": base["question"],
            },  # 缺 external_user_id
        ]
        for payload in cases:
            resp = await self._post(client, payload)
            assert resp.status_code == 422, payload

    async def test_normalized_empty_question_400(self, client, db_session, external_scope):
        """13c. 归一化后为空（空白/纯句尾可删标点）→ 400 EMPTY_QUESTION。"""
        for question in ("   ", "？？", "？？？", "。。。", "?"):
            resp = await self._post(client, _body(question=question))
            assert resp.status_code == 400, question
            assert resp.json()["error"]["code"] == "EMPTY_QUESTION"


class TestNoInternalLeak:
    async def test_response_leaks_no_internal_identity(
        self, client, db_session, external_rag_factory, external_scope
    ):
        """14. 响应不得出现内部 Dataset ID / 服务身份 / Service Key / 内部异常。"""
        fake = external_rag_factory(FakeQueryRag(), db_session)
        fake.seed_default_stream(_rag_stream_events())
        resp = await client.post(EXTERNAL_STREAM_PATH, headers=_headers(SERVICE_KEY), json=_body())
        assert resp.status_code == 200, resp.text
        session_id = _sse_session_id(resp)
        external_scope["session_ids"].append(session_id)
        text = resp.text
        assert EXTERNAL_DATASET_ID not in text
        assert EXTERNAL_SERVICE_USER not in text
        assert SERVICE_KEY not in text
        assert "X-User-Id" not in text
        assert "Traceback" not in text
