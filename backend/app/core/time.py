"""UTC 时间工具：存储与输出约定统一。"""

from datetime import UTC, datetime


def utc_now() -> datetime:
    """数据库存 UTC 的 DATETIME(6)。"""
    return datetime.now(UTC)


def iso8601(dt: datetime | None) -> str | None:
    """API 输出带时区 ISO 8601。"""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.isoformat()
