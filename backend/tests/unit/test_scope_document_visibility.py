"""scope → Dataset → visibility 映射单测（Stage 3 硬决策 §六/§七）。"""

import pytest

import app.rag.scope_policy as scope_policy
from app.rag.scope_policy import (
    dataset_id_for_scope,
    document_visibility_for_scope,
)


class FakeSettings:
    rag_external_dataset_id = "ext_dataset"
    rag_internal_dataset_id = "int_dataset"
    rag_admin_dataset_id = "adm_dataset"
    rag_service_user_admin = "svc_admin"
    rag_service_user_employee = "svc_employee"
    rag_service_user_external = "svc_external"


@pytest.fixture(autouse=True)
def _fake_settings(monkeypatch):
    monkeypatch.setattr(scope_policy, "get_settings", lambda: FakeSettings())


class TestDocumentVisibility:
    def test_external_public_is_public(self):
        assert document_visibility_for_scope("external_public") == "public"
        assert dataset_id_for_scope("external_public") == "ext_dataset"

    def test_internal_shared_is_shared(self):
        assert document_visibility_for_scope("internal_shared") == "shared"
        assert dataset_id_for_scope("internal_shared") == "int_dataset"

    def test_admin_private_is_private(self):
        assert document_visibility_for_scope("admin_private") == "private"
        assert dataset_id_for_scope("admin_private") == "adm_dataset"

    def test_unknown_scope_raises(self):
        with pytest.raises(ValueError):
            document_visibility_for_scope("no_such_scope")
