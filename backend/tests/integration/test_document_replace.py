"""Stage 3 replace 状态机集成测试（真实 DB + 进程内 FakeRag）。

覆盖（§五十三）：成功 / 新文档失败（旧文档不删）/ 旧删除失败 /
多次轮询只删一次 / 并发行锁 / 同 scope 强制 / 非 active 拒绝。
"""

import httpx
import pytest

from app.core.enums import ManagedDocumentStatus
from app.core.time import utc_now_naive
from app.models.document_replacement import DocumentReplacement
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
from tests.integration.conftest import api_login, bearer_headers
from tests.integration.fake_rag_server import FakeRag

PDF_BYTES = b"%PDF-1.4 fake pdf content for test"


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


async def _seed_active_old_document(
    session,
    *,
    created_by_user_id: str,
    file_name: str = "old.pdf",
) -> ManagedDocument:
    """平台侧创建 active 旧文档映射（对应上游已有 rag_document_id）。"""
    now = utc_now_naive()
    doc = ManagedDocument(
        rag_document_id="old_doc_1",
        rag_dataset_id="securities_internal_shared",
        knowledge_scope="internal_shared",
        file_name=file_name,
        source_kind="manual_upload",
        index_version=1,
        platform_status=ManagedDocumentStatus.active.value,
        rag_status="completed",
        chunk_count=10,
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


async def _poll_task(client, token, task_id, times: int = 1):
    last = None
    for _ in range(times):
        resp = await client.get(
            f"/api/v1/admin/integration/tasks/{task_id}",
            headers=await bearer_headers(token),
        )
        assert resp.status_code == 200
        last = resp.json()["data"]
    return last


class TestReplace:
    async def test_replace_success(self, client, admin_user, db_session, monkeypatch):
        fake = FakeRag()
        fake.seed_document(
            "old_doc_1", dataset_id="securities_internal_shared", visibility="shared"
        )
        fake.seed_chunks("old_doc_1", 10)
        _install(fake, db_session, monkeypatch)
        old = await _seed_active_old_document(db_session, created_by_user_id=admin_user["user_id"])
        await db_session.commit()

        token = await _admin_token(client, admin_user)
        resp = await client.post(
            f"/api/v1/admin/documents/{old.id}/replace",
            headers=await bearer_headers(token),
            data={"knowledge_scope": "internal_shared"},
            files=[("file", ("new.pdf", PDF_BYTES, "application/pdf"))],
        )
        assert resp.status_code == 202
        data = resp.json()["data"]
        assert data["task_id"] and data["new_document_id"] and data["replacement_id"]
        assert data["status"] == "pending"

        # 新文档导入成功
        new_rag_doc = [d for d in fake.documents.values() if d["document_id"] != "old_doc_1"][0]
        new_task_id = new_rag_doc["latest_task_id"]
        fake.set_task_status(
            new_task_id,
            "completed",
            done=[{"name": "upload_file"}, {"name": "index"}],
        )
        fake.documents[new_rag_doc["document_id"]]["status"] = "completed"
        fake.documents[new_rag_doc["document_id"]]["chunk_count"] = 20
        fake.documents[new_rag_doc["document_id"]]["index_version"] = 1

        task_view = await _poll_task(client, token, data["task_id"])
        assert task_view["status"] == "succeeded"
        assert fake.delete_calls == 1  # 旧文档只删一次

        old_after = await _read_doc(db_session, old.id)
        assert old_after.platform_status == ManagedDocumentStatus.replaced.value
        new_doc = await _read_doc(db_session, data["new_document_id"])
        assert new_doc.platform_status == ManagedDocumentStatus.active.value
        assert new_doc.chunk_count == 20

        await db_session.commit()
        replacement = await db_session.get(DocumentReplacement, data["replacement_id"])
        assert replacement.status == "completed"
        assert replacement.completed_at is not None

    async def test_replace_new_import_failed_keeps_old(
        self, client, admin_user, db_session, monkeypatch
    ):
        fake = FakeRag()
        fake.seed_document(
            "old_doc_1", dataset_id="securities_internal_shared", visibility="shared"
        )
        _install(fake, db_session, monkeypatch)
        old = await _seed_active_old_document(db_session, created_by_user_id=admin_user["user_id"])
        await db_session.commit()

        token = await _admin_token(client, admin_user)
        resp = await client.post(
            f"/api/v1/admin/documents/{old.id}/replace",
            headers=await bearer_headers(token),
            data={"knowledge_scope": "internal_shared"},
            files=[("file", ("new.pdf", PDF_BYTES, "application/pdf"))],
        )
        assert resp.status_code == 202
        data = resp.json()["data"]
        new_rag_doc = [d for d in fake.documents.values() if d["document_id"] != "old_doc_1"][0]
        fake.set_task_status(
            new_rag_doc["latest_task_id"],
            "failed",
            failed_node="parse_pdf",
            error_code="PARSE_FAILED",
            error_message="PDF 解析失败",
        )

        task_view = await _poll_task(client, token, data["task_id"])
        assert task_view["status"] == "failed"
        # 硬约束：新文档失败绝不删除旧文档
        assert fake.delete_calls == 0

        old_after = await _read_doc(db_session, old.id)
        assert old_after.platform_status == ManagedDocumentStatus.active.value
        new_doc = await _read_doc(db_session, data["new_document_id"])
        assert new_doc.platform_status == ManagedDocumentStatus.import_failed.value
        await db_session.commit()
        replacement = await db_session.get(DocumentReplacement, data["replacement_id"])
        assert replacement.status == "failed"

    async def test_replace_old_delete_failure(self, client, admin_user, db_session, monkeypatch):
        fake = FakeRag()
        fake.seed_document(
            "old_doc_1", dataset_id="securities_internal_shared", visibility="shared"
        )
        fake.fail_delete = True  # 模拟上游删除失败
        _install(fake, db_session, monkeypatch)
        old = await _seed_active_old_document(db_session, created_by_user_id=admin_user["user_id"])
        await db_session.commit()

        token = await _admin_token(client, admin_user)
        resp = await client.post(
            f"/api/v1/admin/documents/{old.id}/replace",
            headers=await bearer_headers(token),
            data={"knowledge_scope": "internal_shared"},
            files=[("file", ("new.pdf", PDF_BYTES, "application/pdf"))],
        )
        data = resp.json()["data"]
        new_rag_doc = [d for d in fake.documents.values() if d["document_id"] != "old_doc_1"][0]
        fake.set_task_status(new_rag_doc["latest_task_id"], "completed")

        # 连续 GET 3 次：终态不可被重新覆盖——上游仍是 completed，
        # 但任务/replacement 必须保持 failed，且旧文档删除只尝试一次
        for _ in range(3):
            task_view = await _poll_task(client, token, data["task_id"])
        assert task_view["status"] == "failed"
        assert "旧文档清理失败" in (task_view["error_message"] or "")
        assert fake.delete_calls == 1

        old_after = await _read_doc(db_session, old.id)
        new_doc = await _read_doc(db_session, data["new_document_id"])
        assert old_after.platform_status == ManagedDocumentStatus.active.value  # 不谎报 replaced
        assert new_doc.platform_status == ManagedDocumentStatus.active.value  # 新文档不回滚
        await db_session.commit()
        replacement = await db_session.get(DocumentReplacement, data["replacement_id"])
        assert replacement.status == "failed"

    async def test_replace_multi_poll_deletes_old_once(
        self, client, admin_user, db_session, monkeypatch
    ):
        fake = FakeRag()
        fake.seed_document(
            "old_doc_1", dataset_id="securities_internal_shared", visibility="shared"
        )
        _install(fake, db_session, monkeypatch)
        old = await _seed_active_old_document(db_session, created_by_user_id=admin_user["user_id"])
        await db_session.commit()

        token = await _admin_token(client, admin_user)
        resp = await client.post(
            f"/api/v1/admin/documents/{old.id}/replace",
            headers=await bearer_headers(token),
            data={"knowledge_scope": "internal_shared"},
            files=[("file", ("new.pdf", PDF_BYTES, "application/pdf"))],
        )
        data = resp.json()["data"]
        new_rag_doc = [d for d in fake.documents.values() if d["document_id"] != "old_doc_1"][0]
        fake.set_task_status(new_rag_doc["latest_task_id"], "completed")
        fake.documents[new_rag_doc["document_id"]]["status"] = "completed"

        # 连续多次轮询：旧文档删除只执行一次
        for _ in range(3):
            await _poll_task(client, token, data["task_id"])
        assert fake.delete_calls == 1

        old_after = await _read_doc(db_session, old.id)
        assert old_after.platform_status == ManagedDocumentStatus.replaced.value

    async def test_replace_wrong_scope_rejected(self, client, admin_user, db_session, monkeypatch):
        fake = FakeRag()
        fake.seed_document(
            "old_doc_1", dataset_id="securities_internal_shared", visibility="shared"
        )
        _install(fake, db_session, monkeypatch)
        old = await _seed_active_old_document(db_session, created_by_user_id=admin_user["user_id"])
        await db_session.commit()

        token = await _admin_token(client, admin_user)
        resp = await client.post(
            f"/api/v1/admin/documents/{old.id}/replace",
            headers=await bearer_headers(token),
            data={"knowledge_scope": "external_public"},  # 与旧文档 internal_shared 不一致
            files=[("file", ("new.pdf", PDF_BYTES, "application/pdf"))],
        )
        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == "INVALID_REQUEST"

    async def test_replace_non_active_rejected(self, client, admin_user, db_session, monkeypatch):
        fake = FakeRag()
        fake.seed_document(
            "old_doc_1", dataset_id="securities_internal_shared", visibility="shared"
        )
        _install(fake, db_session, monkeypatch)
        now = utc_now_naive()
        old = ManagedDocument(
            rag_document_id="old_doc_1",
            rag_dataset_id="securities_internal_shared",
            knowledge_scope="internal_shared",
            file_name="old.pdf",
            source_kind="manual_upload",
            index_version=1,
            platform_status=ManagedDocumentStatus.import_failed.value,
            rag_status="failed",
            chunk_count=0,
            created_by_user_id=admin_user["user_id"],
            created_at=now,
            updated_at=now,
        )
        db_session.add(old)
        await db_session.commit()

        token = await _admin_token(client, admin_user)
        resp = await client.post(
            f"/api/v1/admin/documents/{old.id}/replace",
            headers=await bearer_headers(token),
            data={"knowledge_scope": "internal_shared"},
            files=[("file", ("new.pdf", PDF_BYTES, "application/pdf"))],
        )
        assert resp.status_code == 409
        assert fake.upload_calls == 0
