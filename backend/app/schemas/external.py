"""外部知识 API DTO（《API 接口设计》§10 / §11）。

冻结约束：
- 请求头 `X-Service-Key`，请求体只有 external_session_id / external_user_id / question；
- `extra="forbid"`：role / knowledge_scope / allowed_scopes / dataset_id(s) / 服务身份等
  内部字段一律 422，不得静默忽略；
- 会话 ID 1～120 字符；用户 ID 1～200 字符；问题 1～4000 字符且归一化后非空（服务层校验）；
- 外部用户 ID 只保存加盐哈希（external_subject_hash），不保存原始值、不参与权限计算。
"""

from pydantic import BaseModel, ConfigDict, Field

EXTERNAL_SCHEMA_CONFIG = ConfigDict(extra="forbid")

# 与内部问答共用同一稳定错误码（《API 接口设计》§3）
EMPTY_QUESTION_CODE = "EMPTY_QUESTION"


class ExternalMessageStreamRequest(BaseModel):
    """外部流式问答请求：只允许三个冻结字段，其余一律 422。"""

    model_config = EXTERNAL_SCHEMA_CONFIG

    external_session_id: str = Field(min_length=1, max_length=120)
    external_user_id: str = Field(min_length=1, max_length=200)
    question: str = Field(min_length=1, max_length=4000)
