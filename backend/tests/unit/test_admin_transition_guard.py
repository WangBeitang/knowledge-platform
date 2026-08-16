"""最后 active admin 保护的确定性测试（§七）。

- `validate_admin_transition` 纯函数：覆盖全部规则分支，不依赖共享库状态；
- `UserService._active_admin_count_under_lock` 接线：移出 active admin 时必须走
  `lock_active_admins()`（FOR UPDATE），其余场景走 count。
"""

import pytest

from app.core.errors import AppError
from app.core.time import utc_now_naive
from app.models.user import User
from app.services.user_service import UserService, validate_admin_transition


def _user(user_id: str, *, role: str, status: str) -> User:
    return User(
        id=user_id,
        username=f"u_{user_id}",
        display_name="测试",
        password_hash="x",
        role=role,
        status=status,
        password_changed_at=utc_now_naive(),
        created_at=utc_now_naive(),
        updated_at=utc_now_naive(),
    )


def _expect_conflict(func):
    with pytest.raises(AppError) as exc_info:
        func()
    assert exc_info.value.code == "RESOURCE_CONFLICT"
    assert exc_info.value.status_code == 409


class TestValidateAdminTransition:
    def test_admin_cannot_disable_self_even_if_others_exist(self):
        target = _user("a", role="admin", status="active")
        _expect_conflict(
            lambda: validate_admin_transition(
                operator_id="a",
                target=target,
                next_role="admin",
                next_status="disabled",
                active_admin_count=5,
            )
        )

    def test_last_active_admin_cannot_demote_self(self):
        target = _user("a", role="admin", status="active")
        _expect_conflict(
            lambda: validate_admin_transition(
                operator_id="a",
                target=target,
                next_role="employee",
                next_status="active",
                active_admin_count=1,
            )
        )

    def test_last_active_admin_cannot_be_disabled_by_other_admin(self):
        target = _user("a", role="admin", status="active")
        _expect_conflict(
            lambda: validate_admin_transition(
                operator_id="b",
                target=target,
                next_role="admin",
                next_status="disabled",
                active_admin_count=1,
            )
        )

    def test_self_demote_allowed_when_another_active_admin_exists(self):
        target = _user("a", role="admin", status="active")
        # 不抛异常即通过
        validate_admin_transition(
            operator_id="a",
            target=target,
            next_role="employee",
            next_status="active",
            active_admin_count=2,
        )

    def test_employee_target_unconstrained(self):
        target = _user("e", role="employee", status="active")
        validate_admin_transition(
            operator_id="a",
            target=target,
            next_role="employee",
            next_status="disabled",
            active_admin_count=1,  # 不影响 active admin 数量
        )

    def test_active_admin_stays_active_admin_unconstrained(self):
        target = _user("a", role="admin", status="active")
        validate_admin_transition(
            operator_id="a",
            target=target,
            next_role="admin",
            next_status="active",
            active_admin_count=1,
        )

    def test_disabled_admin_reenable_unconstrained(self):
        target = _user("a", role="admin", status="disabled")
        validate_admin_transition(
            operator_id="b",
            target=target,
            next_role="admin",
            next_status="active",
            active_admin_count=0,  # 增加而非减少
        )


class FakeUserRepository:
    """最小假件：记录是否走了 lock_active_admins，可控制返回的 active admin 列表。"""

    def __init__(self, admins: list[User]) -> None:
        self._admins = admins
        self.lock_called = False
        self.count_calls = 0

    async def lock_active_admins(self) -> list[User]:
        self.lock_called = True
        return list(self._admins)

    async def count_active_admins(self) -> int:
        self.count_calls += 1
        return len(self._admins)


class FakeSessionRepository:
    async def revoke_all_for_user(self, user_id: str, revoked_at) -> int:
        return 0


class FakeAuditService:
    async def record(self, **kwargs) -> None:
        return None


def _service(admins: list[User]) -> UserService:
    return UserService(
        user_repository=FakeUserRepository(admins),
        session_repository=FakeSessionRepository(),
        audit_service=FakeAuditService(),
    )


class TestServiceLockWiring:
    async def test_demoting_active_admin_uses_for_update_lock(self):
        target = _user("a", role="admin", status="active")
        repo = FakeUserRepository(admins=[target, _user("b", role="admin", status="active")])
        service = UserService(repo, FakeSessionRepository(), FakeAuditService())
        count = await service._active_admin_count_under_lock(
            target, next_role="employee", next_status="active", operator=target
        )
        assert repo.lock_called is True
        assert count == 2

    async def test_last_active_admin_demotion_blocked_under_lock(self):
        target = _user("a", role="admin", status="active")
        repo = FakeUserRepository(admins=[target])
        service = UserService(repo, FakeSessionRepository(), FakeAuditService())
        with pytest.raises(AppError) as exc_info:
            await service._active_admin_count_under_lock(
                target, next_role="employee", next_status="active", operator=target
            )
        assert exc_info.value.code == "RESOURCE_CONFLICT"
        assert repo.lock_called is True

    async def test_non_admin_update_uses_count_not_lock(self):
        target = _user("e", role="employee", status="active")
        repo = FakeUserRepository(admins=[_user("a", role="admin", status="active")])
        service = UserService(repo, FakeSessionRepository(), FakeAuditService())
        count = await service._active_admin_count_under_lock(
            target, next_role="employee", next_status="active", operator=target
        )
        assert repo.lock_called is False
        assert repo.count_calls == 1
        assert count == 1

    async def test_active_admin_stays_admin_uses_count_not_lock(self):
        target = _user("a", role="admin", status="active")
        repo = FakeUserRepository(admins=[target])
        service = UserService(repo, FakeSessionRepository(), FakeAuditService())
        count = await service._active_admin_count_under_lock(
            target, next_role="admin", next_status="active", operator=target
        )
        assert repo.lock_called is False
        assert count == 1
