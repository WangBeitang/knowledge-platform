"""JWT iat 与 password_changed_at 精度边界回归测试（DATETIME(6) vs 秒级 iat）。

规则（复核修复后）：
- JWT `iat` 为真实签发秒（不制造未来时间），PyJWT 标准 `verify_iat` 保持开启；
- 同秒边界用 `auth_sessions.issued_at`（DATETIME(6) 微秒）判定：
  1. iat 明显早于 password_changed_at 所在秒 → 旧 Token；
  2. 明显晚于 → 新 Token；
  3. 同秒 → session.issued_at < password_changed_at → 旧 Token。
"""

from datetime import UTC, datetime

import jwt as pyjwt
import pytest

from app.core.security import (
    create_access_token,
    decode_access_token,
    is_iat_before_password_change,
)
from app.core.time import utc_now_naive


def _dt(day_second: float, *, naive: bool = True) -> datetime:
    """构造 2026-08-16 当天的第 day_second 秒（可带小数微秒）。"""
    seconds_total = int(day_second)
    micro = int(round((day_second - seconds_total) * 1_000_000))
    hour = seconds_total // 3600
    minute = (seconds_total % 3600) // 60
    sec = seconds_total % 60
    return datetime(
        2026,
        8,
        16,
        hour,
        minute,
        sec,
        micro,
        tzinfo=None if naive else UTC,
    )


def _epoch_of(dt: datetime) -> float:
    """按“无时区时间视为 UTC”的约定计算 epoch（与判定函数语义一致）。"""
    return dt.replace(tzinfo=UTC).timestamp()


class TestIssuedBeforePasswordChange:
    def test_old_token_same_second_is_rejected(self):
        # 签发 12:00:00.200，重置 12:00:00.500（同秒）：会话签发早于变更 → 旧
        pca = _dt(12 * 3600 + 0.5)
        issued_at = _dt(12 * 3600 + 0.2)
        iat = int(_epoch_of(_dt(12 * 3600 + 0.2)))
        assert is_iat_before_password_change(iat, issued_at, pca) is True

    def test_old_token_cross_second_is_rejected(self):
        pca = _dt(12 * 3600 + 0.5)
        issued_at = _dt(12 * 3600 - 1 + 0.3)  # 上一秒签发
        iat = int(_epoch_of(issued_at))
        assert is_iat_before_password_change(iat, issued_at, pca) is True

    def test_new_token_same_second_after_change_is_valid(self):
        # 重置 12:00:00.500，同秒（12:00:00.700）重新登录：会话签发晚于变更 → 新
        pca = _dt(12 * 3600 + 0.5)
        issued_at = _dt(12 * 3600 + 0.7)
        iat = int(_epoch_of(issued_at))
        assert is_iat_before_password_change(iat, issued_at, pca) is False

    def test_new_token_cross_second_is_valid(self):
        pca = _dt(12 * 3600 + 0.5)
        issued_at = _dt(12 * 3600 + 2.3)
        iat = int(_epoch_of(issued_at))
        assert is_iat_before_password_change(iat, issued_at, pca) is False

    def test_whole_second_boundary(self):
        # pca 恰为整秒 12:00:00.000000；上一秒签发的 token 必为旧
        pca = _dt(12 * 3600, naive=True)
        issued_old = _dt(12 * 3600 - 1 + 0.5)
        assert is_iat_before_password_change(int(_epoch_of(issued_old)), issued_old, pca) is True
        # 整秒后（12:00:01）签发 → 新
        issued_new = _dt(12 * 3600 + 1 + 0.2)
        assert is_iat_before_password_change(int(_epoch_of(issued_new)), issued_new, pca) is False


class TestIatIsRealAndStandardVerify:
    def test_iat_is_real_second_not_future(self):
        """iat 必须是真实签发秒，不允许人为 +1 制造未来时间。"""
        import time as _time

        token, _exp = create_access_token(
            user_id="u1",
            session_id="s1",
            jti="j1",
            role="admin",
        )
        payload = decode_access_token(token)
        assert payload["iat"] == int(_time.time())
        assert payload["iat"] <= int(_time.time())  # 不超前

    def test_standard_iat_verification_enabled(self):
        """decode 恢复 PyJWT 标准 iat 校验：伪造未来 iat 的 token 必须被拒。"""
        from app.core.config import get_settings

        future_iat = int(__import__("time").time()) + 60
        forged = pyjwt.encode(
            {
                "sub": "u1",
                "sid": "s1",
                "jti": "j1",
                "role": "admin",
                "iat": future_iat,
                "exp": future_iat + 3600,
            },
            get_settings().secret_key,
            algorithm="HS256",
        )
        with pytest.raises(pyjwt.ImmatureSignatureError):
            decode_access_token(forged)


class TestNaiveUtcHandling:
    def test_naive_db_time_treated_as_utc(self):
        # 数据库读出的无时区 DATETIME(6) 应被当作 UTC 处理
        pca = utc_now_naive()
        import time as _time

        issued_at = utc_now_naive()
        past_iat = int(_time.time()) - 2
        assert is_iat_before_password_change(past_iat, issued_at, pca) is True

        future_iat = int(_time.time()) + 2
        assert is_iat_before_password_change(future_iat, issued_at, pca) is False
