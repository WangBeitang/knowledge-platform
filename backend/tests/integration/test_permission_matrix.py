"""阶段 2 权限矩阵集成测试（真实 DB）。

核心：employee 无法访问 /admin/*（403）；无 token 401；admin 可访问。
"""

from tests.integration.conftest import _unique, api_login, bearer_headers


async def _admin_token(client, admin_user) -> str:
    resp = await api_login(client, admin_user["username"], admin_user["password"])
    return resp.json()["data"]["access_token"]


class TestAdminOnlyEndpoints:
    async def test_no_token_401(self, client):
        for method, path in [
            ("get", "/api/v1/admin/users"),
            ("get", "/api/v1/admin/integration/rag/status"),
            ("post", "/api/v1/admin/integration/rag/bootstrap"),
        ]:
            resp = await client.request(method.upper(), path, json={} if method == "post" else None)
            assert resp.status_code == 401, f"{path} 应 401，实际 {resp.status_code}"

    async def test_employee_cannot_access_admin_users(self, client, admin_user, tracked_users):
        admin_token = await _admin_token(client, admin_user)
        username = _unique("it_emp")
        created = await client.post(
            "/api/v1/admin/users",
            headers=await bearer_headers(admin_token),
            json={
                "username": username,
                "display_name": "员工",
                "role": "employee",
                "initial_password": "Emp@12345",
            },
        )
        assert created.status_code == 201
        tracked_users.append(created.json()["data"]["id"])

        emp_token = (await api_login(client, username, "Emp@12345")).json()["data"]["access_token"]
        resp = await client.get("/api/v1/admin/users", headers=await bearer_headers(emp_token))
        assert resp.status_code == 403
        assert resp.json()["error"]["code"] == "PERMISSION_DENIED"

    async def test_employee_cannot_bootstrap(self, client, admin_user, tracked_users):
        admin_token = await _admin_token(client, admin_user)
        username = _unique("it_emp")
        created = await client.post(
            "/api/v1/admin/users",
            headers=await bearer_headers(admin_token),
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
            "/api/v1/admin/integration/rag/bootstrap",
            headers=await bearer_headers(emp_token),
            json={"verify_only": False},
        )
        assert resp.status_code == 403

    async def test_admin_can_access_admin_users(self, client, admin_user):
        admin_token = await _admin_token(client, admin_user)
        resp = await client.get("/api/v1/admin/users", headers=await bearer_headers(admin_token))
        assert resp.status_code == 200

    async def test_admin_can_access_rag_status(self, client, admin_user):
        admin_token = await _admin_token(client, admin_user)
        resp = await client.get(
            "/api/v1/admin/integration/rag/status", headers=await bearer_headers(admin_token)
        )
        # RAG 未配置/不可达时也应返回状态视图（逐档 failed/degraded），而不是 500
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert "datasets" in data
        assert len(data["datasets"]) == 3
