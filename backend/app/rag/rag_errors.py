"""上游（原 RAG）错误映射：断链/超时/契约违规 → 稳定错误码。

映射规则（冻结 API §15 / SPEC §5.4）：
- 连接失败 → 503 RAG_UNAVAILABLE（可重试）
- 超时     → 504 RAG_TIMEOUT（可重试）
- 上游违反预期契约（HTML 页、无字段 JSON、未知状态等）→ 502 RAG_BAD_RESPONSE（可重试）

不得把 404、HTML 错误页、无字段 JSON、未知状态静默当成成功。
"""

from typing import Any

import httpx

from app.core.errors import AppError

RAG_UNAVAILABLE_CODE = "RAG_UNAVAILABLE"
RAG_TIMEOUT_CODE = "RAG_TIMEOUT"
RAG_BAD_RESPONSE_CODE = "RAG_BAD_RESPONSE"


class RagError(AppError):
    """上游调用失败（已映射为稳定错误码）。"""


def rag_unavailable(message: str = "知识检索服务暂时不可用，请稍后重试") -> RagError:
    return RagError(RAG_UNAVAILABLE_CODE, message, status_code=503, retryable=True)


def rag_timeout(message: str = "知识检索服务响应超时，请稍后重试") -> RagError:
    return RagError(RAG_TIMEOUT_CODE, message, status_code=504, retryable=True)


def rag_bad_response(message: str = "知识检索服务返回了无法识别的响应") -> RagError:
    return RagError(RAG_BAD_RESPONSE_CODE, message, status_code=502, retryable=True)


def map_http_error(exc: httpx.HTTPError) -> RagError:
    """把 HTTPX 异常映射为稳定 RagError。"""
    if isinstance(exc, httpx.TimeoutException):
        return rag_timeout()
    if isinstance(exc, httpx.HTTPError):
        return rag_unavailable()
    return rag_unavailable()


def parse_json_response(response: httpx.Response) -> dict[str, Any]:
    """安全解析上游 JSON 响应；违反契约（非 JSON / 无 body）→ RAG_BAD_RESPONSE。

    404 等状态码由调用方按业务语义处理；这里只负责把“响应体不符合 JSON 契约”
    映射为 RAG_BAD_RESPONSE，禁止静默当成成功。
    """
    content_type = response.headers.get("content-type", "")
    if "application/json" not in content_type and not response.text.strip().startswith("{"):
        raise rag_bad_response("上游返回了非 JSON 响应")
    try:
        payload = response.json()
    except ValueError:
        raise rag_bad_response("上游返回的 JSON 无法解析") from None
    if not isinstance(payload, dict):
        raise rag_bad_response("上游返回的 JSON 结构不符合契约")
    return payload
