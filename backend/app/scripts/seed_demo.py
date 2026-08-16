"""可选演示数据（CLI）：创建演示 employee 账号（幂等）。

用法：`python -m app.scripts.seed_demo`

职责（冻结 §四）：只负责可选演示数据，不是生产初始化的必要步骤。
凭据从环境变量读取（DEMO_EMPLOYEE_USERNAME / DEMO_EMPLOYEE_PASSWORD），
未配置时跳过；密码不硬编码在仓库。
"""

import asyncio
import os

from sqlalchemy import select

from app.core.database import get_session_factory, init_engine
from app.core.enums import UserRole, UserStatus
from app.core.security import hash_password
from app.core.time import utc_now_naive
from app.models.user import User


async def _run() -> None:
    username = os.getenv("DEMO_EMPLOYEE_USERNAME", "").strip().lower()
    password = os.getenv("DEMO_EMPLOYEE_PASSWORD", "")
    if not username or not password:
        print("[seed_demo] 未配置 DEMO_EMPLOYEE_USERNAME / DEMO_EMPLOYEE_PASSWORD，跳过演示数据")
        return

    init_engine()
    factory = get_session_factory()
    async with factory() as session:
        existing = await session.scalar(select(User).where(User.username == username))
        if existing is not None:
            print(f"[seed_demo] 演示账号已存在（username={username}），跳过")
            await session.commit()
            return
        now = utc_now_naive()
        demo = User(
            username=username,
            display_name="演示员工",
            password_hash=hash_password(password),
            role=UserRole.employee.value,
            status=UserStatus.active.value,
            password_changed_at=now,
            created_by_user_id=None,
            created_at=now,
            updated_at=now,
        )
        session.add(demo)
        await session.commit()
        print(f"[seed_demo] 已创建演示 employee（username={username}）")


def main() -> None:
    asyncio.run(_run())


if __name__ == "__main__":
    main()
