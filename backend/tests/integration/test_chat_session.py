"""Stage 4 会话 CRUD / 权限集成测试（真实 DB + 进程内 FakeQueryRag）。"""

import json

from app.models.audit_log import AuditLog
from tests.integration.conftest import (
    _unique,
    api_login,
    bearer_headers,
    create_user_record,
)
from tests.integration.fake_query_rag_server import FakeQueryRag

SSE_STREAM_PATH = "/api/v1/chat/sessions/{sid}/messages:stream"


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


async def _login(client, username: str, password: str) -> str:
    resp = await api_login(client, username, password)
    assert resp.status_code == 200, resp.text
    return resp.json()["data"]["access_token"]


async def _create_session(client, token: str) -> str:
    resp = await client.post(
        "/api/v1/chat/sessions",
        headers=await bearer_headers(token),
        json={},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["data"]["id"]


async def _make_employee(db_session, tracked_users, client) -> tuple[str, str]:
    username = _unique("it_emp")
    password = "TestEmp#2026"
    user = await create_user_record(
        db_session,
        username=username,
        display_name="集成测试员工",
        role="employee",
        password=password,
    )
    user_id = user.id
    tracked_users.append(user_id)
    await db_session.commit()
    token = await _login(client, username, password)
    return user_id, token


class TestSessionCrud:
    async def test_create_list_detail_patch_delete(
        self, client, admin_user, db_session, chat_rag_factory
    ):
        chat_rag_factory(FakeQueryRag(), db_session)
        token = await _login(client, admin_user["username"], admin_user["password"])

        # 新建
        resp = await client.post(
            "/api/v1/chat/sessions", headers=await bearer_headers(token), json={}
        )
        assert resp.status_code == 201
        session_id = resp.json()["data"]["id"]
        data = resp.json()["data"]
        assert data["title"] == "新会话"
        assert data["status"] == "active"
        assert data["channel"] == "internal_web"

        # 列表
        resp = await client.get("/api/v1/chat/sessions", headers=await bearer_headers(token))
        assert resp.status_code == 200
        items = resp.json()["data"]["items"]
        assert any(item["id"] == session_id for item in items)

        # 详情（空消息）
        resp = await client.get(
            f"/api/v1/chat/sessions/{session_id}", headers=await bearer_headers(token)
        )
        assert resp.status_code == 200
        assert resp.json()["data"]["session"]["id"] == session_id
        assert resp.json()["data"]["messages"] == []

        # PATCH 标题
        resp = await client.patch(
            f"/api/v1/chat/sessions/{session_id}",
            headers=await bearer_headers(token),
            json={"title": "关于双录的咨询"},
        )
        assert resp.status_code == 200
        assert resp.json()["data"]["title"] == "关于双录的咨询"

        # PATCH 归档
        resp = await client.patch(
            f"/api/v1/chat/sessions/{session_id}",
            headers=await bearer_headers(token),
            json={"status": "archived"},
        )
        assert resp.status_code == 200
        assert resp.json()["data"]["status"] == "archived"

        # 审计记录存在（归档 + 改标题）
        from sqlalchemy import select

        audit_count = await db_session.scalar(
            select(AuditLog).where(AuditLog.resource_id == session_id).limit(1)
        )
        assert audit_count is not None

        # DELETE 软删除
        resp = await client.delete(
            f"/api/v1/chat/sessions/{session_id}", headers=await bearer_headers(token)
        )
        assert resp.status_code == 200

        # 删除后 404
        resp = await client.get(
            f"/api/v1/chat/sessions/{session_id}", headers=await bearer_headers(token)
        )
        assert resp.status_code == 404

    async def test_user_a_cannot_read_user_b_session(
        self, client, db_session, tracked_users, chat_rag_factory
    ):
        chat_rag_factory(FakeQueryRag(), db_session)
        user_a, token_a = await _make_employee(db_session, tracked_users, client)
        user_b, token_b = await _make_employee(db_session, tracked_users, client)
        session_b = await _create_session(client, token_b)

        # A 无法读取 B 的会话（404，不泄漏存在性）
        for method in ("GET", "PATCH", "DELETE"):
            kwargs = {"json": {}} if method == "PATCH" else {}
            resp = await getattr(client, method.lower())(
                f"/api/v1/chat/sessions/{session_b}",
                headers=await bearer_headers(token_a),
                **kwargs,
            )
            assert resp.status_code == 404, method
        # A 也无法对 B 的会话发问（404）
        resp = await client.post(
            SSE_STREAM_PATH.format(sid=session_b),
            headers=await bearer_headers(token_a),
            json={"question": "客户如何办理风险测评？"},
        )
        assert resp.status_code == 404

    async def test_admin_cannot_read_employee_session(
        self, client, db_session, tracked_users, admin_user, chat_rag_factory
    ):
        chat_rag_factory(FakeQueryRag(), db_session)
        _, emp_token = await _make_employee(db_session, tracked_users, client)
        session_id = await _create_session(client, emp_token)

        admin_token = await _login(client, admin_user["username"], admin_user["password"])
        resp = await client.get(
            f"/api/v1/chat/sessions/{session_id}", headers=await bearer_headers(admin_token)
        )
        assert resp.status_code == 404

    async def test_archived_session_cannot_ask(
        self, client, admin_user, db_session, chat_rag_factory
    ):
        chat_rag_factory(FakeQueryRag(), db_session)
        token = await _login(client, admin_user["username"], admin_user["password"])
        session_id = await _create_session(client, token)
        resp = await client.patch(
            f"/api/v1/chat/sessions/{session_id}",
            headers=await bearer_headers(token),
            json={"status": "archived"},
        )
        assert resp.status_code == 200
        resp = await client.post(
            SSE_STREAM_PATH.format(sid=session_id),
            headers=await bearer_headers(token),
            json={"question": "客户如何办理风险测评？"},
        )
        assert resp.status_code == 409
        assert resp.json()["error"]["code"] == "RESOURCE_CONFLICT"

    async def test_question_extra_fields_rejected_422(
        self, client, admin_user, db_session, chat_rag_factory
    ):
        chat_rag_factory(FakeQueryRag(), db_session)
        token = await _login(client, admin_user["username"], admin_user["password"])
        session_id = await _create_session(client, token)
        for extra in ("knowledge_scope", "dataset_id", "dataset_ids", "role", "rag_service_user"):
            resp = await client.post(
                SSE_STREAM_PATH.format(sid=session_id),
                headers=await bearer_headers(token),
                json={"question": "客户如何办理风险测评？", extra: "xxx"},
            )
            assert resp.status_code == 422, extra

    async def test_empty_question_400(self, client, admin_user, db_session, chat_rag_factory):
        chat_rag_factory(FakeQueryRag(), db_session)
        token = await _login(client, admin_user["username"], admin_user["password"])
        session_id = await _create_session(client, token)
        resp = await client.post(
            SSE_STREAM_PATH.format(sid=session_id),
            headers=await bearer_headers(token),
            json={"question": "？？？？"},
        )
        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == "EMPTY_QUESTION"

    async def test_requires_auth(self, client, db_session, chat_rag_factory):
        chat_rag_factory(FakeQueryRag(), db_session)
        resp = await client.get("/api/v1/chat/sessions")
        assert resp.status_code == 401
