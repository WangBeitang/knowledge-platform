"""阶段 2 Dataset bootstrap 幂等集成测试（原 RAG 行为以 stub 模拟）。

覆盖：先查后建、重复执行不重复创建、成员 ensure 幂等、
verify_only=true 零写副作用、HTTP 端点真实调用。
"""

import json

import httpx
import pytest

from app.rag.rag_import_client import RagImportClient
from app.services.bootstrap_service import BootstrapService
from tests.integration.conftest import api_login, bearer_headers

# 测试注入的 MockTransport client 统一在用例结束后显式关闭，避免 ResourceWarning
_test_clients: list[RagImportClient] = []


@pytest.fixture(autouse=True)
async def _close_test_clients():
    yield
    while _test_clients:
        client = _test_clients.pop()
        await client.aclose()


class FakeRag:
    """模拟原 RAG 导入服务 Dataset/member 行为（对照已核查源码语义）。"""

    def __init__(self) -> None:
        self.datasets: dict[str, dict] = {}
        self.members: dict[tuple[str, str], dict] = {}
        self.create_calls = 0
        self.upsert_calls = 0

    def seed_dataset(self, dataset_id: str, *, owner_user_id: str = "svc_knowledge_admin") -> None:
        self.datasets[dataset_id] = {
            "dataset_id": dataset_id,
            "name": dataset_id,
            "owner_user_id": owner_user_id,
            "visibility": "private",
            "document_count": 0,
        }

    def seed_member(self, dataset_id: str, user_id: str, role: str) -> None:
        self.members[(dataset_id, user_id)] = {
            "dataset_id": dataset_id,
            "user_id": user_id,
            "role": role,
        }

    async def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        method = request.method
        if not request.headers.get("X-User-Id"):
            return httpx.Response(400, json={"detail": "缺少 X-User-Id 请求头"})

        parts = [p for p in path.split("/") if p]
        if method == "GET" and len(parts) == 1 and parts[0] == "datasets":
            # GET /datasets（列表）
            return httpx.Response(200, json={"code": 200, "items": list(self.datasets.values())})
        if method == "POST" and len(parts) == 1 and parts[0] == "datasets":
            # POST /datasets（创建；重复 dataset_id 由上游抛错，不幂等）
            body = json.loads(request.content)
            dataset_id = body["dataset_id"]
            if dataset_id in self.datasets:
                return httpx.Response(500, json={"detail": "duplicate dataset_id"})
            self.create_calls += 1
            self.datasets[dataset_id] = {
                "dataset_id": dataset_id,
                "name": body.get("name", ""),
                "visibility": body.get("visibility", "private"),
                "document_count": 0,
            }
            return httpx.Response(200, json=self.datasets[dataset_id])
        if method == "GET" and len(parts) == 2 and parts[0] == "datasets":
            # GET /datasets/{id}
            dataset_id = parts[1]
            if dataset_id not in self.datasets:
                return httpx.Response(404, json={"detail": "dataset 不存在"})
            return httpx.Response(200, json=self.datasets[dataset_id])
        if method == "GET" and len(parts) == 3 and parts[0] == "datasets" and parts[2] == "members":
            dataset_id = parts[1]
            if dataset_id not in self.datasets:
                return httpx.Response(404, json={"detail": "dataset 不存在"})
            items = [m for (ds, _u), m in self.members.items() if ds == dataset_id]
            return httpx.Response(200, json={"code": 200, "dataset_id": dataset_id, "items": items})
        if (
            method == "POST"
            and len(parts) == 3
            and parts[0] == "datasets"
            and parts[2] == "members"
        ):
            dataset_id = parts[1]
            if dataset_id not in self.datasets:
                return httpx.Response(404, json={"detail": "dataset 不存在"})
            body = json.loads(request.content)
            user_id = body["user_id"]
            if user_id == self.datasets[dataset_id].get("owner_user_id"):
                return httpx.Response(403, json={"detail": "owner 不能通过 members API 修改"})
            self.upsert_calls += 1
            self.members[(dataset_id, user_id)] = {
                "dataset_id": dataset_id,
                "user_id": user_id,
                "role": body.get("role", "viewer"),
            }
            return httpx.Response(200, json=self.members[(dataset_id, user_id)])
        return httpx.Response(404, json={"detail": "not found"})


def _fake_service(fake: FakeRag) -> BootstrapService:
    client = RagImportClient(
        base_url="http://rag-stub", transport=httpx.MockTransport(fake.handler)
    )
    _test_clients.append(client)
    return BootstrapService(client=client)


class TestBootstrapIdempotency:
    async def test_first_run_creates_then_second_run_idempotent(self):
        fake = FakeRag()
        service = _fake_service(fake)

        items1, overall1, result1 = await service.bootstrap_datasets(
            verify_only=False, operator_user_id="op-1"
        )
        assert overall1 == "succeeded"
        assert result1 == "succeeded"
        # admin_private 无显式成员（owner 即 admin 隐式 admin），成员校验天然通过
        assert {i.scope: (i.status, i.member_status) for i in items1} == {
            "external_public": ("created", "ensured"),
            "internal_shared": ("created", "ensured"),
            "admin_private": ("created", "verified"),
        }
        assert fake.create_calls == 3
        assert len(fake.datasets) == 3

        # 第二次执行：全部 existed/verified，不重复创建、不重复 upsert
        upsert_after_first = fake.upsert_calls
        items2, overall2, result2 = await service.bootstrap_datasets(
            verify_only=False, operator_user_id="op-1"
        )
        assert overall2 == "succeeded"
        assert {i.scope: (i.status, i.member_status) for i in items2} == {
            "external_public": ("existed", "verified"),
            "internal_shared": ("existed", "verified"),
            "admin_private": ("existed", "verified"),
        }
        assert fake.create_calls == 3  # 未重复创建
        assert fake.upsert_calls == upsert_after_first  # 成员已就绪，未重复 upsert

    async def test_verify_only_has_zero_write_side_effects(self):
        fake = FakeRag()
        service = _fake_service(fake)

        items, overall, _result = await service.bootstrap_datasets(
            verify_only=True, operator_user_id="op-1"
        )
        assert all(i.status == "missing" for i in items)
        assert all(i.member_status == "missing" for i in items)
        assert overall == "partial"  # missing 不是成功
        assert fake.create_calls == 0
        assert fake.upsert_calls == 0
        assert len(fake.datasets) == 0

    async def test_verify_only_after_real_bootstrap_reports_verified(self):
        fake = FakeRag()
        service = _fake_service(fake)
        await service.bootstrap_datasets(verify_only=False, operator_user_id="op-1")

        items, overall, _result = await service.bootstrap_datasets(
            verify_only=True, operator_user_id="op-1"
        )
        assert all(i.status == "existed" for i in items)
        assert all(i.member_status == "verified" for i in items)
        assert overall == "succeeded"
        # verify_only 不再产生任何写
        assert fake.upsert_calls == 3  # 仅第一次 bootstrap 的 3 次 upsert

    async def test_unknown_upstream_state_not_mapped_to_success(self):
        # 上游 500：必须标记 failed，不能伪造 succeeded
        async def failing_handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(500, text="<html>boom</html>")

        client = RagImportClient(
            base_url="http://rag-stub", transport=httpx.MockTransport(failing_handler)
        )
        _test_clients.append(client)
        service = BootstrapService(client=client)
        items, overall, result = await service.bootstrap_datasets(
            verify_only=False, operator_user_id="op-1"
        )
        assert all(i.status == "failed" for i in items)
        assert overall == "failed"
        assert result == "failed"


class TestBootstrapHttp:
    async def test_bootstrap_endpoint_full_flow(self, client, admin_user, monkeypatch):
        import app.api.v1.integration as integration_mod
        from app.repositories.audit_log_repository import AuditLogRepository
        from app.services.audit_service import AuditService

        fake = FakeRag()

        def fake_factory(session):
            client = RagImportClient(
                base_url="http://rag-stub", transport=httpx.MockTransport(fake.handler)
            )
            _test_clients.append(client)
            return BootstrapService(
                client=client,
                audit_service=AuditService(AuditLogRepository(session)),
            )

        monkeypatch.setattr(integration_mod, "_bootstrap_service", fake_factory)

        token = (await api_login(client, admin_user["username"], admin_user["password"])).json()[
            "data"
        ]["access_token"]

        first = await client.post(
            "/api/v1/admin/integration/rag/bootstrap",
            headers=await bearer_headers(token),
            json={"verify_only": False},
        )
        assert first.status_code == 200
        data = first.json()["data"]
        assert data["verify_only"] is False
        assert {i["scope"]: i["status"] for i in data["datasets"]} == {
            "external_public": "created",
            "internal_shared": "created",
            "admin_private": "created",
        }
        assert data["overall"] == "succeeded"

        second = await client.post(
            "/api/v1/admin/integration/rag/bootstrap",
            headers=await bearer_headers(token),
            json={"verify_only": False},
        )
        second_data = second.json()["data"]
        assert {i["scope"]: i["status"] for i in second_data["datasets"]} == {
            "external_public": "existed",
            "internal_shared": "existed",
            "admin_private": "existed",
        }
        assert fake.create_calls == 3  # HTTP 层面也不重复创建

        status = await client.get(
            "/api/v1/admin/integration/rag/status", headers=await bearer_headers(token)
        )
        assert status.status_code == 200
        assert {i["scope"]: i["status"] for i in status.json()["data"]["datasets"]} == {
            "external_public": "exists",
            "internal_shared": "exists",
            "admin_private": "exists",
        }


class TestMemberRoleVerification:
    """member 必须 user_id + role 都匹配才算 verified（§三）。"""

    @staticmethod
    def _seeded_fake_with_wrong_role() -> tuple[FakeRag, BootstrapService]:
        fake = FakeRag()
        for ds in (
            "securities_external_public",
            "securities_internal_shared",
            "securities_admin_private",
        ):
            fake.seed_dataset(ds)
        # internal_shared 成员正确（viewer）；external_public 成员 role 错误
        # （应为 viewer，实际 editor）
        fake.seed_member("securities_internal_shared", "svc_knowledge_employee", "viewer")
        fake.seed_member("securities_external_public", "svc_knowledge_employee", "editor")
        return fake, _fake_service(fake)

    async def test_wrong_role_not_verified_in_status(self):
        fake, service = self._seeded_fake_with_wrong_role()
        items, overall = await service.get_rag_status()
        by_scope = {i.scope: (i.status, i.member_status) for i in items}
        # external_public 存在但成员 role 不符 → member missing
        assert by_scope["external_public"] == ("exists", "missing")
        assert by_scope["internal_shared"] == ("exists", "verified")
        # 存在 Dataset 但成员不符 → overall 不是 ok
        assert overall == "partial"
        assert fake.upsert_calls == 0  # 状态查询零写

    async def test_verify_only_detects_wrong_role_but_does_not_fix(self):
        fake, service = self._seeded_fake_with_wrong_role()
        items, overall, _result = await service.bootstrap_datasets(
            verify_only=True, operator_user_id="op-1"
        )
        by_scope = {i.scope: (i.status, i.member_status) for i in items}
        assert by_scope["external_public"] == ("existed", "missing")
        assert overall == "partial"
        assert fake.upsert_calls == 0  # verify-only 不允许修正

    async def test_real_bootstrap_fixes_wrong_role(self):
        fake, service = self._seeded_fake_with_wrong_role()
        items, overall, _result = await service.bootstrap_datasets(
            verify_only=False, operator_user_id="op-1"
        )
        assert {i.scope: i.member_status for i in items}["external_public"] == "ensured"
        assert overall == "succeeded"
        # 上游成员已修正为 viewer
        assert (
            fake.members[("securities_external_public", "svc_knowledge_employee")]["role"]
            == "viewer"
        )

        # 再次校验 → verified
        items2, overall2, _r2 = await service.bootstrap_datasets(
            verify_only=True, operator_user_id="op-1"
        )
        assert {i.scope: i.member_status for i in items2}["external_public"] == "verified"
        assert overall2 == "succeeded"

    async def test_member_missing_means_overall_not_ok(self):
        fake = FakeRag()
        for ds in (
            "securities_external_public",
            "securities_internal_shared",
            "securities_admin_private",
        ):
            fake.seed_dataset(ds)
        # 不种任何成员：internal_shared/external_public 成员缺失
        service = _fake_service(fake)
        _items, overall = await service.get_rag_status()
        assert overall == "partial"


class TestImportBaseUrlConfigured:
    def test_configured_flag_follows_real_config(self, monkeypatch):
        import app.services.bootstrap_service as mod

        class FakeSettings:
            rag_import_base_url = ""

        monkeypatch.setattr(mod, "get_settings", lambda: FakeSettings())
        assert mod.is_import_base_url_configured() is False

        FakeSettings.rag_import_base_url = "http://127.0.0.1:8011"
        assert mod.is_import_base_url_configured() is True

        FakeSettings.rag_import_base_url = "   "
        assert mod.is_import_base_url_configured() is False


class TestDefaultSharedClient:
    def test_bootstrap_service_defaults_to_shared_client(self, monkeypatch):
        import app.services.bootstrap_service as mod
        from app.rag import rag_import_client as ric

        monkeypatch.setattr(ric, "_shared_client", None)
        fake_client = object()

        def fake_get() -> object:
            return fake_client

        monkeypatch.setattr(mod, "get_rag_import_client", fake_get)
        service = BootstrapService()
        assert service.client is fake_client  # 正常应用路径复用共享 client
