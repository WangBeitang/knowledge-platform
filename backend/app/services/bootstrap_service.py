"""Dataset 初始化服务：三档 Dataset 幂等 bootstrap 与状态查询（冻结 §8 / API §6）。

幂等语义：
- 先查后建（先 GET /datasets/{id}，不存在才 POST /datasets）；
- 成员 ensure 使用上游 upsert（幂等），并核对 `user_id + role`（role 不符视为未就绪）；
- 重复执行返回真实状态（existed/created/verified），不伪造成功；
- `verify_only=true` 只查询校验，禁止任何写副作用（含审计），也禁止修正 role。
"""

from app.core.config import get_settings
from app.core.enums import AuditAction
from app.core.errors import AppError
from app.rag.rag_errors import RagError
from app.rag.rag_import_client import RagImportClient, get_rag_import_client
from app.rag.scope_policy import (
    VALID_SCOPES,
    dataset_id_for_scope,
    member_service_users_for_scope,
    service_user_for_role,
)
from app.schemas.integration import RagDatasetStatusItem
from app.services.audit_service import AuditService

_DATASET_DISPLAY_NAMES = {
    "external_public": "证券外部公开知识库",
    "internal_shared": "证券内部共享知识库",
    "admin_private": "证券管理员专属知识库",
}


def _admin_service_user() -> str:
    return service_user_for_role("admin")


def is_import_base_url_configured() -> bool:
    """RAG 导入服务 Base URL 是否真实配置（不泄露 URL 本身）。"""
    return bool(get_settings().rag_import_base_url.strip())


class BootstrapService:
    def __init__(
        self,
        *,
        client: RagImportClient | None = None,
        audit_service: AuditService | None = None,
    ) -> None:
        # 正常应用路径复用共享 client（连接池生命周期由 lifespan 统一管理）；
        # 测试可注入独立 client（MockTransport），由测试自行关闭。
        self.client = client or get_rag_import_client()
        self.audit = audit_service

    # ---------- 状态查询 ----------

    async def get_rag_status(self) -> tuple[list[RagDatasetStatusItem], str]:
        """查询三档 Dataset 状态（只读，不产生任何写副作用）。

        overall 语义：
        - ok：三档均 Dataset exists 且固定服务身份成员完整且角色正确；
        - partial：存在 Dataset 但 member 缺失或角色不符；
        - failed：三档 RAG 调用全部失败。
        """
        service_user = _admin_service_user()
        items: list[RagDatasetStatusItem] = []
        for scope in VALID_SCOPES:
            dataset_id = dataset_id_for_scope(scope)
            if not dataset_id:
                items.append(
                    self._status_item(scope, dataset_id, "failed", "skipped", "Dataset ID 未配置")
                )
                continue
            try:
                dataset = await self.client.get_dataset(dataset_id, service_user=service_user)
                if dataset is None:
                    items.append(
                        self._status_item(scope, dataset_id, "missing", "missing", "Dataset 不存在")
                    )
                    continue
                member_ok, missing = await self._members_verified(scope, dataset_id)
                member_status = "verified" if member_ok else "missing"
                doc_count = dataset.get("document_count")
                items.append(
                    self._status_item(
                        scope,
                        dataset_id,
                        "exists",
                        member_status,
                        f"成员{'完整' if member_ok else '缺失/角色不符'}: {missing}",
                        document_count=doc_count if isinstance(doc_count, int) else None,
                    )
                )
            except RagError as exc:
                items.append(self._status_item(scope, dataset_id, "failed", "skipped", exc.message))
            except AppError as exc:
                items.append(self._status_item(scope, dataset_id, "failed", "skipped", exc.message))
        overall = self._status_overall(items)
        return items, overall

    @staticmethod
    def _status_overall(items: list[RagDatasetStatusItem]) -> str:
        """状态查询的 overall：ok 需要三档 exists + member verified。"""
        if all(i.status == "failed" for i in items):
            return "failed"
        if all(i.status == "exists" and i.member_status == "verified" for i in items):
            return "ok"
        return "partial"

    # ---------- bootstrap ----------

    async def bootstrap_datasets(
        self, *, verify_only: bool, operator_user_id: str | None = None
    ) -> tuple[list[RagDatasetStatusItem], str, str]:
        """执行/校验三档 Dataset bootstrap。返回 (items, overall, result)。

        verify_only=False 时写一条 audit（成功或失败）；verify_only=True 零写副作用。
        operator_user_id 为真实平台操作人（audit_logs.operator_user_id 外键指向 users.id）。
        """
        service_user = _admin_service_user()
        items: list[RagDatasetStatusItem] = []
        for scope in VALID_SCOPES:
            dataset_id = dataset_id_for_scope(scope)
            if not dataset_id:
                items.append(
                    self._status_item(scope, dataset_id, "failed", "skipped", "Dataset ID 未配置")
                )
                continue
            item = await self._bootstrap_one(
                scope, dataset_id, service_user, verify_only=verify_only
            )
            items.append(item)

        overall = "succeeded"
        for item in items:
            if item.status == "failed":
                overall = "failed"
                break
            # 任一档 Dataset 缺失/未创建，或成员缺失/角色不符 → 不算完全成功
            if item.status not in ("created", "existed", "verified"):
                overall = "partial"
                break
            if item.member_status in ("missing", "failed", "skipped"):
                overall = "partial"
                break
        if overall != "succeeded" and not any(i.status == "failed" for i in items):
            overall = "partial"

        result = "succeeded" if overall == "succeeded" else "failed"
        if not verify_only and self.audit is not None and operator_user_id:
            await self.audit.record(
                operator_user_id=operator_user_id,
                action=AuditAction.dataset_bootstrap.value,
                resource_type="dataset",
                resource_id=",".join(dataset_id_for_scope(s) or s for s in VALID_SCOPES),
                result=result,
                after={
                    "verify_only": verify_only,
                    "overall": overall,
                    "items": [i.model_dump() for i in items],
                },
            )
        return items, overall, result

    async def _bootstrap_one(
        self,
        scope: str,
        dataset_id: str,
        service_user: str,
        *,
        verify_only: bool,
    ) -> RagDatasetStatusItem:
        try:
            dataset = await self.client.get_dataset(dataset_id, service_user=service_user)
            if dataset is None:
                if verify_only:
                    return self._status_item(
                        scope,
                        dataset_id,
                        "missing",
                        "missing",
                        "Dataset 不存在（verify-only 不创建）",
                    )
                await self.client.create_dataset(
                    dataset_id=dataset_id,
                    name=_DATASET_DISPLAY_NAMES.get(scope, dataset_id),
                    description=f"券商财富业务知识管理平台 {scope} 档知识库",
                    service_user=service_user,
                )
                created = True
            else:
                created = False

            member_ok, _missing = await self._members_verified(scope, dataset_id)
            if member_ok:
                member_status = "verified"
            elif verify_only:
                # verify-only：只报告不一致，不允许修正 role/缺失
                member_status = "missing"
            else:
                # 真实 bootstrap：幂等 upsert 补齐/修正为冻结要求的 user_id + role
                for member_user_id, role in member_service_users_for_scope(
                    scope, owner_user_id=service_user
                ):
                    await self.client.upsert_member(
                        dataset_id=dataset_id,
                        member_user_id=member_user_id,
                        role=role,
                        operator_service_user=service_user,
                    )
                member_status = "ensured"

            if created:
                status = "created"
                message = "Dataset 已创建"
            else:
                status = "existed"
                message = "Dataset 已存在"
            if member_status == "verified":
                message += "，成员已就绪"
            elif member_status == "missing":
                message += "，成员缺失或角色不符"
            elif member_status == "ensured":
                message += "，成员已补齐"
            return self._status_item(scope, dataset_id, status, member_status, message)
        except RagError as exc:
            return self._status_item(scope, dataset_id, "failed", "skipped", exc.message)
        except AppError as exc:
            return self._status_item(scope, dataset_id, "failed", "skipped", exc.message)

    async def _members_verified(self, scope: str, dataset_id: str) -> tuple[bool, str]:
        """校验该 Dataset 的固定服务身份成员是否就绪（user_id + role 都必须匹配）。

        返回 (verified, missing_desc)：missing_desc 描述缺失或角色不符的成员，便于前端展示。
        """
        service_user = _admin_service_user()
        expected = member_service_users_for_scope(scope, owner_user_id=service_user)
        if not expected:
            return True, ""
        members = await self.client.list_dataset_members(dataset_id, service_user=service_user)
        present = {
            (str(m.get("user_id")), str(m.get("role"))) for m in members if not m.get("removed_at")
        }
        missing = [
            f"{user_id}={role}" for user_id, role in expected if (user_id, role) not in present
        ]
        return (not missing), ",".join(missing)

    @staticmethod
    def _status_item(
        scope: str,
        dataset_id: str,
        status: str,
        member_status: str,
        message: str,
        *,
        document_count: int | None = None,
    ) -> RagDatasetStatusItem:
        return RagDatasetStatusItem(
            scope=scope,
            dataset_id=dataset_id,
            status=status,
            member_status=member_status,
            document_count=document_count,
            message=message,
        )
