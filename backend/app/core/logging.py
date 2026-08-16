"""结构化日志：注入 request_id，密钥/敏感字段打码。"""

import logging
import sys

# 禁止出现在日志中的敏感键（值一律打码）
SENSITIVE_KEYS = {"password", "access_token", "jwt", "service_api_key", "secret_key"}


class RedactingFormatter(logging.Formatter):
    """对敏感键值打码，避免密码/令牌进入日志。"""

    def _redact(self, record: logging.LogRecord) -> logging.LogRecord:
        args = record.args
        if isinstance(args, dict):
            safe = {
                k: ("***" if k.lower() in SENSITIVE_KEYS or "password" in k.lower() else v)
                for k, v in args.items()
            }
            record.args = safe  # type: ignore[assignment]
        return record

    def format(self, record: logging.LogRecord) -> str:
        record = self._redact(record)
        return super().format(record)


def setup_logging() -> None:
    root = logging.getLogger()
    if root.handlers:
        return
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(RedactingFormatter("%(asctime)s %(levelname)s [%(name)s] %(message)s"))
    root.addHandler(handler)
    root.setLevel(logging.INFO)
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
    logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
