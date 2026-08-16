"""Stage 3 rebuild/delete 集成测试（真实 DB + 进程内 FakeRag）。

覆盖（§二十五/§二十六/§五十三）：rebuild 202 + 轮询、状态拒绝、
delete 上游成功后才标删除、上游失败保持原状态、已删除 404、employee 403。
"""

import httpx
import pytest

from app.core.time import utc_now_naive
from app.models.managed_document import ManagedDocument
from app.rag.rag_document_client import RagDocumentClient
from app.rag.rag_import_client import RagImportClient
from app.repositories.audit_log_repository import AuditLogRepository
from app.repositories.document_replacement_repository import DocumentReplacementRepository
from app.repositories.managed_document_repository import ManagedDocumentRepository
from app.repositories.rag_integration_task_repository import RagIntegrationTaskRepository
from app.services.audit_service import AuditService
from app.services.document_service import DocumentService
from app.services.task_service import TaskService
from tests.integration.conftest import _unique, api_login, bearer_headers
from tests.integration.fake_rag_server import FakeRag

_test_clients = []


def _track(client) -> None:
    _test_clients.append(client)


@pytest.fixture(autouse=True)
async def _close_test_clients():
    yield
    while _test_clients:
        c = _test_clients.pop()
        await c.aclose()


def _install(fake: FakeRag, session, monkeypatch) -> None:
    import app.api.v1.documents as documents_mod
    import app.api.v1.integration as integration_mod

    import_client = RagImportClient(
        base_url="http://rag", transport=httpx.MockTransport(fake.handler)
    )
    doc_client = RagDocumentClient(
        base_url="http://rag", transport=httpx.MockTransport(fake.handler)
    )
    _track(import_client)
    _track(doc_client)

    doc_service = DocumentService(
        docs=ManagedDocumentRepository(session),
        tasks=RagIntegrationTaskRepository(session),
        replacements=DocumentReplacementRepository(session),
        audit=AuditService(AuditLogRepository(session)),
        import_client=import_client,
        document_client=doc_client,
    )
    task_service = TaskService(
        tasks=RagIntegrationTaskRepository(session),
        docs=ManagedDocumentRepository(session),
        replacements=DocumentReplacementRepository(session),
        import_client=import_client,
        document_client=doc_client,
    )
    monkeypatch.setattr(documents_mod, "_document_service", lambda s: doc_service)
    monkeypatch.setattr(integration_mod, "_task_service", lambda s: task_service)


async def _seed_document(
    session,
    *,
    created_by_user_id: str,
    rag_document_id: str,
    platform_status: str,
    index_version: int = 1,
) -> ManagedDocument:
    now = utc_now_naive()
    doc = ManagedDocument(
        rag_document_id=rag_document_id,
        rag_dataset_id="securities_internal_shared",
        knowledge_scope="internal_shared",
        file_name=f"{rag_document_id}.pdf",
        source_kind="manual_upload",
        index_version=index_version,
        platform_status=platform_status,
        rag_status="completed" if platform_status == "active" else "failed",
        chunk_count=5,
        created_by_user_id=created_by_user_id,
        created_at=now,
        updated_at=now,
    )
    session.add(doc)
    await session.flush()
    return doc


async def _read_doc(session, doc_id):
    """提交 service 层未提交修改后，用独立 session 读取（避开 identity map 过期对象）。"""
    await session.commit()
    from app.core.database import get_session_factory

    factory = get_session_factory()
    async with factory() as s2:
        return await s2.get(ManagedDocument, doc_id)


async def _admin_token(client, admin_user) -> str:
    resp = await api_login(client, admin_user["username"], admin_user["password"])
    return resp.json()["data"]["access_token"]


class TestRebuild:
    async def test_rebuild_returns_202_and_completes(
        self, client, admin_user, db_session, monkeypatch
    ):
        fake = FakeRag()
        fake.seed_document(
            "rebuild_doc_1",
            dataset_id="securities_internal_shared",
            visibility="shared",
            chunk_count=5,
        )
        fake.seed_chunks("rebuild_doc_1", 5)
        _install(fake, db_session, monkeypatch)
        doc = await _seed_document(
            db_session,
            created_by_user_id=admin_user["user_id"],
            rag_document_id="rebuild_doc_1",
            platform_status="active",
        )
        await db_session.commit()

        token = await _admin_token(client, admin_user)
        resp = await client.post(
            f"/api/v1/admin/documents/{doc.id}/rebuild",
            headers=await bearer_headers(token),
        )
        assert resp.status_code == 202
        data = resp.json()["data"]
        assert data["task_id"]
        assert data["operation"] == "document_rebuild"
        assert fake.rebuild_calls == 1

        # 轮询完成
        new_task_id = data["task_id"]
        rag_task_id = next(iter(fake.tasks))  # rebuild 产生的上游 task
        fake.set_task_status(rag_task_id, "completed")
        fake.documents["rebuild_doc_1"]["status"] = "completed"
        fake.documents["rebuild_doc_1"]["index_version"] = 2
        fake.documents["rebuild_doc_1"]["chunk_count"] = 8

        resp = await client.get(
            f"/api/v1/admin/integration/tasks/{new_task_id}",
            headers=await bearer_headers(token),
        )
        assert resp.json()["data"]["status"] == "succeeded"
        doc_after = await _read_doc(db_session, doc.id)
        assert doc_after.index_version == 2
        assert doc_after.chunk_count == 8
        assert doc_after.platform_status == "active"

    async def test_rebuild_rejects_importing(self, client, admin_user, db_session, monkeypatch):
        fake = FakeRag()
        fake.seed_document(
            "rebuild_doc_1", dataset_id="securities_internal_shared", visibility="shared"
        )
        _install(fake, db_session, monkeypatch)
        doc = await _seed_document(
            db_session,
            created_by_user_id=admin_user["user_id"],
            rag_document_id="rebuild_doc_1",
            platform_status="importing",
        )
        await db_session.commit()

        token = await _admin_token(client, admin_user)
        resp = await client.post(
            f"/api/v1/admin/documents/{doc.id}/rebuild",
            headers=await bearer_headers(token),
        )
        assert resp.status_code == 409
        assert fake.rebuild_calls == 0


class TestDelete:
    async def test_delete_after_upstream_success(self, client, admin_user, db_session, monkeypatch):
        fake = FakeRag()
        fake.seed_document(
            "delete_doc_1", dataset_id="securities_internal_shared", visibility="shared"
        )
        _install(fake, db_session, monkeypatch)
        doc = await _seed_document(
            db_session,
            created_by_user_id=admin_user["user_id"],
            rag_document_id="delete_doc_1",
            platform_status="active",
        )
        await db_session.commit()

        token = await _admin_token(client, admin_user)
        resp = await client.delete(
            f"/api/v1/admin/documents/{doc.id}",
            headers=await bearer_headers(token),
        )
        assert resp.status_code == 200
        assert fake.delete_calls == 1
        doc_after = await _read_doc(db_session, doc.id)
        assert doc_after.platform_status == "deleted"
        assert doc_after.deleted_at is not None

    async def test_delete_upstream_failure_keeps_mapping(
        self, client, admin_user, db_session, monkeypatch
    ):
        fake = FakeRag()
        fake.seed_document(
            "delete_doc_1", dataset_id="securities_internal_shared", visibility="shared"
        )
        fake.fail_delete = True
        _install(fake, db_session, monkeypatch)
        doc = await _seed_document(
            db_session,
            created_by_user_id=admin_user["user_id"],
            rag_document_id="delete_doc_1",
            platform_status="active",
        )
        await db_session.commit()

        token = await _admin_token(client, admin_user)
        resp = await client.delete(
            f"/api/v1/admin/documents/{doc.id}",
            headers=await bearer_headers(token),
        )
        assert resp.status_code == 409
        doc_after = await _read_doc(db_session, doc.id)
        assert doc_after.platform_status == "active"  # 上游失败不提前标 deleted
        assert doc_after.deleted_at is None

    async def test_delete_already_deleted_is_404(self, client, admin_user, db_session, monkeypatch):
        fake = FakeRag()
        _install(fake, db_session, monkeypatch)
        doc = await _seed_document(
            db_session,
            created_by_user_id=admin_user["user_id"],
            rag_document_id="gone_doc",
            platform_status="deleted",
        )
        await db_session.commit()

        token = await _admin_token(client, admin_user)
        resp = await client.delete(
            f"/api/v1/admin/documents/{doc.id}",
            headers=await bearer_headers(token),
        )
        assert resp.status_code == 404


class TestAccess:
    async def test_employee_forbidden_on_documents(self, client, admin_user, tracked_users):
        token = await _admin_token(client, admin_user)
        username = _unique("it_emp")
        created = await client.post(
            "/api/v1/admin/users",
            headers=await bearer_headers(token),
            json={
                "username": username,
                "display_name": "员工",
                "role": "employee",
                "initial_password": "Emp@12345",
            },
        )
        tracked_users.append(created.json()["data"]["id"])
        emp_token = (await api_login(client, username, "Emp@12345")).json()["data"]["access_token"]
        for method, path in [
            ("get", "/api/v1/admin/documents"),
            ("get", "/api/v1/admin/integration/tasks/whatever"),
        ]:
            resp = await client.request(
                method.upper(), path, headers=await bearer_headers(emp_token)
            )
            assert resp.status_code == 403
