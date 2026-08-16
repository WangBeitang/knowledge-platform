"""安全工具：密码哈希、JWT 签发/校验、jti 计算。

规则（冻结）：
- 密码使用 bcrypt 安全哈希，禁止明文；
- JWT 至少包含 sub/sid/jti/role/iat/exp；
- 会话校验（撤销、用户状态、密码变更时间）由 auth 层依赖完成，本模块只做编解码。
"""

import hashlib
import time
import uuid
from typing import Any

import jwt
from passlib.context import CryptContext

from app.core.config import get_settings

_pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

ALGORITHM = "HS256"


def hash_password(plain: str) -> str:
    return _pwd_context.hash(plain)


def verify_password(plain: str, hashed: str) -> bool:
    try:
        return _pwd_context.verify(plain, hashed)
    except Exception:
        return False


def new_jti() -> str:
    return uuid.uuid4().hex


def jti_hash(jti: str) -> str:
    """JWT 唯一标识的 SHA-256，只存哈希不存原令牌。"""
    return hashlib.sha256(jti.encode("utf-8")).hexdigest()


def create_access_token(
    *,
    user_id: str,
    session_id: str,
    jti: str,
    role: str,
    expires_seconds: int | None = None,
) -> tuple[str, int]:
    """签发 JWT，返回 (token, expires_in)。"""
    settings = get_settings()
    now = int(time.time())
    expires_in = expires_seconds or settings.jwt_expires_seconds
    payload: dict[str, Any] = {
        "sub": user_id,
        "sid": session_id,
        "jti": jti,
        "role": role,
        "iat": now,
        "exp": now + expires_in,
    }
    token = jwt.encode(payload, settings.secret_key, algorithm=ALGORITHM)
    return token, expires_in


def decode_access_token(token: str) -> dict[str, Any]:
    """解码并校验签名与过期；失败抛 jwt 异常由调用方映射为 AUTH_REQUIRED。"""
    settings = get_settings()
    return jwt.decode(token, settings.secret_key, algorithms=[ALGORITHM])
