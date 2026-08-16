"""认证 DTO：登录请求/响应、当前用户视图（《API 接口设计》§2.4 / §4.2）。"""

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.common import ApiResponse

AUTH_SCHEMA_CONFIG = ConfigDict(extra="forbid")


class LoginRequest(BaseModel):
    model_config = AUTH_SCHEMA_CONFIG

    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=256)


class LoginUserView(BaseModel):
    id: str
    username: str
    display_name: str
    role: str
    status: str


class LoginData(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int
    user: LoginUserView


class LoginResponse(ApiResponse[LoginData]):
    pass


class MeView(BaseModel):
    id: str
    username: str
    display_name: str
    role: str
    status: str
    created_at: str | None = None
    last_login_at: str | None = None


class MeResponse(ApiResponse[MeView]):
    pass


class LogoutData(BaseModel):
    ok: bool = True


class LogoutResponse(ApiResponse[LogoutData]):
    pass
