"""安全工具：密码哈希、JWT 签发/校验、会话标识、密码变更时间精度处理。

规则（冻结）：
- 密码使用 bcrypt 安全哈希，禁止明文；空密码与超长密码明确拒绝（不产生 500）；
- JWT 至少包含 sub/sid/jti/role/iat/exp；会话撤销、用户状态、密码变更时间由
  auth 层依赖校验，本模块只做编解码与精度相关的判定；
- `auth_sessions` 只保存 jti 的 SHA-256，禁止保存完整 JWT。

精度约定（DATETIME(6) 与 JWT 秒级 iat）：
- JWT `iat` 始终是**真实签发秒**（不制造未来时间），PyJWT 标准 `verify_iat` 保持开启；
- 同秒边界由 `auth_sessions.issued_at`（DATETIME(6)，微秒精度）解决，规则见
  `is_iat_before_password_change`：
  1. `iat` 明显早于 `password_changed_at` 所在秒 → 旧 Token；
  2. 明显晚于 → 新 Token；
  3. 二者同秒 → 用微秒级 `session.issued_at < password_changed_at` 判定旧 Token。
"""

import hashlib
import hmac
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


def external_subject_hash(external_user_id: str) -> str:
    """外部用户标识的加盐哈希（HMAC-SHA256，64 hex = CHAR(64)）。

    冻结规则（API §10 / 数据对象 §4.6、§4.8）：
    - 不保存原始外部用户 ID，只保存确定性加盐哈希；
    - 盐复用现有 `SECRET_KEY`（不新增配置/不写库/不建表）；
    - 只用于 UV 去重、日志关联与会话映射，不参与知识权限计算。
    """
    settings = get_settings()
    digest = hmac.new(
        settings.secret_key.encode("utf-8"),
        external_user_id.encode("utf-8"),
        hashlib.sha256,
    )
    return digest.hexdigest()


def verify_service_key(provided: str | None) -> bool:
    """外部 Service API Key 常量时间校验（冻结 §16：来自 env，不写库/不落日志）。"""
    expected = get_settings().service_api_key
    if not expected or not provided:
        return False
    return hmac.compare_digest(provided.encode("utf-8"), expected.encode("utf-8"))


def is_iat_before_password_change(
    iat: int,
    session_issued_at: datetime,
    password_changed_at: datetime,
) -> bool:
    """判断 JWT 是否应视为旧 Token（签发早于最近密码变更）。

    DATETIME(6) 微秒精度 vs JWT 秒级 iat 的同秒边界处理：
    1. `iat` 明显早于 `password_changed_at` 所在秒 → 旧 Token（True）；
    2. 明显晚于 → 新 Token（False）；
    3. 同秒 → 用会话微秒级签发时间 `session_issued_at` 与 `password_changed_at`
       比较：会话签发早于密码变更 → 旧 Token（True）。

    这样保证：重置前同秒签发的旧 Token 失效、重置后同秒重新登录的新 Token 有效，
    且不制造未来 iat、不关闭标准 iat 校验。
    """
    pca_aware = to_utc_aware(password_changed_at)
    pca_sec = int(pca_aware.timestamp())  # 变更所在秒（floor）
    if iat < pca_sec:
        return True
    if iat > pca_sec:
        return False
    # 同一秒：微秒级比较会话签发时间与密码变更时间
    return to_utc_aware(session_issued_at) < pca_aware


def create_access_token(
    *,
    user_id: str,
    session_id: str,
    jti: str,
    role: str,
    expires_seconds: int | None = None,
) -> tuple[str, int]:
    """签发 JWT，返回 (token, expires_in)。

    `iat` 为真实签发秒（floor(now)），不制造未来签发时间；
    同秒边界由鉴权链用 `auth_sessions.issued_at` 微秒值解决。
    """
    settings = get_settings()
    iat = int(time.time())
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
    """解码并校验签名、过期与标准 iat 校验；失败抛 jwt 异常由调用方映射为 AUTH_REQUIRED。"""
    settings = get_settings()
    return jwt.decode(token, settings.secret_key, algorithms=[ALGORITHM])
