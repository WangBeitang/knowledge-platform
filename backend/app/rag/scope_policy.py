"""身份 → 知识范围 → Dataset → RAG 服务身份的单一事实来源（冻结 §5.2 / 数据对象 §8）。

规则：
- `scopes_for_role` 返回的顺序即 FAQ 匹配优先级
  （admin_private > internal_shared > external_public）；
- `dataset_ids_for_role` 把范围映射为 env 中的 Dataset ID；
- `service_user_for_role` 返回固定上游服务身份（X-User-Id）。

禁止 route/service/frontend 自行重写本矩阵。
"""

from app.core.config import get_settings
from app.core.enums import KnowledgeScope, UserRole

# 角色 → 范围（顺序即匹配优先级）
SCOPES_FOR_ROLE: dict[str, list[str]] = {
    UserRole.admin.value: [
        KnowledgeScope.admin_private.value,
        KnowledgeScope.internal_shared.value,
        KnowledgeScope.external_public.value,
    ],
    UserRole.employee.value: [
        KnowledgeScope.internal_shared.value,
        KnowledgeScope.external_public.value,
    ],
    # external 不是平台登录角色，仅外部服务固定范围
    "external": [KnowledgeScope.external_public.value],
}

# 范围 → Dataset 配置键（env 字段名）
_DATASET_CONFIG_KEYS: dict[str, str] = {
    KnowledgeScope.external_public.value: "rag_external_dataset_id",
    KnowledgeScope.internal_shared.value: "rag_internal_dataset_id",
    KnowledgeScope.admin_private.value: "rag_admin_dataset_id",
}

# 范围 → 原 RAG Document visibility（Stage 3 硬决策 §六）
# 原 RAG Retrieval 不仅过滤 Dataset，还过滤 public / shared+tenant / owner，
# 因此平台导入时必须显式指定与范围一致的 visibility。
_VISIBILITY_FOR_SCOPE: dict[str, str] = {
    KnowledgeScope.external_public.value: "public",
    KnowledgeScope.internal_shared.value: "shared",
    KnowledgeScope.admin_private.value: "private",
}

# 角色 → 上游固定服务身份配置键
_SERVICE_USER_CONFIG_KEYS: dict[str, str] = {
    UserRole.admin.value: "rag_service_user_admin",
    UserRole.employee.value: "rag_service_user_employee",
    "external": "rag_service_user_external",
}

VALID_SCOPES = list(SCOPES_FOR_ROLE[UserRole.admin.value])


def scopes_for_role(role: str) -> list[str]:
    """返回角色允许的知识范围列表（顺序即 FAQ 匹配优先级）。未知角色返回空列表。"""
    return list(SCOPES_FOR_ROLE.get(role, []))


def dataset_id_for_scope(scope: str) -> str:
    """返回范围对应的 Dataset ID（来自 env 配置）。"""
    settings = get_settings()
    key = _DATASET_CONFIG_KEYS.get(scope)
    if key is None:
        raise ValueError(f"未知知识范围: {scope}")
    return str(getattr(settings, key) or "")


def dataset_ids_for_role(role: str) -> list[str]:
    """返回角色可访问的 Dataset ID 列表（保持范围顺序）。"""
    return [dataset_id_for_scope(scope) for scope in scopes_for_role(role)]


def document_visibility_for_scope(scope: str) -> str:
    """返回范围对应的原 RAG Document visibility（public/shared/private）。"""
    visibility = _VISIBILITY_FOR_SCOPE.get(scope)
    if visibility is None:
        raise ValueError(f"未知知识范围: {scope}")
    return visibility


def service_user_for_role(role: str) -> str:
    """返回角色对应的上游固定服务身份（X-User-Id）。"""
    settings = get_settings()
    key = _SERVICE_USER_CONFIG_KEYS.get(role)
    if key is None:
        raise ValueError(f"未知角色: {role}")
    return str(getattr(settings, key) or "")


def member_service_users_for_scope(scope: str, *, owner_user_id: str) -> list[tuple[str, str]]:
    """返回该 Dataset 需要 ensure 的固定服务身份成员 (user_id, role)。

    由范围矩阵反推：所有能访问该 scope 的角色，其服务身份作为 viewer 显式成员；
    owner（管理员服务身份）是创建时的隐式 admin，不入 members 表
    （原 RAG 禁止通过 members API 修改 owner 的 admin 权限）。
    """
    members: list[tuple[str, str]] = []
    for role in SCOPES_FOR_ROLE:
        if scope in SCOPES_FOR_ROLE[role]:
            service_user = service_user_for_role(role)
            if service_user and service_user != owner_user_id:
                members.append((service_user, "viewer"))
    return members
