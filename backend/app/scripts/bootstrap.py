"""生产/真实环境可重复执行的幂等初始化（CLI）。

用法：`python -m app.scripts.bootstrap`

职责（冻结 §四）：
1. 检查初始管理员（INIT_ADMIN_USERNAME / INIT_ADMIN_PASSWORD）；
   不存在则创建 active admin，已存在则不重复创建、不覆盖密码；
2. 执行三档 Dataset bootstrap（先查后建，幂等）；
3. 多次执行不得创建重复管理员、重复 Dataset。

退出码：
- Dataset bootstrap 全部 succeeded → 0（成功）；
- 任一档 failed/缺失 → 非 0（部署脚本据此判定初始化失败）；
- 无论成败，finally 统一关闭 DB engine 与共享 RAG client。
"""

import asyncio
import sys

from sqlalchemy import select

from app.core.config import get_settings
from app.core.database import close_engine, get_session_factory, init_engine
from app.core.enums import UserRole, UserStatus
from app.core.security import hash_password
from app.core.time import utc_now_naive
from app.models.user import User
from app.rag.rag_import_client import close_rag_import_client
from app.repositories.audit_log_repository import AuditLogRepository
from app.services.audit_service import AuditService
from app.services.bootstrap_service import BootstrapService


async def _ensure_initial_admin(session) -> User:
    """幂等创建初始管理员。已存在则不覆盖密码。"""
    settings = get_settings()
    username = settings.init_admin_username.strip().lower()
    password = settings.init_admin_password
    if not username or not password:
        raise SystemExit("缺少 INIT_ADMIN_USERNAME / INIT_ADMIN_PASSWORD，无法初始化初始管理员")

    admin = await session.scalar(select(User).where(User.username == username))
    if admin is not None:
        print(f"[bootstrap] 初始管理员已存在（username={username}），跳过创建，不覆盖密码")
        return admin

    now = utc_now_naive()
    admin = User(
        username=username,
        display_name="系统管理员",
        password_hash=hash_password(password),
        role=UserRole.admin.value,
        status=UserStatus.active.value,
        password_changed_at=now,
        created_by_user_id=None,
        created_at=now,
        updated_at=now,
    )
    session.add(admin)
    await session.flush()
    print(f"[bootstrap] 已创建初始管理员（username={username}，role=admin，status=active）")
    return admin


async def _bootstrap_datasets(session, admin: User) -> tuple[str, str]:
    """三档 Dataset 幂等初始化。返回 (overall, result)。"""
    audit = AuditService(AuditLogRepository(session))
    service = BootstrapService(audit_service=audit)
    items, overall, result = await service.bootstrap_datasets(
        verify_only=False,
        operator_user_id=admin.id,
    )
    for item in items:
        print(
            f"[bootstrap] Dataset {item.scope}: status={item.status} "
            f"member={item.member_status} - {item.message}"
        )
    print(f"[bootstrap] Dataset bootstrap 完成，overall={overall}")
    return overall, result


async def _run() -> int:
    init_engine()
    factory = get_session_factory()
    try:
        async with factory() as session:
            admin = await _ensure_initial_admin(session)
            overall, _result = await _bootstrap_datasets(session, admin)
            # admin/audit 状态正常提交（失败项只影响退出码，不阻断已有记录落库）
            await session.commit()
        return 0 if overall == "succeeded" else 1
    finally:
        # 无论成败，统一关闭 DB engine 与共享 RAG client
        await close_rag_import_client()
        await close_engine()


def main() -> None:
    code = asyncio.run(_run())
    print(f"[bootstrap] 退出码={code}")
    sys.exit(code)


if __name__ == "__main__":
    main()
