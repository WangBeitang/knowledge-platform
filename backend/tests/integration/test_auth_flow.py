"""阶段 2 认证流集成测试（真实 DB）。

覆盖：登录/登出/me、停用旧 JWT 立即失效、重置密码旧 JWT 失效、
角色变更下一请求生效、最后 active admin 保护、审计落库、状态转换语义。
"""

import hashlib

import jwt as _jwt
import pytest

from app.core.config import get_settings
from app.core.enums import UserRole
from app.models.audit_log import AuditLog
from app.models.auth_session import AuthSession
from app.models.user import User
from tests.integration.conftest import _unique, api_login, bearer_headers


async def _create_user_via_api(
    client, token: str, *, username: str, role: str = "employee", password: str
) -> dict:
    resp = await client.post(
        "/api/v1/admin/users",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "username": username,
            "display_name": "测试用户",
            "role": role,
            "initial_password": password,
        },
    )
    assert resp.status_code == 201, f"创建用户失败: {resp.text}"
    return resp.json()["data"]


class TestLogin:
    async def test_login_success_and_session_created(self, client, db_session, admin_user):
        resp = await api_login(client, admin_user["username"], admin_user["password"])
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["token_type"] == "bearer"
        assert data["expires_in"] > 0
        assert data["user"]["role"] == "admin"
        assert data["user"]["status"] == "active"

        # auth_sessions 存在对应记录且只保存 jti 的 SHA-256（精确断言）
        payload = _jwt.decode(data["access_token"], get_settings().secret_key, algorithms=["HS256"])
        session = await db_session.get(AuthSession, payload["sid"])
        assert session is not None
        assert session.revoked_at is None
        assert session.jti_hash == hashlib.sha256(payload["jti"].encode("utf-8")).hexdigest()
        assert "jwt" not in session.jti_hash  # 只保存 SHA-256，不保存原令牌

    async def test_login_wrong_password(self, client, admin_user):
        resp = await api_login(client, admin_user["username"], "wrong-password")
        assert resp.status_code == 401
        assert resp.json()["error"]["code"] == "AUTH_REQUIRED"

    async def test_login_unknown_user(self, client):
        resp = await api_login(client, "no_such_user_xyz", "whatever")
        assert resp.status_code == 401
        assert resp.json()["error"]["code"] == "AUTH_REQUIRED"

    async def test_login_username_case_insensitive(self, client, admin_user):
        resp = await api_login(client, admin_user["username"].upper(), admin_user["password"])
        assert resp.status_code == 200


class TestMeAndLogout:
    async def test_me_returns_current_user(self, client, admin_user):
        token = (await api_login(client, admin_user["username"], admin_user["password"])).json()[
            "data"
        ]["access_token"]
        resp = await client.get("/api/v1/auth/me", headers=await bearer_headers(token))
        assert resp.status_code == 200
        assert resp.json()["data"]["username"] == admin_user["username"]

    async def test_me_without_token_401(self, client):
        resp = await client.get("/api/v1/auth/me")
        assert resp.status_code == 401

    async def test_logout_revokes_only_current_session(self, client, admin_user):
        token_a = (await api_login(client, admin_user["username"], admin_user["password"])).json()[
            "data"
        ]["access_token"]
        token_b = (await api_login(client, admin_user["username"], admin_user["password"])).json()[
            "data"
        ]["access_token"]

        logout = await client.post("/api/v1/auth/logout", headers=await bearer_headers(token_a))
        assert logout.status_code == 200
        assert logout.json()["data"]["ok"] is True

        # 被登出的 token 立即失效
        me = await client.get("/api/v1/auth/me", headers=await bearer_headers(token_a))
        assert me.status_code == 401
        # 其他会话不受影响（只撤销当前 sid）
        me_b = await client.get("/api/v1/auth/me", headers=await bearer_headers(token_b))
        assert me_b.status_code == 200

    async def test_logout_repeat_is_idempotent(self, client, admin_user):
        token = (await api_login(client, admin_user["username"], admin_user["password"])).json()[
            "data"
        ]["access_token"]
        first = await client.post("/api/v1/auth/logout", headers=await bearer_headers(token))
        assert first.status_code == 200
        # 已撤销后再调：token 已失效，按鉴权失败处理（不产生 500）
        second = await client.post("/api/v1/auth/logout", headers=await bearer_headers(token))
        assert second.status_code == 401


class TestUserLifecycle:
    async def test_admin_creates_employee(self, client, admin_user, tracked_users):
        token = (await api_login(client, admin_user["username"], admin_user["password"])).json()[
            "data"
        ]["access_token"]
        username = _unique("it_emp")
        created = await _create_user_via_api(
            client, token, username=username, role="employee", password="Emp@12345"
        )
        tracked_users.append(created["id"])
        assert created["username"] == username
        assert created["role"] == "employee"
        assert created["status"] == "active"

    async def test_create_user_username_lowercased_and_unique(
        self, client, admin_user, tracked_users
    ):
        token = (await api_login(client, admin_user["username"], admin_user["password"])).json()[
            "data"
        ]["access_token"]
        username = _unique("it_emp")
        created = await _create_user_via_api(
            client, token, username=username.upper(), password="Emp@12345"
        )
        tracked_users.append(created["id"])
        assert created["username"] == username.lower()

        dup = await client.post(
            "/api/v1/admin/users",
            headers=await bearer_headers(token),
            json={
                "username": username.lower(),
                "display_name": "重复",
                "role": "employee",
                "initial_password": "x@123456",
            },
        )
        assert dup.status_code == 409
        assert dup.json()["error"]["code"] == "RESOURCE_CONFLICT"

    async def test_create_user_invalid_role(self, client, admin_user):
        token = (await api_login(client, admin_user["username"], admin_user["password"])).json()[
            "data"
        ]["access_token"]
        resp = await client.post(
            "/api/v1/admin/users",
            headers=await bearer_headers(token),
            json={
                "username": "bad_role_user",
                "display_name": "x",
                "role": "superuser",
                "initial_password": "x@123456",
            },
        )
        assert resp.status_code == 422

    async def test_disabled_user_old_jwt_immediately_invalid(
        self, client, db_session, admin_user, tracked_users
    ):
        token = (await api_login(client, admin_user["username"], admin_user["password"])).json()[
            "data"
        ]["access_token"]
        username = _unique("it_emp")
        created = await _create_user_via_api(client, token, username=username, password="Emp@12345")
        tracked_users.append(created["id"])

        emp_token = (await api_login(client, username, "Emp@12345")).json()["data"]["access_token"]
        me = await client.get("/api/v1/auth/me", headers=await bearer_headers(emp_token))
        assert me.status_code == 200

        # 停用
        resp = await client.patch(
            f"/api/v1/admin/users/{created['id']}",
            headers=await bearer_headers(token),
            json={"status": "disabled"},
        )
        assert resp.status_code == 200
        assert resp.json()["data"]["status"] == "disabled"

        # 旧 JWT 下一次请求立即失效
        me_after = await client.get("/api/v1/auth/me", headers=await bearer_headers(emp_token))
        assert me_after.status_code == 401

        # 停用用户不能重新登录
        login_after = await api_login(client, username, "Emp@12345")
        assert login_after.status_code == 401

    async def test_reset_password_invalidates_old_jwt(self, client, admin_user, tracked_users):
        token = (await api_login(client, admin_user["username"], admin_user["password"])).json()[
            "data"
        ]["access_token"]
        username = _unique("it_emp")
        created = await _create_user_via_api(client, token, username=username, password="OldPass@1")
        tracked_users.append(created["id"])

        token_a = (await api_login(client, username, "OldPass@1")).json()["data"]["access_token"]
        assert (
            await client.get("/api/v1/auth/me", headers=await bearer_headers(token_a))
        ).status_code == 200

        # 重置密码
        reset = await client.post(
            f"/api/v1/admin/users/{created['id']}/reset-password",
            headers=await bearer_headers(token),
            json={"new_password": "NewPass@2"},
        )
        assert reset.status_code == 200

        # token A（旧 JWT）立即失效
        me_old = await client.get("/api/v1/auth/me", headers=await bearer_headers(token_a))
        assert me_old.status_code == 401

        # 新密码登录 → token B 有效
        token_b = (await api_login(client, username, "NewPass@2")).json()["data"]["access_token"]
        me_new = await client.get("/api/v1/auth/me", headers=await bearer_headers(token_b))
        assert me_new.status_code == 200

        # 旧密码不再有效
        assert (await api_login(client, username, "OldPass@1")).status_code == 401

    async def test_role_change_takes_effect_next_request(self, client, admin_user, tracked_users):
        admin_token = (
            await api_login(client, admin_user["username"], admin_user["password"])
        ).json()["data"]["access_token"]
        username_b = _unique("it_admin_b")
        admin_b = await _create_user_via_api(
            client, admin_token, username=username_b, role="admin", password="AdminB@123"
        )
        tracked_users.append(admin_b["id"])
        admin_b_token = (await api_login(client, username_b, "AdminB@123")).json()["data"][
            "access_token"
        ]

        # 修改前 admin B 可访问 /admin
        ok = await client.get("/api/v1/admin/users", headers=await bearer_headers(admin_b_token))
        assert ok.status_code == 200

        # admin A 把 admin B 改为 employee
        resp = await client.patch(
            f"/api/v1/admin/users/{admin_b['id']}",
            headers=await bearer_headers(admin_token),
            json={"role": "employee"},
        )
        assert resp.status_code == 200
        assert resp.json()["data"]["role"] == "employee"

        # 下一次请求立即按数据库新角色鉴权：拒绝
        denied = await client.get(
            "/api/v1/admin/users", headers=await bearer_headers(admin_b_token)
        )
        assert denied.status_code == 403
        assert denied.json()["error"]["code"] == "PERMISSION_DENIED"

    async def test_admin_cannot_disable_self(self, client, admin_user):
        token = (await api_login(client, admin_user["username"], admin_user["password"])).json()[
            "data"
        ]["access_token"]
        resp = await client.patch(
            f"/api/v1/admin/users/{admin_user['user_id']}",
            headers=await bearer_headers(token),
            json={"status": "disabled"},
        )
        assert resp.status_code == 409
        assert resp.json()["error"]["code"] == "RESOURCE_CONFLICT"

    async def test_self_demote_allowed_with_another_active_admin(
        self, client, admin_user, tracked_users
    ):
        admin_token = (
            await api_login(client, admin_user["username"], admin_user["password"])
        ).json()["data"]["access_token"]
        username_b = _unique("it_admin_b")
        admin_b = await _create_user_via_api(
            client, admin_token, username=username_b, role="admin", password="AdminB@123"
        )
        tracked_users.append(admin_b["id"])

        # admin A 将自己改为 employee：系统中仍有 admin B active → 允许
        resp = await client.patch(
            f"/api/v1/admin/users/{admin_user['user_id']}",
            headers=await bearer_headers(admin_token),
            json={"role": "employee"},
        )
        assert resp.status_code == 200
        assert resp.json()["data"]["role"] == "employee"

        # admin A 自己的 token 下一次请求失去 admin 权限
        denied = await client.get("/api/v1/admin/users", headers=await bearer_headers(admin_token))
        assert denied.status_code == 403


class TestAuditRecords:
    async def test_audit_logs_for_user_writes(self, client, db_session, admin_user, tracked_users):
        token = (await api_login(client, admin_user["username"], admin_user["password"])).json()[
            "data"
        ]["access_token"]
        username = _unique("it_emp")
        created = await _create_user_via_api(client, token, username=username, password="Emp@12345")
        tracked_users.append(created["id"])

        # 创建审计（HTTP 请求已提交；先结束本地事务快照，确保能看到新提交的审计行）
        await db_session.rollback()
        create_audit = await db_session.scalar(_audit_stmt("user_created", created["id"]))
        assert create_audit is not None
        assert create_audit.result == "succeeded"

        # 重置密码审计
        await client.post(
            f"/api/v1/admin/users/{created['id']}/reset-password",
            headers=await bearer_headers(token),
            json={"new_password": "NewPass@9"},
        )
        await db_session.rollback()
        reset_audit = await db_session.scalar(_audit_stmt("user_password_reset", created["id"]))
        assert reset_audit is not None
        assert reset_audit.request_id  # request_id 已落库


def _audit_stmt(action: str, resource_id: str):
    from sqlalchemy import select

    return (
        select(AuditLog)
        .where(AuditLog.action == action, AuditLog.resource_id == resource_id)
        .limit(1)
    )


@pytest.mark.parametrize("role", [UserRole.employee.value, UserRole.admin.value])
async def test_list_users_visibility(client, admin_user, role):
    token = (await api_login(client, admin_user["username"], admin_user["password"])).json()[
        "data"
    ]["access_token"]
    resp = await client.get("/api/v1/admin/users", headers=await bearer_headers(token))
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert "items" in data
    assert data["page"] >= 1


class TestStatusTransition:
    """用户启停时间状态转换语义（§五）。"""

    async def test_disable_writes_disabled_at_and_revokes_sessions(
        self, client, db_session, admin_user, tracked_users
    ):
        token = (await api_login(client, admin_user["username"], admin_user["password"])).json()[
            "data"
        ]["access_token"]
        username = _unique("it_emp")
        created = await _create_user_via_api(client, token, username=username, password="Emp@12345")
        tracked_users.append(created["id"])

        emp_token = (await api_login(client, username, "Emp@12345")).json()["data"]["access_token"]
        await db_session.rollback()
        user = await db_session.get(User, created["id"])
        assert user.disabled_at is None

        resp = await client.patch(
            f"/api/v1/admin/users/{created['id']}",
            headers=await bearer_headers(token),
            json={"status": "disabled"},
        )
        assert resp.status_code == 200
        # 旧 JWT 立即失效（会话已撤销）
        assert (
            await client.get("/api/v1/auth/me", headers=await bearer_headers(emp_token))
        ).status_code == 401

        await db_session.rollback()
        user = await db_session.get(User, created["id"])
        assert user.disabled_at is not None  # active → disabled 写入 disabled_at

    async def test_reenable_clears_disabled_at(self, client, db_session, admin_user, tracked_users):
        token = (await api_login(client, admin_user["username"], admin_user["password"])).json()[
            "data"
        ]["access_token"]
        username = _unique("it_emp")
        created = await _create_user_via_api(client, token, username=username, password="Emp@12345")
        tracked_users.append(created["id"])

        await client.patch(
            f"/api/v1/admin/users/{created['id']}",
            headers=await bearer_headers(token),
            json={"status": "disabled"},
        )
        await db_session.rollback()
        user = await db_session.get(User, created["id"])
        assert user.disabled_at is not None
        disabled_at_before = user.disabled_at

        await client.patch(
            f"/api/v1/admin/users/{created['id']}",
            headers=await bearer_headers(token),
            json={"status": "active"},
        )
        await db_session.rollback()
        user = await db_session.get(User, created["id"])
        assert user.status == "active"
        assert user.disabled_at is None  # disabled → active 清除 disabled_at

        # 记录确实发生过（不是初始 None）
        assert disabled_at_before is not None

    async def test_disabled_user_display_name_does_not_rewrite_disabled_at(
        self, client, db_session, admin_user, tracked_users
    ):
        token = (await api_login(client, admin_user["username"], admin_user["password"])).json()[
            "data"
        ]["access_token"]
        username = _unique("it_emp")
        created = await _create_user_via_api(client, token, username=username, password="Emp@12345")
        tracked_users.append(created["id"])

        await client.patch(
            f"/api/v1/admin/users/{created['id']}",
            headers=await bearer_headers(token),
            json={"status": "disabled"},
        )
        await db_session.rollback()
        user = await db_session.get(User, created["id"])
        assert user.disabled_at is not None
        first_disabled_at = user.disabled_at

        # disabled → disabled：只改 display_name，不得重写 disabled_at、不得制造新“停用”时间
        resp = await client.patch(
            f"/api/v1/admin/users/{created['id']}",
            headers=await bearer_headers(token),
            json={"display_name": "新展示名"},
        )
        assert resp.status_code == 200
        assert resp.json()["data"]["status"] == "disabled"

        await db_session.rollback()
        user = await db_session.get(User, created["id"])
        assert user.disabled_at == first_disabled_at
        assert user.display_name == "新展示名"
