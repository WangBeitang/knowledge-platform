"""阶段 2 集成测试公共夹具。

数据库不可达时整组跳过（与 stage 1 契约测试一致的本地 CI 策略）；
测试用户使用唯一前缀，测试结束尽力清理（审计/会话/用户）。
"""

import uuid

import httpx
import pymysql
import pytest
from httpx import ASGITransport

from app.core.config import get_settings
from app.core.database import get_session_factory, init_engine
from app.core.enums import UserStatus
from app.core.security import hash_password
from app.core.time import utc_now_naive
from app.main import app
from app.models.user import User

DB_UNREACHABLE = False


def _probe_db() -> bool:
    s = get_settings()
    try:
        conn = pymysql.connect(
            host=s.db_host,
            port=s.db_port,
            user=s.db_user,
            password=s.db_password,
            database=s.db_name,
            connect_timeout=5,
            charset="utf8mb4",
        )
        conn.close()
        return True
    except Exception:  # noqa: BLE001
        return False


if not _probe_db():
    DB_UNREACHABLE = True

pytestmark = pytest.mark.skipif(DB_UNREACHABLE, reason="数据库不可达，跳过集成测试")


@pytest.fixture
async def client():
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.fixture
async def db_session():
    init_engine()
    factory = get_session_factory()
    async with factory() as session:
        yield session


def _unique(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:10]}"


async def create_user_record(
    session, *, username: str, display_name: str, role: str, password: str
) -> User:
    """直接通过 ORM 创建用户（测试夹具，不走服务层）。"""
    now = utc_now_naive()
    user = User(
        username=username,
        display_name=display_name,
        password_hash=hash_password(password),
        role=role,
        status=UserStatus.active.value,
        password_changed_at=now,
        created_at=now,
        updated_at=now,
    )
    session.add(user)
    await session.flush()
    return user


async def cleanup_user(session, user_id: str) -> None:
    """尽力清理测试用户相关记录（聊天 → 文档映射 → 审计 → 会话 → 用户）。"""
    from sqlalchemy import delete, select

    from app.models.audit_log import AuditLog
    from app.models.auth_session import AuthSession
    from app.models.chat_message import ChatMessage
    from app.models.chat_session import ChatSession
    from app.models.document_replacement import DocumentReplacement
    from app.models.managed_document import ManagedDocument
    from app.models.qa_access_log import QaAccessLog
    from app.models.rag_integration_task import RagIntegrationTask

    # 聊天数据（FK：messages/logs → sessions → users）
    session_ids = select(ChatSession.id).where(ChatSession.user_id == user_id)
    await session.execute(delete(ChatMessage).where(ChatMessage.session_id.in_(session_ids)))
    await session.execute(delete(QaAccessLog).where(QaAccessLog.session_id.in_(session_ids)))
    await session.execute(delete(ChatSession).where(ChatSession.user_id == user_id))

    # 先清理该用户创建的文档映射及其关联（FK 顺序：replacements → tasks → documents）
    doc_ids = select(ManagedDocument.id).where(ManagedDocument.created_by_user_id == user_id)
    await session.execute(
        delete(DocumentReplacement).where(
            (DocumentReplacement.old_managed_document_id.in_(doc_ids))
            | (DocumentReplacement.new_managed_document_id.in_(doc_ids))
        )
    )
    await session.execute(
        delete(RagIntegrationTask).where(RagIntegrationTask.managed_document_id.in_(doc_ids))
    )
    await session.execute(
        delete(ManagedDocument).where(ManagedDocument.created_by_user_id == user_id)
    )

    await session.execute(
        delete(AuditLog).where(
            (AuditLog.operator_user_id == user_id) | (AuditLog.resource_id == user_id)
        )
    )
    await session.execute(delete(AuthSession).where(AuthSession.user_id == user_id))
    await session.execute(delete(User).where(User.id == user_id))
    await session.commit()


@pytest.fixture
async def admin_user(db_session):
    """创建一个唯一 active admin（测试用），结束清理。"""
    username = _unique("it_admin")
    password = "TestAdmin#2026"
    user = await create_user_record(
        db_session,
        username=username,
        display_name="集成测试管理员",
        role="admin",
        password=password,
    )
    user_id = user.id  # 提前取主键，避免测试内 rollback 过期 ORM 对象后 teardown 触发懒加载
    await db_session.commit()
    yield {"user": user, "user_id": user_id, "username": username, "password": password}
    await cleanup_user(db_session, user_id)


@pytest.fixture
async def tracked_users(db_session):
    """跟踪测试内创建的用户，teardown 时统一清理。"""
    created: list[str] = []
    yield created
    for user_id in created:
        await cleanup_user(db_session, user_id)


async def api_login(client: httpx.AsyncClient, username: str, password: str) -> httpx.Response:
    return await client.post(
        "/api/v1/auth/login",
        json={"username": username, "password": password},
    )


async def bearer_headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


# ---------- Stage 4 聊天测试夹具 ----------


async def create_faq_record(
    session,
    *,
    knowledge_scope: str,
    question: str,
    normalized_question: str,
    normalized_question_hash: str,
    answer: str,
    created_by_user_id: str,
):
    """直接插入 published FAQ（测试夹具；Stage 5 才有发布接口）。"""
    from app.core.enums import FaqStatus, RagSyncStatus
    from app.models.faq import Faq

    now = utc_now_naive()
    faq = Faq(
        knowledge_scope=knowledge_scope,
        question=question,
        normalized_question=normalized_question,
        normalized_question_hash=normalized_question_hash,
        answer=answer,
        status=FaqStatus.published.value,
        source_candidate_id=None,
        hit_count=0,
        rag_sync_status=RagSyncStatus.pending.value,
        rag_sync_error=None,
        created_by_user_id=created_by_user_id,
        reviewed_by_user_id=created_by_user_id,
        published_at=now,
        updated_at=now,
        unpublished_at=None,
    )
    session.add(faq)
    await session.flush()
    return faq


async def cleanup_faqs(session, faq_ids: list[str]) -> None:
    from sqlalchemy import delete

    from app.models.faq import Faq

    if faq_ids:
        await session.execute(delete(Faq).where(Faq.id.in_(faq_ids)))
        await session.commit()


@pytest.fixture
async def chat_rag_factory(monkeypatch):
    """注入进程内 FakeQueryRag 到 chat 路由（Stage 4 集成测试）。"""
    import httpx

    import app.api.v1.chat as chat_mod
    from app.rag.rag_query_client import RagQueryClient
    from app.rag.rag_trace_client import RagTraceClient
    from app.repositories.audit_log_repository import AuditLogRepository
    from app.repositories.chat_message_repository import ChatMessageRepository
    from app.repositories.chat_session_repository import ChatSessionRepository
    from app.repositories.faq_repository import FaqRepository
    from app.repositories.qa_access_log_repository import QaAccessLogRepository
    from app.services.audit_service import AuditService
    from app.services.chat_service import ChatService
    from tests.integration.fake_query_rag_server import FakeQueryRag

    fakes: list[FakeQueryRag] = []

    def install(fake: FakeQueryRag, session) -> FakeQueryRag:
        fakes.append(fake)
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
        monkeypatch.setattr(chat_mod, "_chat_service", lambda s: service)
        return fake

    return install
