"""UTC 时间工具：存储与输出约定统一。

数据库列（DATETIME(6)）存无时区 UTC；内存比较使用带时区 aware datetime；
API 输出使用带时区 ISO 8601。
"""

from datetime import UTC, datetime


def utc_now() -> datetime:
    """内存/比较用：带时区的当前 UTC 时间。"""
    return datetime.now(UTC)


def utc_now_naive() -> datetime:
    """数据库存储用：无时区 UTC（MySQL DATETIME 无时区语义）。"""
    return datetime.now(UTC).replace(tzinfo=None)


def to_utc_aware(value: datetime) -> datetime:
    """把数据库读出的无时区时间当作 UTC 处理，统一为 aware datetime。"""
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def iso8601(dt: datetime | None) -> str | None:
    """API 输出带时区 ISO 8601。"""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.isoformat()


def parse_utc_bound(value: str, *, end_of_day: bool) -> datetime:
    """把查询参数解析为 UTC naive 边界（看板/审计通用过滤，冻结口径）。

    支持：
    - `YYYY-MM-DD`：date_from 取当日 00:00:00；date_to 取当日 23:59:59.999999；
    - ISO datetime（如 `2026-08-17T10:30:00` 或带时区）：按绝对值处理。
    非法输入抛 ValueError，由路由层 422 捕获。
    """
    from datetime import date
    from datetime import datetime as _datetime

    text = value.strip()
    if "T" in text:
        dt = _datetime.fromisoformat(text.replace("Z", "+00:00"))
        if dt.tzinfo is not None:
            dt = dt.astimezone(UTC).replace(tzinfo=None)
        return dt
    d = date.fromisoformat(text)
    if end_of_day:
        return _datetime.combine(d, _datetime.max.time())
    return _datetime.combine(d, _datetime.min.time())
