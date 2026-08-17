"""契约测试公共夹具：复用集成测试的真实 DB / HTTP 夹具。

数据库不可达时整组跳过（与集成测试一致的本地 CI 策略）。
"""

import pytest

import tests.integration.conftest as _integration  # noqa: F401  触发 APP_ENV=test + DB 探测
from app.core.config import get_settings
from tests.integration.conftest import (  # noqa: F401  re-export fixtures
    admin_user,
    client,
    db_session,
    tracked_users,
)


def _probe_db() -> bool:
    import pymysql

    s = get_settings()
    try:
        conn = pymysql.connect(
            host=s.db_host,
            port=s.db_port,
            user=s.db_user,
            password=s.db_password,
            database=s.db_name,
            connect_timeout=5,
            charset="utf8mb4",
        )
        conn.close()
        return True
    except Exception:  # noqa: BLE001
        return False


DB_UNREACHABLE = not _probe_db()

pytestmark = pytest.mark.skipif(DB_UNREACHABLE, reason="数据库不可达，跳过外部契约测试")
