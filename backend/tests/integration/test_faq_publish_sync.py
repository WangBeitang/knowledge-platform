"""Stage 5 Batch 1：FAQ 完整闭环集成测试（真实 DB/Redis + 进程内 FakeRag）。

覆盖验收重点：
- qa_access_logs → FAQ candidates（analyze 聚合）；
- 重复 analyze 去重正确（覆盖式，不翻倍、不重复建行）；
- 已发布标准问题不重复创建候选；
- candidate 审核（reject/publish）→ 正式 FAQ；员工访问 admin 接口 403；
- 三档 scope 权限正确、各自独立 FAQ 文档（不合成一份）；
- 发布后 MySQL 可查、Redis 精确命中、Redis flush 后从 MySQL 恢复；
- 同 scope + content_hash 幂等（不重复上传）；
- 新文档上传失败保留旧版；新文档成功后删除旧版；
- 旧版删除失败可 retry 且不重新上传新版；
- 并发轮询/重试不重复删除旧文档；
- unpublish 后缓存失效；审计记录写入。
"""

import asyncio
import uuid

import httpx
import pytest
from sqlalchemy import select

from app.core.enums import FaqStatus
from app.core.normalizer import normalize_question, question_hash
from app.core.redis import get_redis
from app.core.time import utc_now_naive
from app.models.faq import Faq
from app.rag.rag_document_client import RagDocumentClient
from app.rag.rag_import_client import RagImportClient
from app.repositories.audit_log_repository import AuditLogRepository
from app.repositories.faq_candidate_repository import FaqCandidateRepository
from app.repositories.faq_repository import FaqRepository
from app.repositories.faq_sync_run_repository import FaqSyncRunRepository
from app.services.audit_service import AuditService
from app.services.faq_service import FaqService, faq_cache_key
from app.services.faq_sync_service import FaqSyncService
from tests.integration.conftest import _unique, api_login, bearer_headers
from tests.integration.fake_rag_server import FakeRag

_test_clients = []


def _make_clients(fake: FakeRag):
    import_client = RagImportClient(
        base_url="http://rag", transport=httpx.MockTransport(fake.handler)
    )
    document_client = RagDocumentClient(
        base_url="http://rag", transport=httpx.MockTransport(fake.handler)
    )
    _test_clients.append(import_client)
    _test_clients.append(document_client)
    return import_client, document_client


@pytest.fixture(autouse=True)
async def _close_test_clients():
    yield
    while _test_clients:
        c = _test_clients.pop()
        await c.aclose()


@pytest.fixture
def faq_rag_factory(monkeypatch):
    """注入 FakeRag 到 FAQ 路由的服务构造（_faq_service / _sync_service）。

    服务构造使用「请求级 session」而不是测试 db_session：
    写操作随请求提交，测试 db_session 查询前需显式 commit 以刷新快照。
    """
    import app.api.v1.faq_candidates as fc_mod
    import app.api.v1.faq_sync_runs as sr_mod
    import app.api.v1.faqs as faqs_mod

    def install(fake: FakeRag, session):
        import_client, document_client = _make_clients(fake)

        def build_sync(s):
            return FaqSyncService(
                runs=FaqSyncRunRepository(s),
                faqs=FaqRepository(s),
                import_client=import_client,
                document_client=document_client,
            )

        def build_faq(s):
            return FaqService(
                repository=FaqRepository(s),
                candidates=FaqCandidateRepository(s),
                audit=AuditService(AuditLogRepository(s)),
                sync_service=build_sync(s),
            )

        monkeypatch.setattr(fc_mod, "_faq_service", build_faq)
        monkeypatch.setattr(faqs_mod, "_faq_service", build_faq)
        monkeypatch.setattr(faqs_mod, "_sync_service", build_sync)
        monkeypatch.setattr(sr_mod, "_sync_service", build_sync)
        return fake

    return install


@pytest.fixture
async def faq_cleanup(db_session):
    """按 scope 清理测试产生的 faq_sync_runs / faqs / faq_candidates / Redis key。"""
    scopes: list[str] = []
    hashes: list[tuple[str, str]] = []

    def track(scope: str, normalized_question_hash: str) -> None:
        if scope not in scopes:
            scopes.append(scope)
        hashes.append((scope, normalized_question_hash))

    yield track

    from sqlalchemy import delete, select

    from app.models.faq import Faq
    from app.models.faq_candidate import FaqCandidate
    from app.models.faq_sync_run import FaqSyncRun

    # 结束旧事务，开新快照（请求级写已提交）
    await db_session.commit()

    for scope in scopes:
        await db_session.execute(delete(FaqSyncRun).where(FaqSyncRun.knowledge_scope == scope))
        faqs = list(
            (await db_session.scalars(select(Faq).where(Faq.knowledge_scope == scope))).all()
        )
        for faq in faqs:
            await db_session.execute(delete(Faq).where(Faq.id == faq.id))
            if faq.source_candidate_id:
                await db_session.execute(
                    delete(FaqCandidate).where(FaqCandidate.id == faq.source_candidate_id)
                )
        await db_session.execute(delete(FaqCandidate).where(FaqCandidate.knowledge_scope == scope))
    await db_session.commit()

    redis = await get_redis()
    if redis is not None:
        for scope, h in hashes:
            try:
                await redis.delete(faq_cache_key(scope, h))
            except Exception:  # noqa: BLE001
                pass


async def _admin_token(client, admin_user) -> str:
    resp = await api_login(client, admin_user["username"], admin_user["password"])
    return resp.json()["data"]["access_token"]


async def _seed_logs(
    db_session,
    *,
    admin_user_id: str,
    question: str,
    ask_count: int,
    allowed_scopes: list[str],
) -> str:
    """插入 ask_count 条相同归一化问题的 qa_access_logs，返回 normalized_question_hash。"""
    from app.models.chat_session import ChatSession
    from app.models.qa_access_log import QaAccessLog

    normalized = normalize_question(question)
    h = question_hash(normalized)
    session = ChatSession(
        channel="internal_web",
        user_id=admin_user_id,
        title="FAQ 测试会话",
        status="active",
        last_message_at=utc_now_naive(),
    )
    db_session.add(session)
    await db_session.flush()
    for i in range(ask_count):
        log = QaAccessLog(
            turn_id=str(uuid.uuid4()),
            session_id=session.id,
            channel="internal_web",
            user_id=admin_user_id,
            external_subject_hash=None,
            question=f"{question}（第 {i} 次）",
            normalized_question=normalized,
            normalized_question_hash=h,
            allowed_scopes_json=allowed_scopes,
            answer_source="rag",
            faq_id=None,
            rag_trace_id=None,
            terminal_reason_code=None,
            citation_count=0,
            citation_document_ids_json=[],
            input_tokens=None,
            output_tokens=None,
            total_tokens=None,
            latency_ms=10,
            status="succeeded",
            error_code=None,
            created_at=utc_now_naive(),
        )
        db_session.add(log)
    await db_session.commit()
    return h


async def _publish_faq(client, token, *, scope, question, answer) -> dict:
    resp = await client.post(
        "/api/v1/admin/faqs",
        headers=await bearer_headers(token),
        json={"knowledge_scope": scope, "question": question, "answer": answer},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["data"]


def _scope_docs(fake: FakeRag, scope: str) -> list[dict]:
    expected_dataset = {
        "external_public": "securities_external_public",
        "internal_shared": "securities_internal_shared",
        "admin_private": "securities_admin_private",
    }[scope]
    return [d for d in fake.documents.values() if d["dataset_id"] == expected_dataset]


class TestAnalyze:
    async def test_analyze_generates_candidates(self, client, admin_user, db_session, faq_cleanup):
        token = await _admin_token(client, admin_user)
        # 唯一问题文本：避免与真实开发库历史日志碰撞（全量聚合按 hash 计数）
        tag = uuid.uuid4().hex[:8]
        q1 = f"如何办理风险测评{tag}？"
        q2 = f"忘记密码怎么办{tag}？"
        h1 = await _seed_logs(
            db_session,
            admin_user_id=admin_user["user_id"],
            question=q1,
            ask_count=3,
            allowed_scopes=["internal_shared", "external_public"],
        )
        h2 = await _seed_logs(
            db_session,
            admin_user_id=admin_user["user_id"],
            question=q2,
            ask_count=2,
            allowed_scopes=["admin_private", "internal_shared", "external_public"],
        )
        faq_cleanup("internal_shared", h1)
        faq_cleanup("admin_private", h2)

        resp = await client.post(
            "/api/v1/admin/faq-candidates/analyze",
            headers=await bearer_headers(token),
            json={},
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()["data"]
        # 真实库可能有历史日志，断言相对性：本次新增 ≥ 2 组
        assert data["created"] >= 2
        assert data["skipped_published"] >= 0

        lst = await client.get(
            "/api/v1/admin/faq-candidates?page_size=50",
            headers=await bearer_headers(token),
        )
        items = lst.json()["data"]["items"]
        by_hash = {i["normalized_question_hash"]: i for i in items}
        assert by_hash[h1]["ask_count"] == 3
        assert by_hash[h1]["knowledge_scope"] == "internal_shared"
        assert by_hash[h1]["status"] == "pending_review"
        assert len(by_hash[h1]["sample_questions"]) == 3
        assert by_hash[h2]["ask_count"] == 2
        assert by_hash[h2]["knowledge_scope"] == "admin_private"

    async def test_repeat_analyze_dedup(self, client, admin_user, db_session, faq_cleanup):
        token = await _admin_token(client, admin_user)
        tag = uuid.uuid4().hex[:8]
        h = await _seed_logs(
            db_session,
            admin_user_id=admin_user["user_id"],
            question=f"重复分析问题{tag}？",
            ask_count=3,
            allowed_scopes=["internal_shared", "external_public"],
        )
        faq_cleanup("internal_shared", h)

        for _ in range(2):
            resp = await client.post(
                "/api/v1/admin/faq-candidates/analyze",
                headers=await bearer_headers(token),
                json={},
            )
            assert resp.status_code == 200

        lst = await client.get(
            "/api/v1/admin/faq-candidates?page_size=50",
            headers=await bearer_headers(token),
        )
        items = [i for i in lst.json()["data"]["items"] if i["normalized_question_hash"] == h]
        # 去重：同 hash 只有一条候选，ask_count 不因重复 analyze 翻倍
        assert len(items) == 1
        assert items[0]["ask_count"] == 3

    async def test_published_question_skipped(
        self, client, admin_user, db_session, faq_cleanup, faq_rag_factory
    ):
        faq_rag_factory(FakeRag(), db_session)
        token = await _admin_token(client, admin_user)
        tag = uuid.uuid4().hex[:8]
        question = f"已有标准答案的问题{tag}？"
        faq = await _publish_faq(
            client, token, scope="internal_shared", question=question, answer="标准答案"
        )
        faq_cleanup("internal_shared", faq["normalized_question_hash"])
        await _seed_logs(
            db_session,
            admin_user_id=admin_user["user_id"],
            question=question,
            ask_count=2,
            allowed_scopes=["internal_shared", "external_public"],
        )
        resp = await client.post(
            "/api/v1/admin/faq-candidates/analyze",
            headers=await bearer_headers(token),
            json={},
        )
        data = resp.json()["data"]
        # 已发布标准问题：该 hash 不生成候选（历史候选可能使 skipped_published 更大）
        assert data["skipped_published"] >= 1
        lst = await client.get(
            "/api/v1/admin/faq-candidates?page_size=50",
            headers=await bearer_headers(token),
        )
        assert not [
            i
            for i in lst.json()["data"]["items"]
            if i["normalized_question_hash"] == faq["normalized_question_hash"]
        ]


class TestCandidateReview:
    async def test_reject_candidate(self, client, admin_user, db_session, faq_cleanup):
        token = await _admin_token(client, admin_user)
        tag = uuid.uuid4().hex[:8]
        h = await _seed_logs(
            db_session,
            admin_user_id=admin_user["user_id"],
            question=f"应该拒绝的问题{tag}",
            ask_count=1,
            allowed_scopes=["internal_shared", "external_public"],
        )
        faq_cleanup("internal_shared", h)
        await client.post(
            "/api/v1/admin/faq-candidates/analyze",
            headers=await bearer_headers(token),
            json={},
        )
        lst = await client.get(
            "/api/v1/admin/faq-candidates?page_size=50",
            headers=await bearer_headers(token),
        )
        cid = next(
            i["id"] for i in lst.json()["data"]["items"] if i["normalized_question_hash"] == h
        )
        resp = await client.post(
            f"/api/v1/admin/faq-candidates/{cid}/reject",
            headers=await bearer_headers(token),
        )
        assert resp.status_code == 200
        assert resp.json()["data"]["status"] == "rejected"
        # 重复拒绝 409
        resp2 = await client.post(
            f"/api/v1/admin/faq-candidates/{cid}/reject",
            headers=await bearer_headers(token),
        )
        assert resp2.status_code == 409

    async def test_publish_candidate_creates_faq(
        self, client, admin_user, db_session, faq_cleanup, faq_rag_factory
    ):
        faq_rag_factory(FakeRag(), db_session)
        token = await _admin_token(client, admin_user)
        tag = uuid.uuid4().hex[:8]
        question = f"候选审核发布问题{tag}？"
        h = await _seed_logs(
            db_session,
            admin_user_id=admin_user["user_id"],
            question=question,
            ask_count=2,
            allowed_scopes=["internal_shared", "external_public"],
        )
        faq_cleanup("internal_shared", h)
        await client.post(
            "/api/v1/admin/faq-candidates/analyze",
            headers=await bearer_headers(token),
            json={},
        )
        lst = await client.get(
            "/api/v1/admin/faq-candidates?page_size=50",
            headers=await bearer_headers(token),
        )
        cid = next(
            i["id"] for i in lst.json()["data"]["items"] if i["normalized_question_hash"] == h
        )
        resp = await client.post(
            f"/api/v1/admin/faq-candidates/{cid}/publish",
            headers=await bearer_headers(token),
            json={
                "knowledge_scope": "internal_shared",
                "question": question,
                "answer": "审核通过的标准答案",
            },
        )
        assert resp.status_code == 200, resp.text
        faq = resp.json()["data"]
        assert faq["status"] == "published"
        assert faq["source_candidate_id"] == cid
        faq_cleanup("internal_shared", faq["normalized_question_hash"])

        # MySQL 可查（事实源）
        from sqlalchemy import select

        from app.models.faq import Faq

        await db_session.commit()  # 刷新快照看到请求已提交的写
        row = await db_session.scalar(select(Faq).where(Faq.id == faq["id"]))
        assert row is not None
        assert row.status == FaqStatus.published.value

        # 候选标记已发布
        from app.models.faq_candidate import FaqCandidate

        cand_row = await db_session.scalar(select(FaqCandidate).where(FaqCandidate.id == cid))
        assert cand_row.status == "published"
        assert cand_row.published_faq_id == faq["id"]

        # Redis 精确缓存写入
        redis = await get_redis()
        if redis is not None:
            raw = await redis.get(faq_cache_key("internal_shared", faq["normalized_question_hash"]))
            assert raw is not None
            import json

            assert json.loads(raw)["faq_id"] == faq["id"]

    async def test_employee_forbidden(self, client, admin_user, tracked_users):
        token = await _admin_token(client, admin_user)
        username = _unique("faq_emp")
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
        resp = await client.get(
            "/api/v1/admin/faq-candidates",
            headers=await bearer_headers(emp_token),
        )
        assert resp.status_code == 403


class TestFaqCrud:
    async def test_three_scopes_each_own_document(
        self, client, admin_user, db_session, faq_cleanup, faq_rag_factory
    ):
        fake = faq_rag_factory(FakeRag(), db_session)
        token = await _admin_token(client, admin_user)
        qa = [
            ("external_public", "外部问题一", "外部答案一"),
            ("internal_shared", "内部问题一", "内部答案一"),
            ("admin_private", "管理员问题一", "管理员答案一"),
        ]
        for scope, q, a in qa:
            faq = await _publish_faq(client, token, scope=scope, question=q, answer=a)
            faq_cleanup(scope, faq["normalized_question_hash"])

        # 三档 Dataset 各自恰有一份 FAQ 文档，文件名为 faq_<scope>.md
        for scope, _q, _a in qa:
            docs = _scope_docs(fake, scope)
            assert len(docs) == 1, f"scope {scope} 应恰有 1 份文档，实际 {len(docs)}"
            assert docs[0]["file_name"] == f"faq_{scope}.md"
        # 总上传 3 次（三份独立文档，不合成一份）
        assert fake.upload_calls == 3

    async def test_create_duplicate_conflict(
        self, client, admin_user, db_session, faq_cleanup, faq_rag_factory
    ):
        faq_rag_factory(FakeRag(), db_session)
        token = await _admin_token(client, admin_user)
        faq = await _publish_faq(
            client, token, scope="internal_shared", question="重复问题？", answer="答案"
        )
        faq_cleanup("internal_shared", faq["normalized_question_hash"])
        resp = await client.post(
            "/api/v1/admin/faqs",
            headers=await bearer_headers(token),
            json={
                "knowledge_scope": "internal_shared",
                "question": "重复问题？",
                "answer": "答案2",
            },
        )
        assert resp.status_code == 409

    async def test_update_recomputes_hash_and_cache(
        self, client, admin_user, db_session, faq_cleanup, faq_rag_factory
    ):
        fake = faq_rag_factory(FakeRag(), db_session)
        token = await _admin_token(client, admin_user)
        faq = await _publish_faq(
            client, token, scope="internal_shared", question="修改前问题？", answer="旧答案"
        )
        old_hash = faq["normalized_question_hash"]
        faq_cleanup("internal_shared", old_hash)

        redis = await get_redis()
        if redis is not None:
            assert await redis.get(faq_cache_key("internal_shared", old_hash)) is not None

        # 单进行中防分叉：publish 上传未完成前 PATCH 不启动第二次上传
        resp = await client.patch(
            f"/api/v1/admin/faqs/{faq['id']}",
            headers=await bearer_headers(token),
            json={"question": "修改后问题", "answer": "新答案"},
        )
        assert resp.status_code == 200, resp.text
        updated = resp.json()["data"]
        assert updated["normalized_question"] == normalize_question("修改后问题")
        new_hash = updated["normalized_question_hash"]
        assert new_hash != old_hash
        faq_cleanup("internal_shared", new_hash)

        if redis is not None:
            # 旧 key 失效、新 key 写入
            assert await redis.get(faq_cache_key("internal_shared", old_hash)) is None
            assert await redis.get(faq_cache_key("internal_shared", new_hash)) is not None
        assert fake.upload_calls == 1  # publish 上传未完成，不触发第二次上传

        # 完成第一轮上传后再次修改 → 触发该范围文档重建（第二次上传）
        rag_task_id = list(fake.tasks)[-1]  # 最新上传任务（publish 的 doc1）
        rag_doc_id = fake.tasks[rag_task_id]["document_id"]
        fake.set_task_status(rag_task_id, "completed", done=[{"name": "upload_file"}])
        fake.documents[rag_doc_id]["status"] = "completed"
        await client.get(
            "/api/v1/admin/faq-sync-runs",
            headers=await bearer_headers(token),
        )
        resp = await client.patch(
            f"/api/v1/admin/faqs/{faq['id']}",
            headers=await bearer_headers(token),
            json={"question": "修改后问题三", "answer": "答案三"},
        )
        assert resp.status_code == 200, resp.text
        h3 = resp.json()["data"]["normalized_question_hash"]
        faq_cleanup("internal_shared", h3)
        assert fake.upload_calls == 2

    async def test_unpublish_removes_cache_and_republish_restores(
        self, client, admin_user, db_session, faq_cleanup, faq_rag_factory
    ):
        faq_rag_factory(FakeRag(), db_session)
        token = await _admin_token(client, admin_user)
        faq = await _publish_faq(
            client, token, scope="internal_shared", question="上下线问题？", answer="答案"
        )
        h = faq["normalized_question_hash"]
        faq_cleanup("internal_shared", h)

        redis = await get_redis()
        if redis is not None:
            assert await redis.get(faq_cache_key("internal_shared", h)) is not None

        resp = await client.post(
            f"/api/v1/admin/faqs/{faq['id']}/unpublish",
            headers=await bearer_headers(token),
        )
        assert resp.status_code == 200
        assert resp.json()["data"]["status"] == "unpublished"
        if redis is not None:
            assert await redis.get(faq_cache_key("internal_shared", h)) is None

        # 下线后精确 lookup 不命中
        from app.repositories.faq_repository import FaqRepository

        await db_session.commit()  # 刷新快照看到请求已提交的写
        service = FaqService(repository=FaqRepository(db_session))
        hit = await service.lookup_exact_faq(
            scopes=["internal_shared"],
            normalized_question=normalize_question("上下线问题？"),
            normalized_question_hash=h,
        )
        assert hit is None

        resp2 = await client.post(
            f"/api/v1/admin/faqs/{faq['id']}/publish",
            headers=await bearer_headers(token),
        )
        assert resp2.status_code == 200
        assert resp2.json()["data"]["status"] == "published"
        if redis is not None:
            assert await redis.get(faq_cache_key("internal_shared", h)) is not None

    async def test_redis_flush_recovered_from_mysql(
        self, client, admin_user, db_session, faq_cleanup, faq_rag_factory
    ):
        faq_rag_factory(FakeRag(), db_session)
        token = await _admin_token(client, admin_user)
        faq = await _publish_faq(
            client, token, scope="internal_shared", question="缓存恢复问题？", answer="恢复答案"
        )
        h = faq["normalized_question_hash"]
        faq_cleanup("internal_shared", h)

        redis = await get_redis()
        if redis is None:
            pytest.skip("Redis 不可达，跳过缓存恢复断言")
        assert await redis.get(faq_cache_key("internal_shared", h)) is not None

        # 模拟 Redis 清空
        await redis.delete(faq_cache_key("internal_shared", h))
        assert await redis.get(faq_cache_key("internal_shared", h)) is None

        # MySQL 回填 Redis，行为不变
        await db_session.commit()  # 刷新快照看到请求已提交的写
        service = FaqService(repository=FaqRepository(db_session))
        hit = await service.lookup_exact_faq(
            scopes=["internal_shared"],
            normalized_question=normalize_question("缓存恢复问题？"),
            normalized_question_hash=h,
        )
        assert hit is not None
        assert hit.answer == "恢复答案"
        backfilled = await redis.get(faq_cache_key("internal_shared", h))
        assert backfilled is not None
        import json

        assert json.loads(backfilled)["faq_id"] == faq["id"]


class TestSyncStateMachine:
    async def _complete_upload(self, fake: FakeRag, run_scope: str | None = None) -> None:
        """把最新上传任务置为 completed，并确认文档快照可查。"""
        rag_task_id = list(fake.tasks)[-1]  # 最新任务（多次 upload 时取最后一次）
        rag_doc_id = fake.tasks[rag_task_id]["document_id"]
        fake.set_task_status(rag_task_id, "completed", done=[{"name": "upload_file"}])
        fake.documents[rag_doc_id]["status"] = "completed"

    async def test_sync_succeeded_and_content_hash_idempotent(
        self, client, admin_user, db_session, faq_cleanup, faq_rag_factory
    ):
        fake = faq_rag_factory(FakeRag(), db_session)
        token = await _admin_token(client, admin_user)
        faq = await _publish_faq(
            client, token, scope="internal_shared", question="同步成功问题？", answer="答案"
        )
        h = faq["normalized_question_hash"]
        faq_cleanup("internal_shared", h)

        # 初始 pending → 上游 completed → 查询 faq-sync-runs 刷新为 succeeded
        await self._complete_upload(fake)
        lst = await client.get(
            "/api/v1/admin/faq-sync-runs",
            headers=await bearer_headers(token),
        )
        runs = lst.json()["data"]["items"]
        assert len(runs) == 1
        assert runs[0]["status"] == "succeeded"
        assert runs[0]["rag_document_id"]
        assert runs[0]["previous_rag_document_id"] is None

        # 幂等：内容未变化再触发同步 → 复用 succeeded，不重复上传
        uploads_before = fake.upload_calls
        import_client, document_client = _make_clients(fake)
        await db_session.commit()  # 刷新快照看到请求已提交的 run
        svc = FaqSyncService(
            runs=FaqSyncRunRepository(db_session),
            faqs=FaqRepository(db_session),
            import_client=import_client,
            document_client=document_client,
        )
        run = await svc.submit_faq_sync(
            knowledge_scope="internal_shared", operator_user_id=admin_user["user_id"]
        )
        assert run.status == "succeeded"
        assert fake.upload_calls == uploads_before  # 未重复上传

    async def test_update_cleans_old_document_after_new_success(
        self, client, admin_user, db_session, faq_cleanup, faq_rag_factory
    ):
        fake = faq_rag_factory(FakeRag(), db_session)
        token = await _admin_token(client, admin_user)
        faq = await _publish_faq(
            client, token, scope="internal_shared", question="第一版问题", answer="第一版答案"
        )
        h1 = faq["normalized_question_hash"]
        faq_cleanup("internal_shared", h1)
        await self._complete_upload(fake)
        lst = await client.get(
            "/api/v1/admin/faq-sync-runs",
            headers=await bearer_headers(token),
        )
        run1 = lst.json()["data"]["items"][0]
        assert run1["status"] == "succeeded"
        old_doc_id = run1["rag_document_id"]

        # 修改 FAQ → 新文档上传成功 → 旧文档被删除，sync 成功
        resp = await client.patch(
            f"/api/v1/admin/faqs/{faq['id']}",
            headers=await bearer_headers(token),
            json={"question": "第二版问题", "answer": "第二版答案"},
        )
        assert resp.status_code == 200
        h2 = resp.json()["data"]["normalized_question_hash"]
        faq_cleanup("internal_shared", h2)

        await self._complete_upload(fake)
        lst = await client.get(
            "/api/v1/admin/faq-sync-runs",
            headers=await bearer_headers(token),
        )
        runs = lst.json()["data"]["items"]
        assert len(runs) == 2
        run2 = runs[0]
        assert run2["status"] == "succeeded"
        assert run2["previous_rag_document_id"] == old_doc_id
        assert run2["rag_document_id"] != old_doc_id
        # 旧文档已在上游删除
        assert fake.document(old_doc_id) is None or fake.document(old_doc_id)["status"] == "deleted"
        # 顺序正确：新文档成功后才删除旧文档（delete 恰好 1 次）
        assert fake.delete_calls == 1

    async def test_new_upload_failure_keeps_old_document(
        self, client, admin_user, db_session, faq_cleanup, faq_rag_factory
    ):
        fake = faq_rag_factory(FakeRag(), db_session)
        token = await _admin_token(client, admin_user)
        faq = await _publish_faq(
            client, token, scope="internal_shared", question="保留旧版问题", answer="旧答案"
        )
        h1 = faq["normalized_question_hash"]
        faq_cleanup("internal_shared", h1)
        await self._complete_upload(fake)
        lst = await client.get(
            "/api/v1/admin/faq-sync-runs",
            headers=await bearer_headers(token),
        )
        old_doc_id = lst.json()["data"]["items"][0]["rag_document_id"]

        # 修改 FAQ 后上游不可用 → 新文档上传失败 → 同步 failed，旧文档保留
        fake.fail_upload = True
        resp = await client.patch(
            f"/api/v1/admin/faqs/{faq['id']}",
            headers=await bearer_headers(token),
            json={"question": "第二版问题", "answer": "第二版答案"},
        )
        assert resp.status_code == 200
        h2 = resp.json()["data"]["normalized_question_hash"]
        faq_cleanup("internal_shared", h2)

        lst = await client.get(
            "/api/v1/admin/faq-sync-runs",
            headers=await bearer_headers(token),
        )
        run = lst.json()["data"]["items"][0]
        assert run["status"] == "failed"
        # 旧文档仍然存在（未删除）
        assert fake.document(old_doc_id) is not None
        assert fake.document(old_doc_id)["status"] != "deleted"
        # MySQL 正式 FAQ 不受影响（发布状态保持、精确命中仍在）
        from app.repositories.faq_repository import FaqRepository
        from app.services.faq_service import FaqService

        await db_session.commit()  # 刷新快照看到请求已提交的写
        service = FaqService(repository=FaqRepository(db_session))
        hit = await service.lookup_exact_faq(
            scopes=["internal_shared"],
            normalized_question=normalize_question("第二版问题"),
            normalized_question_hash=h2,
        )
        assert hit is not None

    async def test_cleanup_failure_retry_without_reupload(
        self, client, admin_user, db_session, faq_cleanup, faq_rag_factory
    ):
        fake = faq_rag_factory(FakeRag(), db_session)
        token = await _admin_token(client, admin_user)
        faq = await _publish_faq(
            client, token, scope="internal_shared", question="清理失败问题一", answer="答案一"
        )
        h1 = faq["normalized_question_hash"]
        faq_cleanup("internal_shared", h1)
        await self._complete_upload(fake)
        lst = await client.get(
            "/api/v1/admin/faq-sync-runs",
            headers=await bearer_headers(token),
        )
        old_doc_id = lst.json()["data"]["items"][0]["rag_document_id"]

        # 修改 → 新文档上传成功 → 旧文档删除失败（409）→ sync failed，新旧 ID 都保留
        resp = await client.patch(
            f"/api/v1/admin/faqs/{faq['id']}",
            headers=await bearer_headers(token),
            json={"question": "清理失败问题二", "answer": "答案二"},
        )
        h2 = resp.json()["data"]["normalized_question_hash"]
        faq_cleanup("internal_shared", h2)

        fake.fail_delete = True
        await self._complete_upload(fake)
        lst = await client.get(
            "/api/v1/admin/faq-sync-runs",
            headers=await bearer_headers(token),
        )
        run = lst.json()["data"]["items"][0]
        assert run["status"] == "failed"
        assert run["previous_rag_document_id"] == old_doc_id
        assert run["rag_document_id"] is not None  # 新文档 ID 保留

        # retry：关闭删除失败 → 只重试旧文档清理，不重新上传新版
        uploads_before = fake.upload_calls
        fake.fail_delete = False
        retry_resp = await client.post(
            f"/api/v1/admin/faqs/{faq['id']}/sync:retry",
            headers=await bearer_headers(token),
        )
        assert retry_resp.status_code == 200, retry_resp.text
        # sync:retry 遵循全局契约 {request_id, data: FaqSyncRunView}
        retry_body = retry_resp.json()
        assert "request_id" in retry_body and "data" in retry_body
        assert retry_body["data"]["status"] == "succeeded"
        assert fake.upload_calls == uploads_before  # 未重新上传
        assert fake.delete_calls == 2  # 第一次失败 + 重试成功

    async def test_concurrent_refresh_no_double_delete(
        self, client, admin_user, db_session, faq_cleanup, faq_rag_factory
    ):
        fake = faq_rag_factory(FakeRag(), db_session)
        token = await _admin_token(client, admin_user)
        # 防御：先刷新一次，把历史残留 pending/syncing run 处理为终态，
        # 避免测试间污染（残留 run 在上游 404 → 保守 failed，不影响后续断言）
        await client.get(
            "/api/v1/admin/faq-sync-runs",
            headers=await bearer_headers(token),
        )
        faq = await _publish_faq(
            client, token, scope="admin_private", question="并发清理问题", answer="答案"
        )
        h = faq["normalized_question_hash"]
        faq_cleanup("admin_private", h)
        await self._complete_upload(fake)
        lst = await client.get(
            "/api/v1/admin/faq-sync-runs",
            headers=await bearer_headers(token),
        )
        old_doc_id = lst.json()["data"]["items"][0]["rag_document_id"]

        # 修改 → 新文档成功（有旧文档待清理）
        resp = await client.patch(
            f"/api/v1/admin/faqs/{faq['id']}",
            headers=await bearer_headers(token),
            json={"question": "并发清理问题2", "answer": "答案2"},
        )
        h2 = resp.json()["data"]["normalized_question_hash"]
        faq_cleanup("admin_private", h2)
        await self._complete_upload(fake)

        # 并发刷新 faq-sync-runs（行锁保证删除只执行一次）
        async def _refresh():
            return await client.get(
                "/api/v1/admin/faq-sync-runs",
                headers=await bearer_headers(token),
            )

        results = await asyncio.gather(*[_refresh() for _ in range(5)])
        for r in results:
            assert r.status_code == 200
        # 核心验收：并发轮询不能重复删除旧文档（条件锁定只允许一个请求进入删除区）
        assert fake.delete_calls == 1
        # 最新 run 已收敛为 succeeded（旧文档删除成功后才标记）
        statuses = {r.json()["data"]["items"][0]["status"] for r in results}
        assert "failed" not in statuses
        assert "succeeded" in statuses
        assert fake.document(old_doc_id) is None or fake.document(old_doc_id)["status"] == "deleted"


class TestAudit:
    async def test_faq_write_operations_audited(
        self, client, admin_user, db_session, faq_cleanup, faq_rag_factory
    ):
        faq_rag_factory(FakeRag(), db_session)
        token = await _admin_token(client, admin_user)
        faq = await _publish_faq(
            client, token, scope="internal_shared", question="审计问题", answer="答案"
        )
        h = faq["normalized_question_hash"]
        faq_cleanup("internal_shared", h)
        await client.patch(
            f"/api/v1/admin/faqs/{faq['id']}",
            headers=await bearer_headers(token),
            json={"question": "审计问题2", "answer": "答案2"},
        )
        await client.post(
            f"/api/v1/admin/faqs/{faq['id']}/unpublish",
            headers=await bearer_headers(token),
        )
        await client.post(
            f"/api/v1/admin/faqs/{faq['id']}/publish",
            headers=await bearer_headers(token),
        )

        from sqlalchemy import func, select

        from app.models.audit_log import AuditLog

        total = await db_session.scalar(
            select(func.count())
            .select_from(AuditLog)
            .where(
                AuditLog.operator_user_id == admin_user["user_id"],
                AuditLog.action.in_(
                    ["faq_created", "faq_updated", "faq_unpublished", "faq_republished"]
                ),
            )
        )
        assert total == 4


class TestFirstReviewFixes:
    """Stage 5 Batch 1 首轮复核修复：unpublished 缓存、sync:retry 契约、单进行中防分叉、
    空 answer 校验、rag_sync_error 清理、写事务顺序。"""

    async def test_unpublished_patch_never_writes_cache(
        self, client, admin_user, db_session, faq_cleanup, faq_rag_factory
    ):
        """publish → unpublish → PATCH：新旧 hash 的 cache 都不存在，lookup miss。"""
        faq_rag_factory(FakeRag(), db_session)
        token = await _admin_token(client, admin_user)
        faq = await _publish_faq(
            client, token, scope="internal_shared", question="下线后修改问题？", answer="旧答案"
        )
        old_hash = faq["normalized_question_hash"]
        faq_cleanup("internal_shared", old_hash)

        redis = await get_redis()
        if redis is None:
            pytest.skip("Redis 不可达，跳过缓存断言")
        assert await redis.get(faq_cache_key("internal_shared", old_hash)) is not None

        # 下线：缓存删除
        resp = await client.post(
            f"/api/v1/admin/faqs/{faq['id']}/unpublish",
            headers=await bearer_headers(token),
        )
        assert resp.status_code == 200
        assert await redis.get(faq_cache_key("internal_shared", old_hash)) is None

        # 下线后 PATCH（问题也改了）
        resp = await client.patch(
            f"/api/v1/admin/faqs/{faq['id']}",
            headers=await bearer_headers(token),
            json={"question": "下线后修改问题v2", "answer": "新答案"},
        )
        assert resp.status_code == 200
        updated = resp.json()["data"]
        new_hash = updated["normalized_question_hash"]
        assert updated["status"] == "unpublished"  # MySQL status 不变
        faq_cleanup("internal_shared", new_hash)

        # 新旧 hash 的 cache 都不存在（unpublished 禁止写 cache）
        assert await redis.get(faq_cache_key("internal_shared", old_hash)) is None
        assert await redis.get(faq_cache_key("internal_shared", new_hash)) is None

        # Stage 4 FAQ lookup：新旧 hash 都 miss
        service = FaqService(repository=FaqRepository(db_session))
        for h in (old_hash, new_hash):
            hit = await service.lookup_exact_faq(
                scopes=["internal_shared"],
                normalized_question="x",
                normalized_question_hash=h,
            )
            assert hit is None, f"unpublished FAQ 的 hash={h} 不应命中"

        # 重新发布后缓存恢复
        resp = await client.post(
            f"/api/v1/admin/faqs/{faq['id']}/publish",
            headers=await bearer_headers(token),
        )
        assert resp.status_code == 200
        assert await redis.get(faq_cache_key("internal_shared", new_hash)) is not None

    async def test_sync_retry_response_contract(
        self, client, admin_user, db_session, faq_cleanup, faq_rag_factory
    ):
        """sync:retry 响应遵循全局 {request_id, data: FaqSyncRunView} 契约。"""
        fake = faq_rag_factory(FakeRag(), db_session)
        token = await _admin_token(client, admin_user)
        faq = await _publish_faq(
            client, token, scope="internal_shared", question="契约测试问题", answer="答案"
        )
        h = faq["normalized_question_hash"]
        faq_cleanup("internal_shared", h)
        await self._complete_upload(fake)
        await client.get(
            "/api/v1/admin/faq-sync-runs",
            headers=await bearer_headers(token),
        )

        resp = await client.post(
            f"/api/v1/admin/faqs/{faq['id']}/sync:retry",
            headers=await bearer_headers(token),
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        # 全局壳：request_id + data（data 为 FaqSyncRunView，含 status）
        assert "request_id" in body and "data" in body
        assert body["data"]["status"] == "succeeded"
        assert body["data"]["knowledge_scope"] == "internal_shared"

    async def test_single_in_progress_no_fork_and_catch_up(
        self, client, admin_user, db_session, faq_cleanup, faq_rag_factory
    ):
        """doc1 succeeded → 修改产生 doc2 syncing → doc2 未完成前再修改不立即上传 →
        doc2 成功后自动 catch-up doc3（previous=doc2）→ doc3 成功后 doc2 被删除，
        最终 Dataset 只保留最新文档。"""
        fake = faq_rag_factory(FakeRag(), db_session)
        token = await _admin_token(client, admin_user)
        faq = await _publish_faq(
            client, token, scope="admin_private", question="分叉问题一", answer="答案一"
        )
        h1 = faq["normalized_question_hash"]
        faq_cleanup("admin_private", h1)
        # doc1 succeeded
        await self._complete_upload(fake)
        await client.get(
            "/api/v1/admin/faq-sync-runs",
            headers=await bearer_headers(token),
        )
        doc1_id = fake.tasks[list(fake.tasks)[-1]]["document_id"]

        # 修改 → doc2 上传（syncing）
        resp = await client.patch(
            f"/api/v1/admin/faqs/{faq['id']}",
            headers=await bearer_headers(token),
            json={"question": "分叉问题二", "answer": "答案二"},
        )
        assert resp.status_code == 200
        h2 = resp.json()["data"]["normalized_question_hash"]
        faq_cleanup("admin_private", h2)
        uploads_after_patch = fake.upload_calls
        assert uploads_after_patch == 2  # doc1 + doc2
        runs = (
            await client.get(
                "/api/v1/admin/faq-sync-runs",
                headers=await bearer_headers(token),
            )
        ).json()["data"]["items"]
        run2 = runs[0]  # 最新 run（doc2，进行中）
        doc2_id = run2["rag_document_id"]

        # doc2 未完成前再次修改 → 不立即上传 doc3（单进行中防分叉）
        resp = await client.patch(
            f"/api/v1/admin/faqs/{faq['id']}",
            headers=await bearer_headers(token),
            json={"question": "分叉问题三", "answer": "答案三"},
        )
        assert resp.status_code == 200
        h3 = resp.json()["data"]["normalized_question_hash"]
        faq_cleanup("admin_private", h3)
        assert fake.upload_calls == uploads_after_patch  # 未上传 doc3

        # doc2 completed → refresh：run2 succeeded + catch-up 自动上传 doc3（previous=doc2）
        await self._complete_upload(fake)
        runs = (
            await client.get(
                "/api/v1/admin/faq-sync-runs",
                headers=await bearer_headers(token),
            )
        ).json()["data"]["items"]
        assert fake.upload_calls == uploads_after_patch + 1  # catch-up 上传 doc3
        run3 = runs[0]  # 最新 run（catch-up 的 doc3）
        assert run3["previous_rag_document_id"] == doc2_id  # previous 指向刚成功的新文档
        # run2 已 succeeded 且其 previous(doc1) 已被清理
        run2_now = runs[1]
        assert run2_now["status"] == "succeeded"
        assert run2_now["previous_rag_document_id"] == doc1_id
        doc3_id = run3["rag_document_id"]

        # doc3 completed → refresh：doc2 被删除，最终只剩 doc3
        await self._complete_upload(fake)
        runs = (
            await client.get(
                "/api/v1/admin/faq-sync-runs",
                headers=await bearer_headers(token),
            )
        ).json()["data"]["items"]
        run3_now = runs[0]
        assert run3_now["status"] == "succeeded"
        assert run3_now["previous_rag_document_id"] == doc2_id
        # doc1、doc2 已删除；Dataset 只保留 doc3
        for doc_id in (doc1_id, doc2_id):
            d = fake.document(doc_id)
            assert d is None or d["status"] == "deleted", f"{doc_id} 应已删除"
        assert fake.document(doc3_id) is not None
        assert fake.document(doc3_id)["status"] == "completed"
        # 该 Dataset 只保留一份有效文档
        live = [d for d in fake.documents.values() if d["status"] != "deleted"]
        assert len(live) == 1 and live[0]["document_id"] == doc3_id

    async def test_late_failed_run_not_override_newer_scope_status(
        self, client, admin_user, db_session, faq_cleanup, faq_rag_factory
    ):
        """旧 run 迟到的 failed 不得覆盖更新 run 的 scope 展示状态。"""
        fake = faq_rag_factory(FakeRag(), db_session)
        token = await _admin_token(client, admin_user)
        faq = await _publish_faq(
            client, token, scope="internal_shared", question="迟到问题一", answer="答案一"
        )
        h1 = faq["normalized_question_hash"]
        faq_cleanup("internal_shared", h1)
        await self._complete_upload(fake)
        await client.get(
            "/api/v1/admin/faq-sync-runs",
            headers=await bearer_headers(token),
        )
        # 修改产生 doc2（syncing）
        resp = await client.patch(
            f"/api/v1/admin/faqs/{faq['id']}",
            headers=await bearer_headers(token),
            json={"question": "迟到问题二", "answer": "答案二"},
        )
        h2 = resp.json()["data"]["normalized_question_hash"]
        faq_cleanup("internal_shared", h2)
        # 修改产生内容变化（doc2 仍进行中，不新上传）
        resp = await client.patch(
            f"/api/v1/admin/faqs/{faq['id']}",
            headers=await bearer_headers(token),
            json={"question": "迟到问题三", "answer": "答案三"},
        )
        h3 = resp.json()["data"]["normalized_question_hash"]
        faq_cleanup("internal_shared", h3)

        # doc2 completed → refresh → run2 succeeded + catch-up 创建 doc3（scope=syncing）
        await self._complete_upload(fake)
        await client.get(
            "/api/v1/admin/faq-sync-runs",
            headers=await bearer_headers(token),
        )
        await db_session.commit()
        rows = list(
            (
                await db_session.scalars(
                    select(Faq).where(
                        Faq.knowledge_scope == "internal_shared", Faq.status == "published"
                    )
                )
            ).all()
        )
        assert rows and rows[0].rag_sync_status == "syncing"  # doc3 进行中展示

        # 模拟 doc2 的迟到 failed：直接调 service（此时 doc3 是 scope 最新 run）
        import_client, document_client = _make_clients(fake)
        svc = FaqSyncService(
            runs=FaqSyncRunRepository(db_session),
            faqs=FaqRepository(db_session),
            import_client=import_client,
            document_client=document_client,
        )
        from app.models.faq_sync_run import FaqSyncRun as _Run

        # doc2 的 run = scope 内第二新的记录（第一个是 catch-up 的 doc3）
        scope_runs = list(
            (
                await db_session.scalars(
                    select(_Run)
                    .where(_Run.knowledge_scope == "internal_shared")
                    .order_by(_Run.created_at.desc())
                )
            ).all()
        )
        run2_row = scope_runs[1]
        await svc._mark_failed_with_scope(
            run2_row, error_code="RAG_IMPORT_FAILED", error_message="迟到失败"
        )
        await db_session.commit()
        # scope 展示状态未被旧 run 覆盖（仍是 syncing，doc3 进行中）
        rows = list(
            (
                await db_session.scalars(
                    select(Faq).where(
                        Faq.knowledge_scope == "internal_shared", Faq.status == "published"
                    )
                )
            ).all()
        )
        assert rows[0].rag_sync_status == "syncing"

        # doc3 completed → refresh → 最终 succeeded，error 清理
        await self._complete_upload(fake)
        await client.get(
            "/api/v1/admin/faq-sync-runs",
            headers=await bearer_headers(token),
        )
        # commit 结束旧事务快照 + expire identity map（expire_on_commit=False）
        await db_session.commit()
        db_session.expire_all()
        rows = list(
            (
                await db_session.scalars(
                    select(Faq).where(
                        Faq.knowledge_scope == "internal_shared", Faq.status == "published"
                    )
                )
            ).all()
        )
        assert rows[0].rag_sync_status == "succeeded"
        assert rows[0].rag_sync_error is None

    async def test_empty_answer_rejected_no_side_effects(
        self, client, admin_user, db_session, faq_cleanup, faq_rag_factory
    ):
        """create / PATCH 空 answer：稳定 400，不写库、不写 Redis、不触发 sync。"""
        fake = faq_rag_factory(FakeRag(), db_session)
        token = await _admin_token(client, admin_user)

        # create 空 answer
        resp = await client.post(
            "/api/v1/admin/faqs",
            headers=await bearer_headers(token),
            json={"knowledge_scope": "internal_shared", "question": "空答案问题", "answer": "   "},
        )
        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == "INVALID_REQUEST"
        assert fake.upload_calls == 0  # 空 answer 不触发上传

        # 合法创建后 PATCH 空 answer
        faq = await _publish_faq(
            client, token, scope="internal_shared", question="空答案问题", answer="正常答案"
        )
        h = faq["normalized_question_hash"]
        faq_cleanup("internal_shared", h)
        uploads_before = fake.upload_calls
        resp = await client.patch(
            f"/api/v1/admin/faqs/{faq['id']}",
            headers=await bearer_headers(token),
            json={"question": "空答案问题", "answer": ""},
        )
        assert resp.status_code == 400

        # 无副作用：未触发 upload、MySQL answer 未变、Redis 未写空答案
        assert fake.upload_calls == uploads_before
        await db_session.commit()
        from sqlalchemy import select

        from app.models.faq import Faq

        row = await db_session.scalar(select(Faq).where(Faq.id == faq["id"]))
        assert row.answer == "正常答案"

    async def test_rag_sync_error_cleared_after_success(
        self, client, admin_user, db_session, faq_cleanup, faq_rag_factory
    ):
        """failed → retry → succeeded：published FAQ 状态=succeeded 且 error=NULL。"""
        fake = faq_rag_factory(FakeRag(), db_session)
        token = await _admin_token(client, admin_user)
        faq = await _publish_faq(
            client, token, scope="internal_shared", question="错误清理问题一", answer="答案一"
        )
        h1 = faq["normalized_question_hash"]
        faq_cleanup("internal_shared", h1)
        await self._complete_upload(fake)
        await client.get(
            "/api/v1/admin/faq-sync-runs",
            headers=await bearer_headers(token),
        )

        # 修改 → 上传失败 → sync failed，FAQ 展示 failed + error
        fake.fail_upload = True
        resp = await client.patch(
            f"/api/v1/admin/faqs/{faq['id']}",
            headers=await bearer_headers(token),
            json={"question": "错误清理问题二", "answer": "答案二"},
        )
        h2 = resp.json()["data"]["normalized_question_hash"]
        faq_cleanup("internal_shared", h2)
        await db_session.commit()
        rows = list(
            (
                await db_session.scalars(
                    select(Faq).where(
                        Faq.knowledge_scope == "internal_shared", Faq.status == "published"
                    )
                )
            ).all()
        )
        assert rows[0].rag_sync_status == "failed"
        assert rows[0].rag_sync_error is not None

        # retry（恢复上游）→ 新 run upload → complete → refresh → succeeded，error 清理
        fake.fail_upload = False
        retry_resp = await client.post(
            f"/api/v1/admin/faqs/{faq['id']}/sync:retry",
            headers=await bearer_headers(token),
        )
        assert retry_resp.status_code == 200, retry_resp.text
        await self._complete_upload(fake)
        await client.get(
            "/api/v1/admin/faq-sync-runs",
            headers=await bearer_headers(token),
        )
        # commit 结束旧事务快照 + expire identity map（expire_on_commit=False）
        await db_session.commit()
        db_session.expire_all()
        rows = list(
            (
                await db_session.scalars(
                    select(Faq).where(
                        Faq.knowledge_scope == "internal_shared", Faq.status == "published"
                    )
                )
            ).all()
        )
        assert rows[0].rag_sync_status == "succeeded"
        assert rows[0].rag_sync_error is None

    # ---------- 复用 TestSyncStateMachine 的上传推进 helper ----------

    async def _complete_upload(self, fake: FakeRag) -> None:
        """把最新上传任务置为 completed，并确认文档快照可查。"""
        rag_task_id = list(fake.tasks)[-1]
        rag_doc_id = fake.tasks[rag_task_id]["document_id"]
        fake.set_task_status(rag_task_id, "completed", done=[{"name": "upload_file"}])
        fake.documents[rag_doc_id]["status"] = "completed"


class TestTransactionOrder:
    """写事务顺序（冻结 API §12）：MySQL 业务事务先真正 commit → Redis → RAG。

    请求级 get_db 在路由完成后才 commit；FaqService 各写方法在 audit 之后
    显式 commit，保证副作用执行时新状态已对外可见，且 commit 失败时副作用为零。
    """

    async def test_mysql_committed_before_side_effects(
        self, client, admin_user, db_session, faq_cleanup, faq_rag_factory, monkeypatch
    ):
        """Redis 副作用执行时，独立 DB session 已能读到 committed 的新 FAQ 状态。"""
        faq_rag_factory(FakeRag(), db_session)
        token = await _admin_token(client, admin_user)

        from app.core.database import get_session_factory

        observed: dict[str, str | None] = {}
        original_set_cache = FaqService.set_faq_cache

        async def spy_set_faq_cache(self, faq):
            # 副作用（Redis 写）执行点：用独立 session 读 MySQL，必须已提交
            async with get_session_factory()() as fresh:
                row = await fresh.get(Faq, faq.id)
                observed["status"] = row.status if row else None
                observed["answer"] = row.answer if row else None
            return await original_set_cache(self, faq)

        monkeypatch.setattr(FaqService, "set_faq_cache", spy_set_faq_cache)

        resp = await client.post(
            "/api/v1/admin/faqs",
            headers=await bearer_headers(token),
            json={
                "knowledge_scope": "internal_shared",
                "question": "事务顺序问题一",
                "answer": "事务答案一",
            },
        )
        assert resp.status_code == 200, resp.text
        faq = resp.json()["data"]
        h1 = faq["normalized_question_hash"]
        faq_cleanup("internal_shared", h1)
        # Redis 副作用执行时 MySQL 已 commit：独立 session 可读
        assert observed["status"] == "published"
        assert observed["answer"] == "事务答案一"

    async def test_mysql_commit_failure_skips_side_effects(
        self, client, admin_user, db_session, faq_cleanup, faq_rag_factory, monkeypatch
    ):
        """业务 commit 失败时：Redis/RAG 副作用调用次数均为 0，请求正常失败。"""
        fake = faq_rag_factory(FakeRag(), db_session)
        question = "事务失败问题一"

        # 直接构造 FaqService（与 HTTP 路由同一构造方式），用测试 db_session
        import_client, document_client = _make_clients(fake)
        svc = FaqService(
            repository=FaqRepository(db_session),
            candidates=FaqCandidateRepository(db_session),
            audit=AuditService(AuditLogRepository(db_session)),
            sync_service=FaqSyncService(
                runs=FaqSyncRunRepository(db_session),
                faqs=FaqRepository(db_session),
                import_client=import_client,
                document_client=document_client,
            ),
        )

        # 模拟业务 commit 失败：session.commit 抛异常（在 Redis/RAG 副作用之前）
        original_commit = db_session.commit

        async def failing_commit():
            raise RuntimeError("模拟业务 commit 失败")

        monkeypatch.setattr(db_session, "commit", failing_commit)

        with pytest.raises(RuntimeError, match="模拟业务 commit 失败"):
            await svc.create_faq(
                knowledge_scope="internal_shared",
                question=question,
                answer="事务失败答案",
                operator=admin_user["user"],
                client_ip="127.0.0.1",
            )

        # 恢复 commit（否则后续事务/teardown 受影响）
        monkeypatch.setattr(db_session, "commit", original_commit)

        # Redis 副作用为 0：新 FAQ 的 cache key 不存在
        h = question_hash(normalize_question(question))
        redis = await get_redis()
        if redis is not None:
            assert await redis.get(faq_cache_key("internal_shared", h)) is None
        # RAG 副作用为 0：未触发上传
        assert fake.upload_calls == 0
        # MySQL 业务状态未落库（commit 失败，事务回滚）
        await db_session.rollback()
        row = await db_session.scalar(
            select(Faq).where(Faq.knowledge_scope == "internal_shared", Faq.status == "published")
        )
        assert row is None or row.normalized_question_hash != h
