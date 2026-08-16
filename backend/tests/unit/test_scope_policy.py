"""scope_policy 单元测试：锁死角色 → 范围 → Dataset → 服务身份矩阵（冻结 §5.2）。"""

import pytest

import app.rag.scope_policy as scope_policy
from app.rag.scope_policy import (
    VALID_SCOPES,
    dataset_id_for_scope,
    dataset_ids_for_role,
    member_service_users_for_scope,
    scopes_for_role,
    service_user_for_role,
)


class FakeSettings:
    """与 Settings 字段同构的桩（scope_policy 只读这几个配置）。"""

    rag_external_dataset_id = "ext_dataset"
    rag_internal_dataset_id = "int_dataset"
    rag_admin_dataset_id = "adm_dataset"
    rag_service_user_admin = "svc_admin"
    rag_service_user_employee = "svc_employee"
    rag_service_user_external = "svc_external"


@pytest.fixture(autouse=True)
def _fake_settings(monkeypatch):
    monkeypatch.setattr(scope_policy, "get_settings", lambda: FakeSettings())


class TestScopesForRole:
    def test_admin_three_tiers_in_order(self):
        # admin 三档，且顺序正确（admin_private > internal_shared > external_public）
        assert scopes_for_role("admin") == [
            "admin_private",
            "internal_shared",
            "external_public",
        ]

    def test_employee_two_tiers_in_order(self):
        assert scopes_for_role("employee") == ["internal_shared", "external_public"]

    def test_external_one_tier(self):
        assert scopes_for_role("external") == ["external_public"]

    def test_unknown_role_returns_empty(self):
        assert scopes_for_role("superuser") == []


class TestDatasetIdMapping:
    def test_scope_to_dataset_id(self):
        assert dataset_id_for_scope("external_public") == "ext_dataset"
        assert dataset_id_for_scope("internal_shared") == "int_dataset"
        assert dataset_id_for_scope("admin_private") == "adm_dataset"

    def test_dataset_ids_for_role(self):
        assert dataset_ids_for_role("admin") == ["adm_dataset", "int_dataset", "ext_dataset"]
        assert dataset_ids_for_role("employee") == ["int_dataset", "ext_dataset"]
        assert dataset_ids_for_role("external") == ["ext_dataset"]

    def test_unknown_scope_raises(self):
        with pytest.raises(ValueError):
            dataset_id_for_scope("no_such_scope")

    def test_valid_scopes_are_all_three(self):
        assert VALID_SCOPES == ["admin_private", "internal_shared", "external_public"]


class TestServiceUserMapping:
    def test_service_user_per_role(self):
        assert service_user_for_role("admin") == "svc_admin"
        assert service_user_for_role("employee") == "svc_employee"
        assert service_user_for_role("external") == "svc_external"

    def test_unknown_role_raises(self):
        with pytest.raises(ValueError):
            service_user_for_role("guest")


class TestMemberMatrix:
    """member_service_users_for_scope：由范围矩阵反推，owner（admin 服务身份）不入 members。"""

    def test_admin_private_only_owner_no_member(self):
        # admin_private 只有 admin 能访问；admin 是 owner，因此无显式成员
        assert member_service_users_for_scope("admin_private", owner_user_id="svc_admin") == []

    def test_internal_shared_has_employee_member(self):
        members = member_service_users_for_scope("internal_shared", owner_user_id="svc_admin")
        assert members == [("svc_employee", "viewer")]

    def test_external_public_has_employee_and_external(self):
        members = member_service_users_for_scope("external_public", owner_user_id="svc_admin")
        assert members == [("svc_employee", "viewer"), ("svc_external", "viewer")]

    def test_employee_never_gets_admin_private(self):
        # employee 的服务身份绝不能出现在 admin_private 的成员里
        for owner in ("svc_admin", "someone_else"):
            users = {
                u for u, _r in member_service_users_for_scope("admin_private", owner_user_id=owner)
            }
            assert "svc_employee" not in users

    def test_external_only_external_public(self):
        # external 服务身份只出现在 external_public
        ext_in = {
            scope
            for scope in VALID_SCOPES
            for u, _r in member_service_users_for_scope(scope, owner_user_id="svc_admin")
            if u == "svc_external"
        }
        assert ext_in == {"external_public"}
