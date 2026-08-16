"""用户管理服务（仅管理员）：创建/修改/重置密码，含最后 active admin 保护。

规则（冻结）：
- username 全局唯一、统一转小写；role 仅 admin|employee；status 初始 active；
- 系统始终至少存在一个 active admin；管理员不能停用自己；
- 任何角色/状态修改不得把 active admin 数量降为 0（自己降权允许，只要仍有其他 active admin）；
- 状态转换语义：active→disabled 写 disabled_at 并撤销会话；disabled→active 清 disabled_at；
  disabled→disabled（如只改 display_name）不重写 disabled_at、不重复撤销会话；
- 重置密码必须更新 password_changed_at 并撤销该用户全部会话（旧 JWT 立即失效）。

最后 active admin 并发保护：当修改会把某 active admin 移出 active admin 集合时，
在同一事务内 `SELECT ... FOR UPDATE` 锁定当前 active admin 行，锁定后重算数量，
只有数量 > 1 才允许降权/停用。
"""

from app.core.enums import AuditAction, UserRole, UserStatus
from app.core.errors import bad_request, conflict, not_found
from app.core.security import hash_password
from app.core.time import utc_now_naive
from app.models.user import User
from app.repositories.auth_session_repository import AuthSessionRepository
from app.repositories.user_repository import UserRepository
from app.schemas.user import UserView
from app.services.audit_service import AuditService

USER_RESOURCE_TYPE = "user"


def user_view(user: User) -> UserView:
    from app.core.time import iso8601

    return UserView(
        id=user.id,
        username=user.username,
        display_name=user.display_name,
        role=user.role,
        status=user.status,
        last_login_at=iso8601(user.last_login_at),
        created_at=iso8601(user.created_at),
        updated_at=iso8601(user.updated_at),
    )


def validate_admin_transition(
    *,
    operator_id: str,
    target: User,
    next_role: str,
    next_status: str,
    active_admin_count: int,
) -> None:
    """最后 active admin 保护的确定性判定（纯函数，可单测）。

    - 管理员不能停用自己（无论是否最后一个）；
    - 若修改会把当前 active admin 移出 active admin 集合，且移除后数量为 0 → 409；
    - 自己降权：有其他 active admin（count > 1）时允许。
    """
    if next_status == UserStatus.disabled.value and target.id == operator_id:
        raise conflict("管理员不能停用自己")

    is_active_admin_now = (
        target.role == UserRole.admin.value and target.status == UserStatus.active.value
    )
    stays_active_admin = (
        next_role == UserRole.admin.value and next_status == UserStatus.active.value
    )
    if is_active_admin_now and not stays_active_admin and active_admin_count <= 1:
        raise conflict("系统至少需要保留一个有效管理员，无法完成该修改")


class UserService:
    def __init__(
        self,
        user_repository: UserRepository,
        session_repository: AuthSessionRepository,
        audit_service: AuditService,
    ) -> None:
        self.users = user_repository
        self.sessions = session_repository
        self.audit = audit_service

    async def list_users(
        self,
        *,
        page: int,
        page_size: int,
        sort_by: str,
        sort_order: str,
    ) -> tuple[list[UserView], int]:
        rows, total = await self.users.list_page(
            page=page,
            page_size=page_size,
            sort_by=sort_by,
            sort_order=sort_order,
        )
        return [user_view(row) for row in rows], total

    async def create_user(
        self,
        *,
        operator: User,
        username: str,
        display_name: str,
        role: str,
        initial_password: str,
        client_ip: str | None,
    ) -> UserView:
        normalized = username.strip().lower()
        if not normalized:
            raise bad_request("用户名不能为空")
        if await self.users.get_by_username(normalized) is not None:
            raise conflict("用户名已存在")
        password_hash = hash_password(initial_password)  # 空/超长在此明确拒绝
        now = utc_now_naive()
        user = User(
            username=normalized,
            display_name=display_name.strip(),
            password_hash=password_hash,
            role=role,
            status=UserStatus.active.value,
            password_changed_at=now,
            created_by_user_id=operator.id,
            created_at=now,
            updated_at=now,
        )
        created = await self.users.add(user)
        after = user_view(created).model_dump()
        # 审计安全快照：不包含密码（模型本身无明文/哈希外泄；显式剔除密码相关键）
        safe_after = {k: v for k, v in after.items() if "password" not in k.lower()}
        await self.audit.record(
            operator_user_id=operator.id,
            action=AuditAction.user_created.value,
            resource_type=USER_RESOURCE_TYPE,
            resource_id=created.id,
            result="succeeded",
            after=safe_after,
            client_ip=client_ip,
        )
        return user_view(created)

    async def update_user(
        self,
        *,
        operator: User,
        user_id: str,
        display_name: str | None,
        role: str | None,
        status: str | None,
        client_ip: str | None,
    ) -> UserView:
        target = await self.users.get_by_id(user_id)
        if target is None:
            raise not_found("用户不存在")
        if display_name is None and role is None and status is None:
            raise bad_request("至少需要提供一个要更新的字段")

        before = user_view(target).model_dump()
        next_role = role or target.role
        next_status = status or target.status

        # 最后 active admin 保护：同一事务内锁定 active admin 行后重算数量
        await self._active_admin_count_under_lock(
            target, next_role=next_role, next_status=next_status, operator=operator
        )

        if display_name is not None:
            target.display_name = display_name.strip()
        target.role = next_role
        old_status = target.status
        target.status = next_status
        # 状态转换语义：只有 status 实际变化时才维护 disabled_at / 撤销会话
        if next_status != old_status:
            if next_status == UserStatus.disabled.value:
                target.disabled_at = utc_now_naive()
                # 停用即撤销该用户全部会话：旧 JWT 下一次请求立即失效
                await self.sessions.revoke_all_for_user(target.id, utc_now_naive())
            elif old_status == UserStatus.disabled.value:
                # disabled → active：清除停用时间
                target.disabled_at = None
        target.updated_at = utc_now_naive()
        await self.users.session.flush()

        after = user_view(target).model_dump()
        safe_before = {k: v for k, v in before.items() if "password" not in k.lower()}
        safe_after = {k: v for k, v in after.items() if "password" not in k.lower()}
        await self.audit.record(
            operator_user_id=operator.id,
            action=AuditAction.user_updated.value,
            resource_type=USER_RESOURCE_TYPE,
            resource_id=target.id,
            result="succeeded",
            before=safe_before,
            after=safe_after,
            client_ip=client_ip,
        )
        return user_view(target)

    async def _active_admin_count_under_lock(
        self,
        target: User,
        *,
        next_role: str,
        next_status: str,
        operator: User,
    ) -> int:
        """计算 active admin 数量；若本次修改会把 target 移出集合，则先加行锁再重算。

        - “管理员不能停用自己”不依赖数量，先做确定性校验（纯函数内）；
        - 需要数量判定时，`SELECT ... FOR UPDATE` 锁定当前 active admin 行，
          与后续 UPDATE 处于同一事务，杜绝 count→update 并发窗口。
        """
        is_active_admin_now = (
            target.role == UserRole.admin.value and target.status == UserStatus.active.value
        )
        stays_active_admin = (
            next_role == UserRole.admin.value and next_status == UserStatus.active.value
        )
        if is_active_admin_now and not stays_active_admin:
            admins = await self.users.lock_active_admins()
            count = len(admins)
        else:
            count = await self.users.count_active_admins()
        validate_admin_transition(
            operator_id=operator.id,
            target=target,
            next_role=next_role,
            next_status=next_status,
            active_admin_count=count,
        )
        return count

    async def reset_password(
        self,
        *,
        operator: User,
        user_id: str,
        new_password: str,
        client_ip: str | None,
    ) -> UserView:
        target = await self.users.get_by_id(user_id)
        if target is None:
            raise not_found("用户不存在")
        target.password_hash = hash_password(new_password)  # 空/超长在此明确拒绝
        target.password_changed_at = utc_now_naive()
        target.updated_at = utc_now_naive()
        await self.users.session.flush()
        # 撤销该用户全部会话：旧 JWT 下一次请求立即失效
        await self.sessions.revoke_all_for_user(target.id, utc_now_naive())

        after = user_view(target).model_dump()
        safe_after = {k: v for k, v in after.items() if "password" not in k.lower()}
        await self.audit.record(
            operator_user_id=operator.id,
            action=AuditAction.user_password_reset.value,
            resource_type=USER_RESOURCE_TYPE,
            resource_id=target.id,
            result="succeeded",
            after=safe_after,
            client_ip=client_ip,
        )
        return user_view(target)
