"""稳定错误模型：AppError + 稳定错误码 + 全局异常处理器。

错误码清单以《API 接口设计》第 3 节为准（v1.1 已移除 IDEMPOTENCY_CONFLICT）。
message 只允许向用户展示安全文案，禁止堆栈/SQL/密钥/上游原始内部异常。
"""

from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app.core.request_context import get_request_id


class AppError(Exception):
    """业务异常：携带稳定错误码与是否可重试。"""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        status_code: int = 400,
        retryable: bool = False,
        details: dict[str, Any] | None = None,
        acceptance_ambiguous: bool = False,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code
        self.retryable = retryable
        self.details = details or {}
        # 网络层不确定（timeout/connection）：调用方无法判断上游是否已接受请求。
        # 仅当为 True 时，平台必须保留 acceptance-ambiguous 状态，禁止直接释放。
        self.acceptance_ambiguous = acceptance_ambiguous


# ---- 常用错误快捷构造 ----


def not_found(message: str = "资源不存在或无权访问") -> AppError:
    return AppError("RESOURCE_NOT_FOUND", message, status_code=404)


def forbidden(message: str = "无权访问") -> AppError:
    return AppError("PERMISSION_DENIED", message, status_code=403)


def bad_request(message: str, code: str = "INVALID_REQUEST") -> AppError:
    return AppError(code, message, status_code=400)


def conflict(message: str) -> AppError:
    return AppError("RESOURCE_CONFLICT", message, status_code=409)


def rag_unavailable(message: str = "知识检索服务暂时不可用，请稍后重试") -> AppError:
    return AppError("RAG_UNAVAILABLE", message, status_code=503, retryable=True)


# ---- 错误响应组装 ----


def error_body(request_id: str, error: AppError) -> dict[str, Any]:
    return {
        "request_id": request_id,
        "error": {
            "code": error.code,
            "message": error.message,
            "retryable": error.retryable,
            "details": error.details,
        },
    }


def register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppError)
    async def _handle_app_error(request: Request, exc: AppError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content=error_body(get_request_id(), exc),
        )

    @app.exception_handler(RequestValidationError)
    async def _handle_validation_error(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        err = AppError(
            "VALIDATION_ERROR",
            "请求参数校验失败",
            status_code=422,
            details={"fields": exc.errors()[:10]},
        )
        return JSONResponse(
            status_code=422,
            content=error_body(get_request_id(), err),
        )

    @app.exception_handler(Exception)
    async def _handle_unexpected(request: Request, exc: Exception) -> JSONResponse:
        # 未预期异常：记完整堆栈到日志，对外只返回安全文案
        from app.core.logging import get_logger

        get_logger("errors").exception("unhandled error")
        err = AppError(
            "INTERNAL_ERROR",
            "服务内部错误",
            status_code=500,
            retryable=True,
        )
        return JSONResponse(
            status_code=500,
            content=error_body(get_request_id(), err),
        )
