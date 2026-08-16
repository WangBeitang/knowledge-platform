"""Stage 3 Chunk 管理集成测试（真实 DB + 进程内 FakeRag）。

覆盖（§五十四）：分页（>100）、Adapter 字段映射、禁用/恢复、
版本冲突 INDEX_VERSION_CONFLICT、无正文编辑接口。
"""

import httpx
import pytest

from app.core.enums import ManagedDocumentStatus
from app.core.time import utc_now_naive
from app.models.managed_document import ManagedDocument
from app.rag.rag_document_client import RagDocumentClient
from app.repositories.audit_log_repository import AuditLogRepository
from app.repositories.managed_document_repository import ManagedDocumentRepository
from app.services.audit_service import AuditService
from app.services.chunk_service import ChunkService
from tests.integration.conftest import api_login, bearer_headers
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
    import app.api.v1.chunks as chunks_mod

    doc_client = RagDocumentClient(
        base_url="http://rag", transport=httpx.MockTransport(fake.handler)
    )
    _track(doc_client)
    service = ChunkService(
        docs=ManagedDocumentRepository(session),
        audit=AuditService(AuditLogRepository(session)),
        document_client=doc_client,
    )
    monkeypatch.setattr(chunks_mod, "_chunk_service", lambda s: service)


async def _seed_active_document(
    session,
    *,
    created_by_user_id: str,
    rag_document_id: str = "doc_chunks_1",
    index_version: int = 2,
    chunk_count: int = 0,
) -> ManagedDocument:
    now = utc_now_naive()
    doc = ManagedDocument(
        rag_document_id=rag_document_id,
        rag_dataset_id="securities_internal_shared",
        knowledge_scope="internal_shared",
        file_name="chunks.pdf",
        source_kind="manual_upload",
        index_version=index_version,
        platform_status=ManagedDocumentStatus.active.value,
        rag_status="completed",
        chunk_count=chunk_count,
        created_by_user_id=created_by_user_id,
        created_at=now,
        updated_at=now,
    )
    session.add(doc)
    await session.flush()
    return doc


async def _admin_token(client, admin_user) -> str:
    resp = await api_login(client, admin_user["username"], admin_user["password"])
    return resp.json()["data"]["access_token"]


class TestChunkPagination:
    async def test_page_2_beyond_100(self, client, admin_user, db_session, monkeypatch):
        fake = FakeRag()
        fake.seed_document(
            "doc_chunks_1",
            dataset_id="securities_internal_shared",
            visibility="shared",
            index_version=2,
            chunk_count=150,
        )
        fake.seed_chunks("doc_chunks_1", 150)
        _install(fake, db_session, monkeypatch)
        doc = await _seed_active_document(
            db_session, created_by_user_id=admin_user["user_id"], chunk_count=150
        )
        await db_session.commit()

        token = await _admin_token(client, admin_user)
        page1 = await client.get(
            f"/api/v1/admin/documents/{doc.id}/chunks?page=1&page_size=100",
            headers=await bearer_headers(token),
        )
        assert page1.status_code == 200
        d1 = page1.json()["data"]
        assert [i["position"] for i in d1["items"]] == list(range(0, 100))
        assert d1["total"] == 150

        page2 = await client.get(
            f"/api/v1/admin/documents/{doc.id}/chunks?page=2&page_size=100",
            headers=await bearer_headers(token),
        )
        assert page2.status_code == 200
        d2 = page2.json()["data"]
        assert [i["position"] for i in d2["items"]] == list(range(100, 150))
        assert d2["items"][0]["position"] == 100  # 不是重复 0~99


class TestChunkAdapter:
    async def test_field_mapping(self, client, admin_user, db_session, monkeypatch):
        fake = FakeRag()
        fake.seed_document(
            "doc_chunks_1",
            dataset_id="securities_internal_shared",
            visibility="shared",
            index_version=2,
            chunk_count=3,
        )
        fake.seed_chunks("doc_chunks_1", 3)
        # 模拟一个禁用 chunk（含 latest_event 原因）
        fake.chunks[("doc_chunks_1", 1)]["effective_enabled"] = False
        fake.chunks[("doc_chunks_1", 1)]["manual_status"] = "disabled"
        fake.chunks[("doc_chunks_1", 1)]["latest_event"] = {
            "reason_type": "outdated_content",
            "reason_detail": "旧版制度内容",
        }
        _install(fake, db_session, monkeypatch)
        doc = await _seed_active_document(
            db_session, created_by_user_id=admin_user["user_id"], chunk_count=3
        )
        await db_session.commit()

        token = await _admin_token(client, admin_user)
        resp = await client.get(
            f"/api/v1/admin/documents/{doc.id}/chunks?page=1&page_size=20",
            headers=await bearer_headers(token),
        )
        items = resp.json()["data"]["items"]
        chunk1 = next(i for i in items if i["position"] == 1)
        # position ← chunk_index；enabled ← effective_enabled；reason ← latest_event
        assert chunk1["enabled"] is False
        assert chunk1["disabled_reason_code"] == "outdated_content"
        assert chunk1["disabled_reason_text"] == "旧版制度内容"
        assert chunk1["text"]  # content_preview
        assert chunk1["document_id"] == doc.id
        # 安全 metadata 只含白名单
        assert "owner_user_id" not in chunk1["metadata"]

    async def test_chunk_detail_returns_full_text(
        self, client, admin_user, db_session, monkeypatch
    ):
        fake = FakeRag()
        fake.seed_document(
            "doc_chunks_1",
            dataset_id="securities_internal_shared",
            visibility="shared",
            index_version=2,
            chunk_count=3,
        )
        fake.seed_chunks("doc_chunks_1", 3)
        _install(fake, db_session, monkeypatch)
        doc = await _seed_active_document(
            db_session, created_by_user_id=admin_user["user_id"], chunk_count=3
        )
        await db_session.commit()

        token = await _admin_token(client, admin_user)
        chunk_id = fake.chunks[("doc_chunks_1", 0)]["chunk_id"]
        resp = await client.get(
            f"/api/v1/admin/documents/{doc.id}/chunks/{chunk_id}",
            headers=await bearer_headers(token),
        )
        assert resp.status_code == 200
        item = resp.json()["data"]
        assert "第 0 段正文内容" in item["text"]  # 详情返回全文


class TestChunkEnable:
    async def test_disable_and_restore(self, client, admin_user, db_session, monkeypatch):
        fake = FakeRag()
        fake.seed_document(
            "doc_chunks_1",
            dataset_id="securities_internal_shared",
            visibility="shared",
            index_version=2,
            chunk_count=5,
        )
        fake.seed_chunks("doc_chunks_1", 5)
        _install(fake, db_session, monkeypatch)
        doc = await _seed_active_document(
            db_session, created_by_user_id=admin_user["user_id"], chunk_count=5
        )
        await db_session.commit()

        token = await _admin_token(client, admin_user)
        chunk_id = fake.chunks[("doc_chunks_1", 2)]["chunk_id"]
        # 禁用
        resp = await client.patch(
            f"/api/v1/admin/documents/{doc.id}/chunks/{chunk_id}/enabled",
            headers=await bearer_headers(token),
            json={
                "enabled": False,
                "reason_code": "outdated_content",
                "reason_text": "旧版制度内容",
                "expected_index_version": 2,
            },
        )
        assert resp.status_code == 200
        assert resp.json()["data"]["enabled"] is False
        assert fake.chunks[("doc_chunks_1", 2)]["effective_enabled"] is False

        # 恢复
        resp = await client.patch(
            f"/api/v1/admin/documents/{doc.id}/chunks/{chunk_id}/enabled",
            headers=await bearer_headers(token),
            json={
                "enabled": True,
                "reason_code": "manual_restore",
                "reason_text": "人工恢复",
                "expected_index_version": 2,
            },
        )
        assert resp.status_code == 200
        assert resp.json()["data"]["enabled"] is True
        assert fake.chunks[("doc_chunks_1", 2)]["effective_enabled"] is True

    async def test_version_conflict(self, client, admin_user, db_session, monkeypatch):
        fake = FakeRag()
        fake.seed_document(
            "doc_chunks_1",
            dataset_id="securities_internal_shared",
            visibility="shared",
            index_version=2,
            chunk_count=3,
        )
        fake.seed_chunks("doc_chunks_1", 3)
        _install(fake, db_session, monkeypatch)
        doc = await _seed_active_document(
            db_session, created_by_user_id=admin_user["user_id"], index_version=2, chunk_count=3
        )
        await db_session.commit()

        token = await _admin_token(client, admin_user)
        chunk_id = fake.chunks[("doc_chunks_1", 0)]["chunk_id"]
        resp = await client.patch(
            f"/api/v1/admin/documents/{doc.id}/chunks/{chunk_id}/enabled",
            headers=await bearer_headers(token),
            json={
                "enabled": False,
                "reason_code": "other",
                "reason_text": "人工禁用",
                "expected_index_version": 1,  # 故意传旧版本
            },
        )
        assert resp.status_code == 409
        assert resp.json()["error"]["code"] == "INDEX_VERSION_CONFLICT"
        assert resp.json()["error"]["retryable"] is False

    async def test_other_reason_requires_text(self, client, admin_user, db_session, monkeypatch):
        fake = FakeRag()
        fake.seed_document(
            "doc_chunks_1",
            dataset_id="securities_internal_shared",
            visibility="shared",
            index_version=2,
            chunk_count=3,
        )
        fake.seed_chunks("doc_chunks_1", 3)
        _install(fake, db_session, monkeypatch)
        doc = await _seed_active_document(
            db_session, created_by_user_id=admin_user["user_id"], chunk_count=3
        )
        await db_session.commit()

        token = await _admin_token(client, admin_user)
        chunk_id = fake.chunks[("doc_chunks_1", 0)]["chunk_id"]
        resp = await client.patch(
            f"/api/v1/admin/documents/{doc.id}/chunks/{chunk_id}/enabled",
            headers=await bearer_headers(token),
            json={
                "enabled": False,
                "reason_code": "other",
                "reason_text": "",
                "expected_index_version": 2,
            },
        )
        assert resp.status_code == 400

    async def test_no_text_edit_endpoint(self):
        from app.main import app

        # 平台不存在 Chunk 正文编辑接口（PATCH text / update_text）
        for route in app.routes:
            path = getattr(route, "path", "")
            methods = getattr(route, "methods", None) or set()
            if "chunks" in path and any(m in methods for m in {"PATCH", "PUT"}):
                assert not path.endswith(("/text", "/content")), f"发现正文编辑接口: {path}"
