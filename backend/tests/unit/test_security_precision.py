"""JWT iat 与 password_changed_at 精度边界回归测试（DATETIME(6) vs 秒级 iat）。

验证目标（冻结 §3.1）：
- 重置前的旧 Token 立即失效（含同秒场景）；
- 重置后同秒重新登录的新 Token 不被误判为旧 Token；
- 数据库读出的无时区时间按 UTC 处理，不受服务器本地时区影响。
"""

from datetime import UTC, datetime

import pytest

from app.core.security import (
    create_access_token,
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
    """按“无时区时间视为 UTC”的约定计算 epoch（与校验函数语义一致）。"""
    return dt.replace(tzinfo=UTC).timestamp()


class TestIssuedBeforePasswordChange:
    def test_old_token_same_second_is_rejected(self):
        # 签发于 12:00:00.200，重置于 12:00:00.500（同秒）
        pca = _dt(12 * 3600 + 0.5)
        old_iat = int(_epoch_of(_dt(12 * 3600 + 0.2)))
        assert is_iat_before_password_change(old_iat, pca) is True

    def test_old_token_cross_second_is_rejected(self):
        pca = _dt(12 * 3600 + 0.5)
        assert is_iat_before_password_change(int(_epoch_of(pca)) - 1, pca) is True

    def test_new_token_after_change_is_valid(self):
        pca = _dt(12 * 3600 + 0.5)
        # 重置后（12:00:02 秒）签发的新 Token 不应被拒绝
        assert is_iat_before_password_change(int(_epoch_of(pca)) + 2, pca) is False

    def test_whole_second_boundary(self):
        pca = _dt(12 * 3600, naive=True)  # 恰为整秒 12:00:00.000000
        assert is_iat_before_password_change(int(_epoch_of(pca)) - 1, pca) is True
        assert is_iat_before_password_change(int(_epoch_of(pca)), pca) is False


class TestCreateTokenSameSecond:
    def _token_iat(self, *, now_ts: float, pca: datetime) -> int:
        class FakeTime:
            @staticmethod
            def time() -> float:
                return now_ts

        monkeypatch = pytest.MonkeyPatch()
        monkeypatch.setattr("app.core.security.time.time", FakeTime.time)
        try:
            token, _exp = create_access_token(
                user_id="u1",
                session_id="s1",
                jti="j1",
                role="admin",
                password_changed_at=pca,
            )
            import jwt as _jwt

            from app.core.config import get_settings

            payload = _jwt.decode(token, get_settings().secret_key, algorithms=["HS256"])
            return int(payload["iat"])
        finally:
            monkeypatch.undo()

    def test_same_second_login_bumps_iat(self):
        # 重置于 12:00:00.500，同秒（12:00:00.700）登录：iat 提升到下一秒，避免误判
        pca = _dt(12 * 3600 + 0.5)
        now_ts = _epoch_of(_dt(12 * 3600 + 0.7))
        iat = self._token_iat(now_ts=now_ts, pca=pca)
        assert iat == int(_epoch_of(pca)) + 1
        assert is_iat_before_password_change(iat, pca) is False  # 新 Token 有效

    def test_cross_second_login_no_bump(self):
        pca = _dt(12 * 3600 + 0.5)
        now_ts = _epoch_of(_dt(12 * 3600 + 2.3))
        iat = self._token_iat(now_ts=now_ts, pca=pca)
        assert iat == int(_epoch_of(pca)) + 2
        assert is_iat_before_password_change(iat, pca) is False

    def test_no_password_change_no_bump(self):
        pca = _dt(9 * 3600)  # 很久以前
        now_ts = _epoch_of(_dt(12 * 3600 + 0.9))
        iat = self._token_iat(now_ts=now_ts, pca=pca)
        assert iat == int(_epoch_of(_dt(12 * 3600 + 0.9)))


class TestNaiveUtcHandling:
    def test_naive_db_time_treated_as_utc(self):
        # 数据库读出的无时区 DATETIME(6) 应被当作 UTC 处理
        pca = utc_now_naive()
        import time as _time

        past_iat = int(_time.time()) - 2
        assert is_iat_before_password_change(past_iat, pca) is True

        future_iat = int(_time.time()) + 2
        assert is_iat_before_password_change(future_iat, pca) is False
