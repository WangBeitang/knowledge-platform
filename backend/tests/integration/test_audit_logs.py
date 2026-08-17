"""Stage 5 Batch 3：审计查询集成测试（真实 DB/Redis + 进程内直连）。

覆盖验收重点：
- 分页（page/page_size/total）正确；默认 created_at 降序、asc 反转；
- 筛选：action / resource_type / result / operator_user_id / 日期范围；
- 排序白名单外字段安全回退（不报错）；
- operator_username 关联正确；
- 审计响应无敏感字段泄漏：查询侧兜底打码（password/JWT/Key/连接串）；
- employee 访问 /admin/audit-logs → 403。
"""

import uuid
from datetime import datetime, timedelta

import pytest
from sqlalchemy import delete

from app.models.audit_log import AuditLog
from tests.integration.conftest import api_login, bearer_headers

# 固定 UTC 测试时刻：未来唯一时间窗（2030-06），避免与库中历史审计
# 记录（smoke 遗留）或并发测试互相污染；断言型查询均显式传日期范围。
BASE = datetime(2030, 6, 1, 8, 0, 0)
WINDOW = {"date_from": "2030-06-01", "date_to": "2030-06-30"}


async def _admin_token(client, admin_user) -> str:
    resp = await api_login(client, admin_user["username"], admin_user["password"])
    return resp.json()["data"]["access_token"]


async def _seed_audit(db_session, *, operator_user_id: str, specs: list[dict]):
    """直接插入多条审计记录，返回 (ids, request_ids)。"""
    ids: list[str] = []
    for i, spec in enumerate(specs):
        record = AuditLog(
            id=str(uuid.uuid4()),
            request_id=spec.get("request_id", str(uuid.uuid4())),
            operator_user_id=operator_user_id,
            action=spec.get("action", "document_import"),
            resource_type=spec.get("resource_type", "document"),
            resource_id=spec.get("resource_id"),
            result=spec.get("result", "succeeded"),
            before_json=spec.get("before"),
            after_json=spec.get("after"),
            error_code=spec.get("error_code"),
            client_ip=spec.get("client_ip", "127.0.0.1"),
            created_at=spec.get("created_at", BASE + timedelta(minutes=i)),
        )
        db_session.add(record)
        ids.append(record.id)
    await db_session.commit()
    return ids


@pytest.fixture
async def audit_cleanup(db_session):
    ids: list[str] = []
    yield ids
    if ids:
        await db_session.execute(delete(AuditLog).where(AuditLog.id.in_(ids)))
        await db_session.commit()


async def test_audit_pagination_and_sorting(client, db_session, admin_user, audit_cleanup):
    """分页正确；默认 created_at 降序；sort_order=asc 反转。"""
    token = await _admin_token(client, admin_user)
    ids = await _seed_audit(
        db_session,
        operator_user_id=admin_user["user_id"],
        specs=[
            dict(action="user_created", resource_type="user"),
            dict(action="dataset_bootstrap", resource_type="dataset"),
            dict(action="document_import", resource_type="document"),
        ],
    )
    audit_cleanup.extend(ids)
    resp = await client.get(
        "/api/v1/admin/audit-logs",
        params={**WINDOW, "page": 1, "page_size": 2},
        headers=await bearer_headers(token),
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["total"] == 3
    assert data["page"] == 1
    assert data["page_size"] == 2
    assert len(data["items"]) == 2
    # 默认 created_at 降序：第 3 条（document_import，最晚）在前
    assert data["items"][0]["action"] == "document_import"
    # 第二页
    resp2 = await client.get(
        "/api/v1/admin/audit-logs",
        params={**WINDOW, "page": 2, "page_size": 2},
        headers=await bearer_headers(token),
    )
    assert resp2.status_code == 200, resp2.text
    items2 = resp2.json()["data"]["items"]
    assert len(items2) == 1
    assert items2[0]["action"] == "user_created"
    # asc 反转
    resp3 = await client.get(
        "/api/v1/admin/audit-logs",
        params={**WINDOW, "sort_order": "asc"},
        headers=await bearer_headers(token),
    )
    assert resp3.status_code == 200, resp3.text
    assert resp3.json()["data"]["items"][0]["action"] == "user_created"


async def test_audit_filters(client, db_session, admin_user, audit_cleanup):
    """按 action / result / resource_type / operator / 日期范围筛选。"""
    token = await _admin_token(client, admin_user)
    ids = await _seed_audit(
        db_session,
        operator_user_id=admin_user["user_id"],
        specs=[
            dict(
                action="document_import",
                resource_type="document",
                result="succeeded",
                created_at=BASE,
            ),
            dict(
                action="document_delete",
                resource_type="document",
                result="succeeded",
                created_at=BASE + timedelta(hours=1),
            ),
            dict(
                action="chunk_status_changed",
                resource_type="chunk",
                result="failed",
                error_code="INDEX_VERSION_CONFLICT",
                created_at=BASE + timedelta(hours=5),
            ),
        ],
    )
    audit_cleanup.extend(ids)
    # action 筛选
    r = await client.get(
        "/api/v1/admin/audit-logs",
        params={**WINDOW, "action": "document_delete"},
        headers=await bearer_headers(token),
    )
    assert r.status_code == 200, r.text
    assert r.json()["data"]["total"] == 1
    assert r.json()["data"]["items"][0]["action"] == "document_delete"
    # result 筛选
    r2 = await client.get(
        "/api/v1/admin/audit-logs",
        params={**WINDOW, "result": "failed"},
        headers=await bearer_headers(token),
    )
    assert r2.status_code == 200, r2.text
    assert r2.json()["data"]["total"] == 1
    assert r2.json()["data"]["items"][0]["error_code"] == "INDEX_VERSION_CONFLICT"
    # resource_type 筛选
    r3 = await client.get(
        "/api/v1/admin/audit-logs",
        params={**WINDOW, "resource_type": "chunk"},
        headers=await bearer_headers(token),
    )
    assert r3.status_code == 200, r3.text
    assert r3.json()["data"]["total"] == 1
    # 日期范围（06-01 12:00 之后 → 只剩 chunk 一条）
    r4 = await client.get(
        "/api/v1/admin/audit-logs",
        params={"date_from": "2030-06-01T12:00:00"},
        headers=await bearer_headers(token),
    )
    assert r4.status_code == 200, r4.text
    assert r4.json()["data"]["total"] == 1
    assert r4.json()["data"]["items"][0]["action"] == "chunk_status_changed"
    # operator_user_id 筛选（用不存在的 id → 0 条，验证参数生效）
    r5 = await client.get(
        "/api/v1/admin/audit-logs",
        params={**WINDOW, "operator_user_id": "no-such-user"},
        headers=await bearer_headers(token),
    )
    assert r5.status_code == 200, r5.text
    assert r5.json()["data"]["total"] == 0


async def test_audit_operator_username(client, db_session, admin_user, audit_cleanup):
    """operator_username 关联 users 表正确返回。"""
    token = await _admin_token(client, admin_user)
    ids = await _seed_audit(
        db_session,
        operator_user_id=admin_user["user_id"],
        specs=[dict(action="faq_sync_retried", resource_type="faq_sync_run")],
    )
    audit_cleanup.extend(ids)
    resp = await client.get(
        "/api/v1/admin/audit-logs",
        params=WINDOW,
        headers=await bearer_headers(token),
    )
    assert resp.status_code == 200, resp.text
    item = resp.json()["data"]["items"][0]
    assert item["operator_user_id"] == admin_user["user_id"]
    assert item["operator_username"] == admin_user["username"]


async def test_audit_no_sensitive_leak(client, db_session, admin_user, audit_cleanup):
    """审计响应禁止泄露敏感字段：查询侧兜底打码。"""
    token = await _admin_token(client, admin_user)
    # 模拟历史脏数据：ORM 直接插入未打码的敏感快照
    ids = await _seed_audit(
        db_session,
        operator_user_id=admin_user["user_id"],
        specs=[
            dict(
                action="user_created",
                resource_type="user",
                before={
                    "password": "PlainPassword#1",
                    "jwt": "eyJhbGciOiJIUzI1NiJ9.fake",
                },
                after={
                    "service_api_key": "sk-live-123456",
                    "db_connection": "mysql://root:pwd@db:3306/kp",
                },
            )
        ],
    )
    audit_cleanup.extend(ids)
    resp = await client.get(
        "/api/v1/admin/audit-logs",
        params=WINDOW,
        headers=await bearer_headers(token),
    )
    assert resp.status_code == 200, resp.text
    body = resp.text
    assert "PlainPassword#1" not in body
    assert "eyJhbGciOiJIUzI1NiJ9.fake" not in body
    assert "sk-live-123456" not in body
    assert "mysql://root:pwd@db:3306/kp" not in body
    item = resp.json()["data"]["items"][0]
    assert item["before"]["password"] == "***"
    assert item["after"]["service_api_key"] == "***"


async def test_audit_invalid_sort_fallback(client, db_session, admin_user, audit_cleanup):
    """白名单外 sort_by 安全回退默认 created_at，不报错。"""
    token = await _admin_token(client, admin_user)
    ids = await _seed_audit(
        db_session,
        operator_user_id=admin_user["user_id"],
        specs=[dict(action="gap_ignored", resource_type="knowledge_gap")],
    )
    audit_cleanup.extend(ids)
    resp = await client.get(
        "/api/v1/admin/audit-logs",
        params={**WINDOW, "sort_by": "operator_user_id"},
        headers=await bearer_headers(token),
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["total"] == 1
    # 非法 result 参数 → 422
    r2 = await client.get(
        "/api/v1/admin/audit-logs",
        params={"result": "unknown"},
        headers=await bearer_headers(token),
    )
    assert r2.status_code == 422


async def test_audit_employee_forbidden(client, db_session, tracked_users):
    """employee 访问审计查询 → 403。"""
    from tests.integration.conftest import create_user_record

    emp = await create_user_record(
        db_session,
        username="audit_emp",
        display_name="员工",
        role="employee",
        password="EmpPass#2026",
    )
    tracked_users.append(emp.id)
    await db_session.commit()
    resp = await api_login(client, "audit_emp", "EmpPass#2026")
    token = resp.json()["data"]["access_token"]
    r = await client.get("/api/v1/admin/audit-logs", headers=await bearer_headers(token))
    assert r.status_code == 403
