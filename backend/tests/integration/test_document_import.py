"""Stage 3 文档导入集成测试（真实 DB + 进程内 FakeRag）。"""

import httpx
import pytest

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

PDF_BYTES = b"%PDF-1.4 fake pdf content for test"
EXE_BYTES = b"MZ fake exe content"


def _build_service(fake: FakeRag, session) -> DocumentService:
    client = RagImportClient(base_url="http://rag", transport=httpx.MockTransport(fake.handler))
    doc_client = RagDocumentClient(
        base_url="http://rag", transport=httpx.MockTransport(fake.handler)
    )
    _track(client)
    _track(doc_client)
    return DocumentService(
        docs=ManagedDocumentRepository(session),
        tasks=RagIntegrationTaskRepository(session),
        replacements=DocumentReplacementRepository(session),
        audit=AuditService(AuditLogRepository(session)),
        import_client=client,
        document_client=doc_client,
    )


def _build_task_service(fake: FakeRag, session) -> TaskService:
    client = RagImportClient(base_url="http://rag", transport=httpx.MockTransport(fake.handler))
    doc_client = RagDocumentClient(
        base_url="http://rag", transport=httpx.MockTransport(fake.handler)
    )
    _track(client)
    _track(doc_client)
    return TaskService(
        tasks=RagIntegrationTaskRepository(session),
        docs=ManagedDocumentRepository(session),
        replacements=DocumentReplacementRepository(session),
        import_client=client,
        document_client=doc_client,
    )


_test_clients = []


def _track(client) -> None:
    _test_clients.append(client)


@pytest.fixture(autouse=True)
async def _close_test_clients():
    yield
    while _test_clients:
        c = _test_clients.pop()
        await c.aclose()


@pytest.fixture
def rag_factory(monkeypatch):
    import app.api.v1.documents as documents_mod
    import app.api.v1.integration as integration_mod

    fakes: list[FakeRag] = []

    def install(fake: FakeRag, session):
        fakes.append(fake)
        doc_service = _build_service(fake, session)
        task_service = _build_task_service(fake, session)
        monkeypatch.setattr(documents_mod, "_document_service", lambda s: doc_service)
        monkeypatch.setattr(integration_mod, "_task_service", lambda s: task_service)
        return fake

    return install


async def _admin_token(client, admin_user) -> str:
    resp = await api_login(client, admin_user["username"], admin_user["password"])
    return resp.json()["data"]["access_token"]


class TestImport:
    async def test_single_pdf_creates_mappings(self, client, admin_user, db_session, rag_factory):
        fake = rag_factory(FakeRag(), db_session)
        token = await _admin_token(client, admin_user)
        resp = await client.post(
            "/api/v1/admin/documents/import",
            headers=await bearer_headers(token),
            data={"knowledge_scope": "internal_shared"},
            files=[("files", ("a.pdf", PDF_BYTES, "application/pdf"))],
        )
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["submitted_count"] == 1
        assert data["rejected_count"] == 0
        item = data["items"][0]
        assert item["status"] == "pending"
        assert item["document_id"]
        assert item["task_id"]

        # 上游请求参数正确：dataset = securities_internal_shared、visibility = shared
        doc_id = item["document_id"]
        from app.models.managed_document import ManagedDocument
        from app.models.rag_integration_task import RagIntegrationTask

        doc = await db_session.get(ManagedDocument, doc_id)
        assert doc is not None
        assert doc.rag_dataset_id == "securities_internal_shared"
        assert doc.knowledge_scope == "internal_shared"
        assert doc.file_name == "a.pdf"
        assert doc.platform_status == "importing"
        assert doc.rag_status == "pending"
        # latest_rag_task_id 是上游 rag_task_id；item.task_id 是平台 task id
        assert doc.latest_rag_task_id == next(iter(fake.tasks))

        # 任务映射创建正确
        from sqlalchemy import select

        tasks = list(
            (
                await db_session.scalars(
                    select(RagIntegrationTask).where(
                        RagIntegrationTask.managed_document_id == doc_id
                    )
                )
            ).all()
        )
        assert len(tasks) == 1
        assert tasks[0].operation == "document_import"
        assert tasks[0].status == "pending"
        # task.rag_task_id 是上游 id；item.task_id 是平台 task id
        assert tasks[0].id == item["task_id"]
        assert tasks[0].rag_task_id == next(iter(fake.tasks))
        assert tasks[0].rag_document_id == doc.rag_document_id

    @pytest.mark.parametrize(
        ("scope", "expected_dataset", "expected_visibility"),
        [
            ("external_public", "securities_external_public", "public"),
            ("internal_shared", "securities_internal_shared", "shared"),
            ("admin_private", "securities_admin_private", "private"),
        ],
    )
    async def test_scope_visibility_mapping(
        self,
        client,
        admin_user,
        db_session,
        rag_factory,
        scope,
        expected_dataset,
        expected_visibility,
    ):
        fake = rag_factory(FakeRag(), db_session)
        token = await _admin_token(client, admin_user)
        resp = await client.post(
            "/api/v1/admin/documents/import",
            headers=await bearer_headers(token),
            data={"knowledge_scope": scope},
            files=[("files", ("doc.pdf", PDF_BYTES, "application/pdf"))],
        )
        assert resp.status_code == 200
        assert resp.json()["data"]["submitted_count"] == 1
        # 上游文档已按 scope 创建
        uploaded = list(fake.documents.values())
        assert len(uploaded) == 1
        assert uploaded[0]["dataset_id"] == expected_dataset
        assert uploaded[0]["visibility"] == expected_visibility
        assert uploaded[0]["owner_user_id"] == "svc_knowledge_admin"

    async def test_multi_file_partial_rejected(self, client, admin_user, db_session, rag_factory):
        fake = rag_factory(FakeRag(), db_session)
        token = await _admin_token(client, admin_user)
        resp = await client.post(
            "/api/v1/admin/documents/import",
            headers=await bearer_headers(token),
            data={"knowledge_scope": "internal_shared"},
            files=[
                ("files", ("a.pdf", PDF_BYTES, "application/pdf")),
                ("files", ("b.exe", EXE_BYTES, "application/octet-stream")),
                ("files", ("c.pdf", PDF_BYTES, "application/pdf")),
            ],
        )
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["submitted_count"] == 2
        assert data["rejected_count"] == 1
        by_name = {i["file_name"]: i for i in data["items"]}
        assert by_name["a.pdf"]["status"] == "pending"
        assert by_name["c.pdf"]["status"] == "pending"
        assert by_name["b.exe"]["status"] == "rejected"
        assert by_name["b.exe"]["error"]["code"] == "UNSUPPORTED_FILE_TYPE"
        # 只调用上游 2 次，成功文件不回滚
        assert fake.upload_calls == 2

    async def test_non_pdf_does_not_call_upstream(
        self, client, admin_user, db_session, rag_factory
    ):
        fake = rag_factory(FakeRag(), db_session)
        token = await _admin_token(client, admin_user)
        resp = await client.post(
            "/api/v1/admin/documents/import",
            headers=await bearer_headers(token),
            data={"knowledge_scope": "internal_shared"},
            files=[("files", ("bad.exe", EXE_BYTES, "application/octet-stream"))],
        )
        assert resp.status_code == 200
        assert resp.json()["data"]["rejected_count"] == 1
        assert fake.upload_calls == 0  # 非 PDF 绝不调用原 RAG

    async def test_unsafe_filename_keeps_basename(
        self, client, admin_user, db_session, rag_factory
    ):
        rag_factory(FakeRag(), db_session)
        token = await _admin_token(client, admin_user)
        resp = await client.post(
            "/api/v1/admin/documents/import",
            headers=await bearer_headers(token),
            data={"knowledge_scope": "internal_shared"},
            files=[("files", ("..\\..\\evil.pdf", PDF_BYTES, "application/pdf"))],
        )
        assert resp.status_code == 200
        item = resp.json()["data"]["items"][0]
        assert item["file_name"] == "evil.pdf"  # 只保留安全 basename

    async def test_invalid_scope_bad_request(self, client, admin_user):
        token = await _admin_token(client, admin_user)
        resp = await client.post(
            "/api/v1/admin/documents/import",
            headers=await bearer_headers(token),
            data={"knowledge_scope": "no_such_scope"},
            files=[("files", ("a.pdf", PDF_BYTES, "application/pdf"))],
        )
        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == "INVALID_REQUEST"

    async def test_employee_forbidden(self, client, admin_user, tracked_users):
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
        resp = await client.post(
            "/api/v1/admin/documents/import",
            headers=await bearer_headers(emp_token),
            data={"knowledge_scope": "internal_shared"},
            files=[("files", ("a.pdf", PDF_BYTES, "application/pdf"))],
        )
        assert resp.status_code == 403


class TestTaskPolling:
    """导入任务轮询：真实上游状态映射 + 终态刷新文档快照（§五十二）。"""

    async def test_full_lifecycle_pending_running_succeeded(
        self, client, admin_user, db_session, rag_factory
    ):
        fake = rag_factory(FakeRag(), db_session)
        token = await _admin_token(client, admin_user)
        resp = await client.post(
            "/api/v1/admin/documents/import",
            headers=await bearer_headers(token),
            data={"knowledge_scope": "internal_shared"},
            files=[("files", ("a.pdf", PDF_BYTES, "application/pdf"))],
        )
        task_id = resp.json()["data"]["items"][0]["task_id"]
        rag_task_id = next(iter(fake.tasks))

        # pending
        view = await _poll(client, token, task_id)
        assert view["status"] == "pending"

        # processing → running
        fake.set_task_status(rag_task_id, "processing", running=[{"name": "parse_pdf"}])
        view = await _poll(client, token, task_id)
        assert view["status"] == "running"
        assert len(view["running_nodes"]) == 1

        # completed → succeeded，且刷新真实文档快照
        rag_doc_id = fake.tasks[rag_task_id]["document_id"]
        fake.set_task_status(
            rag_task_id, "completed", done=[{"name": "upload_file"}, {"name": "index"}]
        )
        fake.documents[rag_doc_id]["status"] = "completed"
        fake.documents[rag_doc_id]["chunk_count"] = 42
        fake.documents[rag_doc_id]["index_version"] = 3
        view = await _poll(client, token, task_id)
        assert view["status"] == "succeeded"
        # completed 必须用本轮节点（done=本轮响应、running 清空），不沿用上一轮 running 缓存
        assert [n["name"] for n in view["done_nodes"]] == ["upload_file", "index"]
        assert view["running_nodes"] == []

        await db_session.commit()  # 提交 TaskService 对 doc 的刷新
        from sqlalchemy import select

        from app.models.managed_document import ManagedDocument

        doc = await db_session.scalar(
            select(ManagedDocument).where(ManagedDocument.latest_rag_task_id == rag_task_id)
        )
        assert doc.platform_status == "active"
        assert doc.chunk_count == 42
        assert doc.index_version == 3

    async def test_unknown_status_not_succeeded(self, client, admin_user, db_session, rag_factory):
        fake = rag_factory(FakeRag(), db_session)
        token = await _admin_token(client, admin_user)
        resp = await client.post(
            "/api/v1/admin/documents/import",
            headers=await bearer_headers(token),
            data={"knowledge_scope": "internal_shared"},
            files=[("files", ("a.pdf", PDF_BYTES, "application/pdf"))],
        )
        task_id = resp.json()["data"]["items"][0]["task_id"]
        fake.unknown_task_status = "weird_state"
        view = await _poll(client, token, task_id)
        assert view["status"] == "running"  # 未知状态绝不映射 succeeded
        assert view["rag_status"] == "weird_state"

    async def test_terminal_task_not_overridden_by_upstream_drift(
        self, client, admin_user, db_session, rag_factory
    ):
        """任务二：终态后即使上游状态漂移，平台仍返回持久化终态，不再刷新上游。"""
        fake = rag_factory(FakeRag(), db_session)
        token = await _admin_token(client, admin_user)
        resp = await client.post(
            "/api/v1/admin/documents/import",
            headers=await bearer_headers(token),
            data={"knowledge_scope": "internal_shared"},
            files=[("files", ("a.pdf", PDF_BYTES, "application/pdf"))],
        )
        task_id = resp.json()["data"]["items"][0]["task_id"]
        rag_task_id = next(iter(fake.tasks))

        fake.set_task_status(rag_task_id, "completed", done=[{"name": "upload_file"}])
        view = await _poll(client, token, task_id)
        assert view["status"] == "succeeded"

        # 上游漂移回 processing：终态短路，不得重新覆盖为 running/succeeded
        fake.set_task_status(rag_task_id, "processing", running=[{"name": "parse_pdf"}])
        for _ in range(2):
            view = await _poll(client, token, task_id)
        assert view["status"] == "succeeded"
        assert view["running_nodes"] == []
        assert [n["name"] for n in view["done_nodes"]] == ["upload_file"]

    async def test_failed_sets_import_failed(self, client, admin_user, db_session, rag_factory):
        fake = rag_factory(FakeRag(), db_session)
        token = await _admin_token(client, admin_user)
        resp = await client.post(
            "/api/v1/admin/documents/import",
            headers=await bearer_headers(token),
            data={"knowledge_scope": "internal_shared"},
            files=[("files", ("a.pdf", PDF_BYTES, "application/pdf"))],
        )
        task_id = resp.json()["data"]["items"][0]["task_id"]
        rag_task_id = next(iter(fake.tasks))
        fake.set_task_status(
            rag_task_id,
            "failed",
            failed_node="parse_pdf",
            error_code="PARSE_FAILED",
            error_message="PDF 解析失败",
        )
        view = await _poll(client, token, task_id)
        assert view["status"] == "failed"
        assert view["failed_node"] == "parse_pdf"

        await db_session.commit()  # 提交 TaskService 对 doc 的刷新
        from sqlalchemy import select

        from app.models.managed_document import ManagedDocument

        doc = await db_session.scalar(
            select(ManagedDocument).where(ManagedDocument.latest_rag_task_id == rag_task_id)
        )
        assert doc.platform_status == "import_failed"  # 导入失败不标 active


async def _poll(client, token, task_id) -> dict:
    resp = await client.get(
        f"/api/v1/admin/integration/tasks/{task_id}",
        headers=await bearer_headers(token),
    )
    assert resp.status_code == 200
    return resp.json()["data"]
