"""用户管理 DTO（仅管理员）：创建/修改/重置密码/分页列表（《API 接口设计》§5）。"""

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.common import ApiResponse, Page

USER_SCHEMA_CONFIG = ConfigDict(extra="forbid")


class UserCreateRequest(BaseModel):
    model_config = USER_SCHEMA_CONFIG

    username: str = Field(min_length=1, max_length=64)
    display_name: str = Field(min_length=1, max_length=100)
    role: str = Field(pattern="^(admin|employee)$")
    initial_password: str = Field(min_length=1, max_length=256)


class UserUpdateRequest(BaseModel):
    model_config = USER_SCHEMA_CONFIG

    display_name: str | None = Field(default=None, min_length=1, max_length=100)
    role: str | None = Field(default=None, pattern="^(admin|employee)$")
    status: str | None = Field(default=None, pattern="^(active|disabled)$")


class ResetPasswordRequest(BaseModel):
    model_config = USER_SCHEMA_CONFIG

    new_password: str = Field(min_length=1, max_length=256)


class UserView(BaseModel):
    id: str
    username: str
    display_name: str
    role: str
    status: str
    last_login_at: str | None = None
    created_at: str | None = None
    updated_at: str | None = None


class UserListData(Page[UserView]):
    pass


class UserListResponse(ApiResponse[UserListData]):
    pass


class UserViewResponse(ApiResponse[UserView]):
    pass


class UserMessageData(BaseModel):
    id: str
    message: str = "操作成功"


class UserMessageResponse(ApiResponse[UserMessageData]):
    pass
