"""Stage 5 Batch 3：运营看板集成测试（真实 DB/Redis + 进程内直连）。

覆盖验收重点：
- 日期/channel 过滤正确；summary 只统计真实日志；
- success rate / latency 正确；
- Token 全部存在时统计正确；部分缺失时 token_coverage_rate 正确，
  缺失值不伪造成 0（不参与求和、全空为 null）；
- trends 日/小时聚合正确；top-questions 排序正确；
- top-documents 基于真实 Citation document IDs；
- 空数据区间返回合法空结果（不报错、不造数据）；
- limit 上限（<=100）与非法日期参数校验；
- employee 访问 Dashboard → 403。
"""

import uuid
from datetime import datetime, timedelta

import pytest
from sqlalchemy import delete

from app.core.normalizer import normalize_question, question_hash
from app.core.time import utc_now_naive
from app.models.chat_session import ChatSession
from app.models.qa_access_log import QaAccessLog
from tests.integration.conftest import api_login, bearer_headers

# 固定 UTC 测试时刻：使用未来唯一时间窗（2030-06），避免与库中历史
# 真实日志（smoke 遗留）或并发测试互相污染；断言型查询均显式传日期范围。
BASE = datetime(2030, 6, 1, 8, 0, 0)
WINDOW = {"date_from": "2030-06-01", "date_to": "2030-06-30"}


async def _admin_token(client, admin_user) -> str:
    resp = await api_login(client, admin_user["username"], admin_user["password"])
    return resp.json()["data"]["access_token"]


async def _seed_logs(db_session, *, specs: list[dict], user_id: str):
    """插入 chat_session + 多条 qa_access_logs，返回 (session_ids, turn_ids)。

    spec 支持：question / created_at / channel / user_id / external_subject_hash /
    status / error_code / input_tokens / output_tokens / total_tokens /
    latency_ms / citation_document_ids / answer_source / terminal_reason_code。
    """
    session_ids: list[str] = []
    turn_ids: list[str] = []
    session = ChatSession(
        channel="internal_web",
        user_id=user_id,
        title="看板测试会话",
        status="active",
        last_message_at=utc_now_naive(),
    )
    db_session.add(session)
    await db_session.flush()
    session_ids.append(session.id)
    for spec in specs:
        question = spec.get("question", "默认问题")
        normalized = normalize_question(question)
        turn_id = str(uuid.uuid4())
        turn_ids.append(turn_id)
        db_session.add(
            QaAccessLog(
                turn_id=turn_id,
                session_id=session.id,
                channel=spec.get("channel", "internal_web"),
                user_id=spec.get("user_id", user_id),
                external_subject_hash=spec.get("external_subject_hash"),
                question=question,
                normalized_question=normalized,
                normalized_question_hash=question_hash(normalized),
                allowed_scopes_json=spec.get("allowed_scopes", ["internal_shared"]),
                answer_source=spec.get("answer_source", "rag"),
                faq_id=None,
                rag_trace_id=None,
                terminal_reason_code=spec.get("terminal_reason_code"),
                citation_count=len(spec.get("citation_document_ids", [])),
                citation_document_ids_json=spec.get("citation_document_ids", []),
                input_tokens=spec.get("input_tokens"),
                output_tokens=spec.get("output_tokens"),
                total_tokens=spec.get("total_tokens"),
                latency_ms=spec.get("latency_ms", 10),
                status=spec.get("status", "succeeded"),
                error_code=spec.get("error_code"),
                created_at=spec.get("created_at", utc_now_naive()),
            )
        )
    await db_session.commit()
    return session_ids, turn_ids


@pytest.fixture
async def dashboard_cleanup(db_session):
    """跟踪测试产生的 logs/sessions，teardown 统一清理。"""
    session_ids: list[str] = []
    turn_ids: list[str] = []
    yield session_ids, turn_ids
    if turn_ids:
        await db_session.execute(
            delete(QaAccessLog).where(QaAccessLog.turn_id.in_(turn_ids))
        )
    if session_ids:
        await db_session.execute(
            delete(ChatSession).where(ChatSession.id.in_(session_ids))
        )
    await db_session.commit()


async def test_summary_counts_real_logs(
    client, db_session, admin_user, dashboard_cleanup
):
    """summary 只统计真实日志：PV/UV/问答量/成功率/延迟/Token 全部正确。"""
    token = await _admin_token(client, admin_user)
    session_ids, turn_ids = await _seed_logs(
        db_session,
        user_id=admin_user["user_id"],
        specs=[
            # 用户 A：3 条成功 + 1 条失败；token 只有 2 条完整
            dict(
                question="风险测评怎么做",
                created_at=BASE,
                status="succeeded",
                input_tokens=100,
                output_tokens=50,
                total_tokens=150,
                latency_ms=100,
            ),
            dict(
                question="风险测评怎么做",
                created_at=BASE + timedelta(hours=1),
                status="succeeded",
                input_tokens=200,
                output_tokens=80,
                total_tokens=280,
                latency_ms=200,
            ),
            dict(
                question="风险测评怎么做",
                created_at=BASE + timedelta(hours=2),
                status="failed",
                error_code="RAG_UNAVAILABLE",
                latency_ms=300,
            ),
            # 用户 B（外部匿名）：1 条成功，token 缺失
            dict(
                question="开户需要什么材料",
                created_at=BASE + timedelta(hours=3),
                user_id=None,
                external_subject_hash="ext-abc123",
                status="succeeded",
                latency_ms=400,
            ),
        ],
    )
    resp = await client.get(
        "/api/v1/admin/dashboard/summary",
        params=WINDOW,
        headers=await bearer_headers(token),
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    # 4 条日志：PV=4；UV=2（用户 A + 外部 ext-abc123）
    assert data["pv_count"] == 4
    assert data["uv_count"] == 2
    # 问答量=成功轮次=3；成功率=3/4=0.75
    assert data["question_count"] == 3
    assert data["success_rate"] == 0.75
    # 平均延迟 = (100+200+300+400)/4 = 250
    assert data["avg_latency_ms"] == 250.0
    # Token 只累加真实回填值：input=300, output=130, total=430；coverage=2/4=0.5
    assert data["token_input_total"] == 300
    assert data["token_output_total"] == 130
    assert data["token_total"] == 430
    assert data["token_coverage_rate"] == 0.5


async def test_summary_date_and_channel_filter(
    client, db_session, admin_user, dashboard_cleanup
):
    """date_from/date_to/channel 过滤只统计区间内真实日志。"""
    token = await _admin_token(client, admin_user)
    session_ids, turn_ids = await _seed_logs(
        db_session,
        user_id=admin_user["user_id"],
        specs=[
            # 区间外（08-10）internal_web
            dict(question="q1", created_at=BASE, channel="internal_web", latency_ms=10),
            # 区间内（08-12）internal_web
            dict(
                question="q2",
                created_at=BASE + timedelta(days=2),
                channel="internal_web",
                latency_ms=20,
            ),
            # 区间内（08-12）external_api
            dict(
                question="q3",
                created_at=BASE + timedelta(days=2),
                channel="external_api",
                user_id=None,
                external_subject_hash="ext-1",
                latency_ms=30,
            ),
        ],
    )
    # 仅 06-02 ~ 06-04：2 条（06-01 的日志在窗口外）
    resp = await client.get(
        "/api/v1/admin/dashboard/summary",
        params={"date_from": "2030-06-02", "date_to": "2030-06-04"},
        headers=await bearer_headers(token),
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["pv_count"] == 2
    assert data["uv_count"] == 2  # 用户 A + ext-1
    assert data["avg_latency_ms"] == 25.0
    # 再叠加 channel=external_api：只剩 1 条
    resp2 = await client.get(
        "/api/v1/admin/dashboard/summary",
        params={
            "date_from": "2030-06-02",
            "date_to": "2030-06-04",
            "channel": "external_api",
        },
        headers=await bearer_headers(token),
    )
    assert resp2.status_code == 200, resp2.text
    data2 = resp2.json()["data"]
    assert data2["pv_count"] == 1
    assert data2["uv_count"] == 1


async def test_summary_empty_range_returns_legal_empty(
    client, db_session, admin_user, dashboard_cleanup
):
    """空数据区间返回合法空结果：不报错、不造数据（null 而非 0）。"""
    token = await _admin_token(client, admin_user)
    resp = await client.get(
        "/api/v1/admin/dashboard/summary",
        params={"date_from": "2030-01-01", "date_to": "2030-01-31"},
        headers=await bearer_headers(token),
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["pv_count"] == 0
    assert data["uv_count"] == 0
    assert data["question_count"] == 0
    assert data["success_rate"] is None  # 0/0 不伪造 0
    assert data["avg_latency_ms"] is None
    assert data["token_input_total"] is None
    assert data["token_output_total"] is None
    assert data["token_total"] is None
    assert data["token_coverage_rate"] is None


async def test_trends_day_and_hour_aggregation(
    client, db_session, admin_user, dashboard_cleanup
):
    """trends 日/小时粒度聚合正确，桶内统计口径与 summary 一致。"""
    token = await _admin_token(client, admin_user)
    session_ids, turn_ids = await _seed_logs(
        db_session,
        user_id=admin_user["user_id"],
        specs=[
            # 06-01：2 条（1 成功 1 失败）
            dict(
                question="a",
                created_at=BASE,
                status="succeeded",
                total_tokens=100,
                input_tokens=80,
                output_tokens=20,
                latency_ms=50,
            ),
            dict(
                question="b",
                created_at=BASE + timedelta(hours=3),
                status="failed",
                error_code="X",
                latency_ms=150,
            ),
            # 06-02：1 条成功
            dict(
                question="c",
                created_at=BASE + timedelta(days=1),
                status="succeeded",
                total_tokens=50,
                input_tokens=40,
                output_tokens=10,
                latency_ms=100,
            ),
        ],
    )
    resp = await client.get(
        "/api/v1/admin/dashboard/trends",
        params={**WINDOW, "granularity": "day"},
        headers=await bearer_headers(token),
    )
    assert resp.status_code == 200, resp.text
    items = resp.json()["data"]["items"]
    assert [i["bucket"] for i in items] == ["2030-06-01", "2030-06-02"]
    assert items[0]["pv_count"] == 2
    assert items[0]["question_count"] == 1
    assert items[0]["success_rate"] == 0.5
    assert items[0]["avg_latency_ms"] == 100.0
    assert items[0]["token_total"] == 100
    assert items[0]["token_coverage_rate"] == 0.5
    assert items[1]["pv_count"] == 1
    assert items[1]["success_rate"] == 1.0

    # 小时粒度：3 条分布在 3 个不同小时
    resp2 = await client.get(
        "/api/v1/admin/dashboard/trends",
        params={**WINDOW, "granularity": "hour"},
        headers=await bearer_headers(token),
    )
    assert resp2.status_code == 200, resp2.text
    items2 = resp2.json()["data"]["items"]
    assert len(items2) == 3
    assert items2[0]["bucket"] == "2030-06-01T08:00:00+00:00"
    assert items2[0]["pv_count"] == 1


async def test_trends_empty_range(client, db_session, admin_user, dashboard_cleanup):
    token = await _admin_token(client, admin_user)
    resp = await client.get(
        "/api/v1/admin/dashboard/trends",
        params={"date_from": "2030-01-01", "date_to": "2030-01-31"},
        headers=await bearer_headers(token),
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["granularity"] == "day"
    assert data["items"] == []


async def test_top_questions_ordering(
    client, db_session, admin_user, dashboard_cleanup
):
    """top-questions 基于真实问题日志聚合，按频次降序，sample 为最近一次。"""
    token = await _admin_token(client, admin_user)
    session_ids, turn_ids = await _seed_logs(
        db_session,
        user_id=admin_user["user_id"],
        specs=[
            # "如何做风险测评"出现 3 次（归一化一致，原始文本有标点/空白差异）
            dict(question="如何做风险测评？", created_at=BASE),
            dict(question="如何做风险测评", created_at=BASE + timedelta(hours=1)),
            dict(question=" 如何做风险测评 ", created_at=BASE + timedelta(hours=2)),
            # "开户需要什么"出现 2 次
            dict(question="开户需要什么", created_at=BASE + timedelta(hours=3)),
            dict(question="开户需要什么", created_at=BASE + timedelta(hours=4)),
            # "怎么销户"出现 1 次
            dict(question="怎么销户", created_at=BASE + timedelta(hours=5)),
        ],
    )
    resp = await client.get(
        "/api/v1/admin/dashboard/top-questions",
        params=WINDOW,
        headers=await bearer_headers(token),
    )
    assert resp.status_code == 200, resp.text
    items = resp.json()["data"]
    assert [i["ask_count"] for i in items] == [3, 2, 1]
    assert items[0]["normalized_question"] == normalize_question("如何做风险测评")
    # sample_question 是最近一次（created_at 最大）原始问题
    assert items[0]["sample_question"] == " 如何做风险测评 "
    assert items[1]["normalized_question"] == normalize_question("开户需要什么")

    # limit 生效
    resp2 = await client.get(
        "/api/v1/admin/dashboard/top-questions",
        params={**WINDOW, "limit": 2},
        headers=await bearer_headers(token),
    )
    assert resp2.status_code == 200, resp2.text
    assert len(resp2.json()["data"]) == 2


async def test_top_documents_from_real_citation(
    client, db_session, admin_user, dashboard_cleanup
):
    """top-documents 基于真实 Citation document IDs 聚合，含文档名关联。"""
    token = await _admin_token(client, admin_user)
    session_ids, turn_ids = await _seed_logs(
        db_session,
        user_id=admin_user["user_id"],
        specs=[
            dict(
                question="q1",
                citation_document_ids=["doc-A", "doc-B"],
                created_at=BASE,
            ),
            dict(
                question="q2",
                citation_document_ids=["doc-A"],
                created_at=BASE + timedelta(hours=1),
            ),
            dict(
                question="q3",
                citation_document_ids=["doc-B", "doc-C"],
                created_at=BASE + timedelta(hours=2),
            ),
            dict(
                question="q4",
                citation_document_ids=[],
                created_at=BASE + timedelta(hours=3),
            ),
        ],
    )
    # 关联一条平台文档映射（doc-A 有 file_name）
    from app.models.managed_document import ManagedDocument

    doc = ManagedDocument(
        rag_document_id="doc-A",
        rag_dataset_id="ds-internal",
        knowledge_scope="internal_shared",
        file_name="风险测评指南.pdf",
        source_kind="manual_upload",
        index_version=1,
        rag_status="succeeded",
        platform_status="active",
        created_by_user_id=admin_user["user_id"],
    )
    db_session.add(doc)
    await db_session.commit()
    try:
        resp = await client.get(
            "/api/v1/admin/dashboard/top-documents",
            params=WINDOW,
            headers=await bearer_headers(token),
        )
        assert resp.status_code == 200, resp.text
        items = resp.json()["data"]
        # 引用次数：doc-A=2, doc-B=2, doc-C=1；同频按 document_id 升序
        assert [i["document_id"] for i in items] == ["doc-A", "doc-B", "doc-C"]
        assert [i["citation_count"] for i in items] == [2, 2, 1]
        assert items[0]["file_name"] == "风险测评指南.pdf"
        assert items[1]["file_name"] is None
    finally:
        await db_session.execute(
            delete(ManagedDocument).where(ManagedDocument.id == doc.id)
        )
        await db_session.commit()


async def test_employee_forbidden(client, db_session, tracked_users):
    """employee 访问 Dashboard → 403 PERMISSION_DENIED。"""
    from tests.integration.conftest import create_user_record

    emp = await create_user_record(
        db_session,
        username="dash_emp",
        display_name="员工",
        role="employee",
        password="EmpPass#2026",
    )
    tracked_users.append(emp.id)
    await db_session.commit()
    resp = await api_login(client, "dash_emp", "EmpPass#2026")
    token = resp.json()["data"]["access_token"]
    for path in (
        "/api/v1/admin/dashboard/summary",
        "/api/v1/admin/dashboard/trends",
        "/api/v1/admin/dashboard/top-questions",
        "/api/v1/admin/dashboard/top-documents",
    ):
        r = await client.get(path, headers=await bearer_headers(token))
        assert r.status_code == 403, f"{path} -> {r.status_code}"


async def test_rank_limit_and_invalid_params(
    client, db_session, admin_user, dashboard_cleanup
):
    """limit 上限（>100 → 422）与非法日期（→ 422）参数校验。"""
    token = await _admin_token(client, admin_user)
    r = await client.get(
        "/api/v1/admin/dashboard/top-questions",
        params={"limit": 101},
        headers=await bearer_headers(token),
    )
    assert r.status_code == 422
    r2 = await client.get(
        "/api/v1/admin/dashboard/top-documents",
        params={"limit": 101},
        headers=await bearer_headers(token),
    )
    assert r2.status_code == 422
    r3 = await client.get(
        "/api/v1/admin/dashboard/summary",
        params={"date_from": "not-a-date"},
        headers=await bearer_headers(token),
    )
    assert r3.status_code == 422
    r4 = await client.get(
        "/api/v1/admin/dashboard/trends",
        params={"granularity": "minute"},
        headers=await bearer_headers(token),
    )
    assert r4.status_code == 422
