"""安全工具：密码哈希、JWT 签发/校验、会话标识、密码变更时间精度处理。

规则（冻结）：
- 密码使用 bcrypt 安全哈希，禁止明文；空密码与超长密码明确拒绝（不产生 500）；
- JWT 至少包含 sub/sid/jti/role/iat/exp；会话撤销、用户状态、密码变更时间由
  auth 层依赖校验，本模块只做编解码与精度相关的判定；
- `auth_sessions` 只保存 jti 的 SHA-256，禁止保存完整 JWT。

精度约定（DATETIME(6) 与 JWT 秒级 iat）：
- 签发时 `iat = floor(now)`；若与最近 `password_changed_at` 处于同一秒，
  iat 提升到下一秒，保证“重置密码后立即重新登录”的新 Token 不会被误判为旧 Token；
- 校验时按 `iat < ceil(password_changed_at)` 判定旧 Token（详见
  `is_iat_before_password_change`）。
"""

import hashlib
import math
import time
import uuid
from datetime import datetime
from typing import Any

import jwt
from passlib.context import CryptContext

from app.core.config import get_settings
from app.core.errors import bad_request
from app.core.time import to_utc_aware

_pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

ALGORITHM = "HS256"

# passlib/bcrypt 对输入长度的限制：72 字节。超长密码显式拒绝，避免底层截断或抛错。
BCRYPT_MAX_BYTES = 72


def validate_password_policy(plain: str) -> None:
    """密码安全前置校验：非空 + bcrypt 长度限制内。

    本阶段不建设企业密码复杂度策略（大小写/数字/特殊字符/历史/过期均不要求），
    只做不允许空密码与底层长度限制的安全明确处理。
    """
    if not plain:
        raise bad_request("密码不能为空")
    if len(plain.encode("utf-8")) > BCRYPT_MAX_BYTES:
        raise bad_request("密码过长（超过 72 字节）")


def hash_password(plain: str) -> str:
    validate_password_policy(plain)
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


def _ceil_seconds(dt: datetime) -> int:
    """把（可能是微秒精度）时间向上取整到秒的时间戳。"""
    aware = to_utc_aware(dt)
    ts = aware.timestamp()
    return math.ceil(ts)


def is_iat_before_password_change(iat: int, password_changed_at: datetime) -> bool:
    """判断 JWT 签发时间是否早于最近密码变更时间。

    返回 True 表示该 Token 应视为旧 Token（拒绝）。
    DATETIME(6) 微秒精度 vs JWT 秒级 iat：统一用 `ceil(密码变更时间)` 比较，
    配合签发侧同秒提升 iat 的逻辑（见 create_access_token），保证：
    - 重置前的旧 Token 立即失效（含同秒场景）；
    - 重置后立即登录的新 Token 不被误判。
    """
    return iat < _ceil_seconds(password_changed_at)


def create_access_token(
    *,
    user_id: str,
    session_id: str,
    jti: str,
    role: str,
    password_changed_at: datetime | None = None,
    expires_seconds: int | None = None,
) -> tuple[str, int]:
    """签发 JWT，返回 (token, expires_in)。

    password_changed_at 用于处理同秒边界：若签发秒与最近密码变更秒相同，
    iat 取下一秒，避免“重置后同秒重新登录”的新 Token 被 `is_iat_before_password_change`
    误判为旧 Token。
    """
    settings = get_settings()
    now = time.time()
    iat = int(now)  # floor 到秒
    if password_changed_at is not None:
        changed_sec = int(to_utc_aware(password_changed_at).timestamp())
        if iat == changed_sec:
            iat += 1
    expires_in = expires_seconds or settings.jwt_expires_seconds
    payload: dict[str, Any] = {
        "sub": user_id,
        "sid": session_id,
        "jti": jti,
        "role": role,
        "iat": iat,
        "exp": iat + expires_in,
    }
    token = jwt.encode(payload, settings.secret_key, algorithm=ALGORITHM)
    return token, expires_in


def decode_access_token(token: str) -> dict[str, Any]:
    """解码并校验签名与过期；失败抛 jwt 异常由调用方映射为 AUTH_REQUIRED。

    关闭 PyJWT 对 `iat` 的“不得晚于当前时间”校验：平台在签发时可能为处理
    DATETIME(6) 与秒级 iat 的同秒边界，有意把 iat 提升到下一秒（见
    `create_access_token`）；iat 与 `password_changed_at` 的语义比较由
    `is_iat_before_password_change` 在鉴权链中自行完成。签名与 exp 校验保持开启。
    """
    settings = get_settings()
    return jwt.decode(
        token,
        settings.secret_key,
        algorithms=[ALGORITHM],
        options={"verify_iat": False},
    )
