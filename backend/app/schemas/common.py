"""通用 Pydantic DTO：响应壳、分页、错误体。"""

from typing import Any, Generic, TypeVar

from pydantic import BaseModel

T = TypeVar("T")


class ApiResponse(BaseModel, Generic[T]):
    request_id: str
    data: T


class Page(BaseModel, Generic[T]):
    items: list[T]
    page: int
    page_size: int
    total: int


class ErrorBody(BaseModel):
    code: str
    message: str
    retryable: bool
    details: dict[str, Any] = {}
