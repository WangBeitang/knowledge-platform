"""Stage 5 Batch 2：知识缺口闭环集成测试（真实 DB/Redis + 进程内直连）。

覆盖验收重点：
- 正常 RAG 完成 + 0 Citation → no_citation 候选；有 Citation → 不产生；
- insufficient_evidence（上游明确证据不足）→ insufficient_evidence 候选；
- RAG failed / unavailable / timeout / 系统异常 / 失败 Turn → 不产生；
- FAQ 精确命中（answer_source=faq_cache）→ 不产生缺口；
- 重复 analyze 去重正确（覆盖式，不翻倍、不重复建行），ask_count 聚合正确；
- ignore / resolve 状态机（pending_review → 终态；重复操作 409；不复活）；
- resolve 支持冻结字段 resolution_note / resolved_document_id；
- employee 访问 admin 接口 403；gap 不存在 404；
- ignore / resolve 写现有 AuditService（audit_logs 落库）。
"""

import uuid

import pytest
from sqlalchemy import delete, select

from app.core.normalizer import normalize_question, question_hash
from app.core.time import utc_now_naive
from app.models.chat_session import ChatSession
from app.models.qa_access_log import QaAccessLog
from tests.integration.conftest import _unique, api_login, bearer_headers, create_user_record


async def _admin_token(client, admin_user) -> str:
    resp = await api_login(client, admin_user["username"], admin_user["password"])
    return resp.json()["data"]["access_token"]


async def _seed_logs(
    db_session,
    *,
    user_id: str,
    specs: list[dict],
) -> tuple[str, str, str]:
    """插入多条 qa_access_logs（每条 spec 可自定义关键字段），返回 (hash, scope, session_id)。

    spec 默认：成功 RAG Turn + 0 citation（no_citation 缺口）。
    可覆盖：citation_count / terminal_reason_code / answer_source / status / error_code。
    """
    first = specs[0]
    question = first["question"]
    normalized = normalize_question(question)
    h = question_hash(normalized)
    scope = first.get("knowledge_scope", "internal_shared")
    chat_session = ChatSession(
        channel="internal_web",
        user_id=user_id,
        title="知识缺口测试会话",
        status="active",
        last_message_at=utc_now_naive(),
    )
    db_session.add(chat_session)
    await db_session.flush()
    for _i, spec in enumerate(specs):
        log = QaAccessLog(
            turn_id=str(uuid.uuid4()),
            session_id=chat_session.id,
            channel="internal_web",
            user_id=user_id,
            external_subject_hash=None,
            question=spec.get("question", question),
            normalized_question=normalized,
            normalized_question_hash=h,
            allowed_scopes_json=spec.get(
                "allowed_scopes", [scope, "external_public"]
            ),
            answer_source=spec.get("answer_source", "rag"),
            faq_id=None,
            rag_trace_id=None,
            terminal_reason_code=spec.get("terminal_reason_code"),
            citation_count=spec.get("citation_count", 0),
            citation_document_ids_json=spec.get("citation_document_ids", []),
            input_tokens=None,
            output_tokens=None,
            total_tokens=None,
            latency_ms=10,
            status=spec.get("status", "succeeded"),
            error_code=spec.get("error_code"),
            created_at=utc_now_naive(),
        )
        db_session.add(log)
    await db_session.commit()
    return h, scope, chat_session.id


@pytest.fixture
async def gap_cleanup(db_session):
    """按 scope 清理测试产生的知识缺口候选。"""
    scopes: list[str] = []

    def track(scope: str) -> None:
        if scope not in scopes:
            scopes.append(scope)

    yield track

    from app.models.knowledge_gap_candidate import KnowledgeGapCandidate

    for scope in scopes:
        await db_session.execute(
            delete(KnowledgeGapCandidate).where(
                KnowledgeGapCandidate.knowledge_scope == scope
            )
        )
    await db_session.commit()


async def _gap_by_hash(client, token, h: str) -> dict | None:
    resp = await client.get(
        "/api/v1/admin/knowledge-gaps?page_size=100",
        headers=await bearer_headers(token),
    )
    assert resp.status_code == 200, resp.text
    for item in resp.json()["data"]["items"]:
        if item["normalized_question_hash"] == h:
            return item
    return None


async def _audit_count(db_session, *, action: str, operator_user_id: str) -> int:
    from app.models.audit_log import AuditLog

    await db_session.commit()  # 刷新请求级 session 已提交的写
    stmt = select(AuditLog).where(
        AuditLog.action == action,
        AuditLog.operator_user_id == operator_user_id,
    )
    return len((await db_session.scalars(stmt)).all())


class TestAnalyze:
    async def test_no_citation_creates_gap(
        self, client, admin_user, db_session, gap_cleanup
    ):
        token = await _admin_token(client, admin_user)
        tag = uuid.uuid4().hex[:8]
        h, scope, _ = await _seed_logs(
            db_session,
            user_id=admin_user["user_id"],
            specs=[
                {"question": f"怎么开通科创板权限{tag}？"},
                {"question": f"怎么开通科创板权限{tag}？", "citation_count": 0},
                {"question": f"怎么开通科创板权限{tag}？（重复提问）", "citation_count": 0},
            ],
        )
        gap_cleanup(scope)

        resp = await client.post(
            "/api/v1/admin/knowledge-gaps/analyze",
            headers=await bearer_headers(token),
            json={},
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()["data"]
        assert set(data) == {"created", "updated"}
        assert data["created"] >= 1

        item = await _gap_by_hash(client, token, h)
        assert item is not None
        assert item["reason_code"] == "no_citation"
        assert item["status"] == "pending_review"
        assert item["ask_count"] == 3
        assert item["knowledge_scope"] == scope
        assert len(item["sample_questions"]) >= 1
        assert item["resolution_note"] is None
        assert item["resolved_document_id"] is None

    async def test_with_citation_no_gap(self, client, admin_user, db_session, gap_cleanup):
        token = await _admin_token(client, admin_user)
        tag = uuid.uuid4().hex[:8]
        h, scope, _ = await _seed_logs(
            db_session,
            user_id=admin_user["user_id"],
            specs=[
                {
                    "question": f"期权开户条件{tag}？",
                    "citation_count": 2,
                    "citation_document_ids": ["doc-a", "doc-b"],
                },
                {
                    "question": f"期权开户条件{tag}？",
                    "citation_count": 1,
                    "citation_document_ids": ["doc-a"],
                },
            ],
        )
        gap_cleanup(scope)

        resp = await client.post(
            "/api/v1/admin/knowledge-gaps/analyze",
            headers=await bearer_headers(token),
            json={},
        )
        assert resp.status_code == 200, resp.text
        assert await _gap_by_hash(client, token, h) is None

    async def test_insufficient_evidence_creates_gap(
        self, client, admin_user, db_session, gap_cleanup
    ):
        token = await _admin_token(client, admin_user)
        tag = uuid.uuid4().hex[:8]
        h, scope, _ = await _seed_logs(
            db_session,
            user_id=admin_user["user_id"],
            specs=[
                {
                    "question": f"某只私募基金的底层持仓{tag}？",
                    "citation_count": 0,
                    "terminal_reason_code": "insufficient_evidence",
                },
                {
                    "question": f"某只私募基金的底层持仓{tag}？",
                    "citation_count": 0,
                    "terminal_reason_code": "insufficient_evidence",
                },
            ],
        )
        gap_cleanup(scope)

        resp = await client.post(
            "/api/v1/admin/knowledge-gaps/analyze",
            headers=await bearer_headers(token),
            json={},
        )
        assert resp.status_code == 200, resp.text

        item = await _gap_by_hash(client, token, h)
        assert item is not None
        assert item["reason_code"] == "insufficient_evidence"
        assert item["ask_count"] == 2

    async def test_failed_or_unavailable_no_gap(
        self, client, admin_user, db_session, gap_cleanup
    ):
        """失败 Turn / RAG 不可用 / timeout / 系统异常均不产生缺口。"""
        token = await _admin_token(client, admin_user)
        tag = uuid.uuid4().hex[:8]
        base = f"不可用的知识{tag}？"
        failed_h, failed_scope, _ = await _seed_logs(
            db_session,
            user_id=admin_user["user_id"],
            specs=[
                {"question": base, "status": "failed", "error_code": "RAG_UNAVAILABLE"},
                {"question": base, "status": "failed", "error_code": "RAG_TIMEOUT"},
                {"question": base, "status": "failed", "error_code": "INTERNAL_ERROR"},
            ],
        )
        gap_cleanup(failed_scope)

        resp = await client.post(
            "/api/v1/admin/knowledge-gaps/analyze",
            headers=await bearer_headers(token),
            json={},
        )
        assert resp.status_code == 200, resp.text
        assert await _gap_by_hash(client, token, failed_h) is None

    async def test_faq_cache_hit_no_gap(self, client, admin_user, db_session, gap_cleanup):
        """FAQ 精确命中（answer_source=faq_cache、0 citation）不是知识缺口。"""
        token = await _admin_token(client, admin_user)
        tag = uuid.uuid4().hex[:8]
        h, scope, _ = await _seed_logs(
            db_session,
            user_id=admin_user["user_id"],
            specs=[
                {
                    "question": f"风险测评怎么办理{tag}？",
                    "answer_source": "faq_cache",
                    "citation_count": 0,
                },
                {
                    "question": f"风险测评怎么办理{tag}？",
                    "answer_source": "faq_cache",
                    "citation_count": 0,
                },
            ],
        )
        gap_cleanup(scope)

        resp = await client.post(
            "/api/v1/admin/knowledge-gaps/analyze",
            headers=await bearer_headers(token),
            json={},
        )
        assert resp.status_code == 200, resp.text
        assert await _gap_by_hash(client, token, h) is None

    async def test_repeat_analyze_dedup_and_aggregate(
        self, client, admin_user, db_session, gap_cleanup
    ):
        """重复 analyze 不重复建行；ask_count 全量聚合（含新旧日志）。"""
        token = await _admin_token(client, admin_user)
        tag = uuid.uuid4().hex[:8]
        question = f"重复分析缺口{tag}？"
        h, scope, _ = await _seed_logs(
            db_session,
            user_id=admin_user["user_id"],
            specs=[{"question": question} for _ in range(3)],
        )
        gap_cleanup(scope)

        for _ in range(2):
            resp = await client.post(
                "/api/v1/admin/knowledge-gaps/analyze",
                headers=await bearer_headers(token),
                json={},
            )
            assert resp.status_code == 200, resp.text

        items = []
        for page in (1, 2):
            resp = await client.get(
                f"/api/v1/admin/knowledge-gaps?page={page}&page_size=100",
                headers=await bearer_headers(token),
            )
            items += [
                i
                for i in resp.json()["data"]["items"]
                if i["normalized_question_hash"] == h
            ]
        assert len(items) == 1
        assert items[0]["ask_count"] == 3

        # 再插入 2 条同 hash 日志，重复 analyze 后 ask_count 累加为 5，仍只有一行
        await _seed_logs(
            db_session,
            user_id=admin_user["user_id"],
            specs=[{"question": question}, {"question": question}],
        )
        resp = await client.post(
            "/api/v1/admin/knowledge-gaps/analyze",
            headers=await bearer_headers(token),
            json={},
        )
        assert resp.status_code == 200, resp.text
        item = await _gap_by_hash(client, token, h)
        assert item is not None
        assert item["ask_count"] == 5

    async def test_mixed_reason_priority_and_citation_excluded(
        self, client, admin_user, db_session, gap_cleanup
    ):
        """组内既有 insufficient_evidence 又有 no_citation → 取 insufficient_evidence；
        有引用的正常日志不计入 ask_count。"""
        token = await _admin_token(client, admin_user)
        tag = uuid.uuid4().hex[:8]
        question = f"混合原因缺口{tag}？"
        h, scope, _ = await _seed_logs(
            db_session,
            user_id=admin_user["user_id"],
            specs=[
                {"question": question, "citation_count": 0},  # no_citation
                {
                    "question": question,
                    "citation_count": 0,
                    "terminal_reason_code": "insufficient_evidence",
                },
                {
                    "question": question,
                    "citation_count": 3,
                    "citation_document_ids": ["doc-x"],
                },  # 有引用：不产生缺口、不计入
            ],
        )
        gap_cleanup(scope)

        resp = await client.post(
            "/api/v1/admin/knowledge-gaps/analyze",
            headers=await bearer_headers(token),
            json={},
        )
        assert resp.status_code == 200, resp.text
        item = await _gap_by_hash(client, token, h)
        assert item is not None
        assert item["reason_code"] == "insufficient_evidence"
        assert item["ask_count"] == 2


class TestReview:
    async def test_ignore_gap(self, client, admin_user, db_session, gap_cleanup):
        token = await _admin_token(client, admin_user)
        tag = uuid.uuid4().hex[:8]
        h, scope, _ = await _seed_logs(
            db_session,
            user_id=admin_user["user_id"],
            specs=[{"question": f"待忽略缺口{tag}？"}],
        )
        gap_cleanup(scope)
        await client.post(
            "/api/v1/admin/knowledge-gaps/analyze",
            headers=await bearer_headers(token),
            json={},
        )
        item = await _gap_by_hash(client, token, h)
        assert item is not None and item["status"] == "pending_review"

        resp = await client.post(
            f"/api/v1/admin/knowledge-gaps/{item['id']}/ignore",
            headers=await bearer_headers(token),
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["data"]["status"] == "ignored"
        assert resp.json()["data"]["reviewed_by_user_id"] == admin_user["user_id"]

        # 重复 ignore → 409
        resp = await client.post(
            f"/api/v1/admin/knowledge-gaps/{item['id']}/ignore",
            headers=await bearer_headers(token),
        )
        assert resp.status_code == 409, resp.text

        # 审计落库
        assert await _audit_count(
            db_session, action="gap_ignored", operator_user_id=admin_user["user_id"]
        ) == 1

    async def test_resolve_gap(self, client, admin_user, db_session, gap_cleanup):
        token = await _admin_token(client, admin_user)
        tag = uuid.uuid4().hex[:8]
        h, scope, _ = await _seed_logs(
            db_session,
            user_id=admin_user["user_id"],
            specs=[{"question": f"待解决缺口{tag}？"}],
        )
        gap_cleanup(scope)
        await client.post(
            "/api/v1/admin/knowledge-gaps/analyze",
            headers=await bearer_headers(token),
            json={},
        )
        item = await _gap_by_hash(client, token, h)
        assert item is not None

        resp = await client.post(
            f"/api/v1/admin/knowledge-gaps/{item['id']}/resolve",
            headers=await bearer_headers(token),
            json={
                "resolution_note": "已补充科创板权限文档",
                "resolved_document_id": "doc-gap-001",
            },
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()["data"]
        assert data["status"] == "resolved"
        assert data["resolution_note"] == "已补充科创板权限文档"
        assert data["resolved_document_id"] == "doc-gap-001"
        assert data["reviewed_by_user_id"] == admin_user["user_id"]

        # 重复 resolve → 409；resolve 后 ignore → 409
        resp = await client.post(
            f"/api/v1/admin/knowledge-gaps/{item['id']}/resolve",
            headers=await bearer_headers(token),
            json={},
        )
        assert resp.status_code == 409, resp.text
        resp = await client.post(
            f"/api/v1/admin/knowledge-gaps/{item['id']}/ignore",
            headers=await bearer_headers(token),
        )
        assert resp.status_code == 409, resp.text

        assert await _audit_count(
            db_session, action="gap_resolved", operator_user_id=admin_user["user_id"]
        ) == 1

    async def test_resolve_empty_body_allowed(
        self, client, admin_user, db_session, gap_cleanup
    ):
        """resolve 两个字段均可选：无 body / 空对象 / 只有 note 均合法。"""
        token = await _admin_token(client, admin_user)
        tag = uuid.uuid4().hex[:8]
        h, scope, _ = await _seed_logs(
            db_session,
            user_id=admin_user["user_id"],
            specs=[{"question": f"空 body 解决缺口{tag}？"}],
        )
        gap_cleanup(scope)
        await client.post(
            "/api/v1/admin/knowledge-gaps/analyze",
            headers=await bearer_headers(token),
            json={},
        )
        item = await _gap_by_hash(client, token, h)
        assert item is not None

        resp = await client.post(
            f"/api/v1/admin/knowledge-gaps/{item['id']}/resolve",
            headers=await bearer_headers(token),
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["data"]["status"] == "resolved"
        assert resp.json()["data"]["resolution_note"] is None

    async def test_reanalyze_does_not_revive_terminal(
        self, client, admin_user, db_session, gap_cleanup
    ):
        """已 ignore/resolve 的候选重复 analyze 不复活（status 保留）。"""
        token = await _admin_token(client, admin_user)
        tag = uuid.uuid4().hex[:8]
        h, scope, _ = await _seed_logs(
            db_session,
            user_id=admin_user["user_id"],
            specs=[{"question": f"终态不复活缺口{tag}？"}],
        )
        gap_cleanup(scope)
        await client.post(
            "/api/v1/admin/knowledge-gaps/analyze",
            headers=await bearer_headers(token),
            json={},
        )
        item = await _gap_by_hash(client, token, h)
        assert item is not None
        await client.post(
            f"/api/v1/admin/knowledge-gaps/{item['id']}/resolve",
            headers=await bearer_headers(token),
            json={"resolution_note": "已解决"},
        )

        # 新日志 + 重复 analyze → 仍 resolved，且 ask_count 更新
        await _seed_logs(
            db_session,
            user_id=admin_user["user_id"],
            specs=[{"question": f"终态不复活缺口{tag}？"}],
        )
        resp = await client.post(
            "/api/v1/admin/knowledge-gaps/analyze",
            headers=await bearer_headers(token),
            json={},
        )
        assert resp.status_code == 200, resp.text
        item = await _gap_by_hash(client, token, h)
        assert item is not None
        assert item["status"] == "resolved"
        assert item["ask_count"] == 2

    async def test_gap_not_found(self, client, admin_user):
        token = await _admin_token(client, admin_user)
        fake_id = "00000000-0000-0000-0000-000000000000"
        for path in (f"/api/v1/admin/knowledge-gaps/{fake_id}/ignore",
                     f"/api/v1/admin/knowledge-gaps/{fake_id}/resolve"):
            resp = await client.post(path, headers=await bearer_headers(token), json={})
            assert resp.status_code == 404, resp.text

    async def test_employee_forbidden(
        self, client, admin_user, db_session, tracked_users, gap_cleanup
    ):
        """employee 访问 admin 知识缺口接口一律 403。"""
        token = await _admin_token(client, admin_user)
        tag = uuid.uuid4().hex[:8]
        h, scope, _ = await _seed_logs(
            db_session,
            user_id=admin_user["user_id"],
            specs=[{"question": f"权限缺口{tag}？"}],
        )
        gap_cleanup(scope)
        await client.post(
            "/api/v1/admin/knowledge-gaps/analyze",
            headers=await bearer_headers(token),
            json={},
        )
        item = await _gap_by_hash(client, token, h)
        assert item is not None

        emp = await create_user_record(
            db_session,
            username=_unique("it_emp"),
            display_name="缺口权限测试员工",
            role="employee",
            password="EmpTest#2026",
        )
        tracked_users.append(emp.id)
        await db_session.commit()
        emp_token = (await api_login(client, emp.username, "EmpTest#2026")).json()[
            "data"
        ]["access_token"]

        checks = [
            ("POST", "/api/v1/admin/knowledge-gaps/analyze"),
            ("GET", "/api/v1/admin/knowledge-gaps"),
            ("POST", f"/api/v1/admin/knowledge-gaps/{item['id']}/ignore"),
            ("POST", f"/api/v1/admin/knowledge-gaps/{item['id']}/resolve"),
        ]
        for method, path in checks:
            resp = await client.request(
                method, path, headers=await bearer_headers(emp_token), json={}
            )
            assert resp.status_code == 403, (method, path, resp.text)
