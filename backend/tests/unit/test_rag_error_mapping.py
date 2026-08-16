"""RAG 错误映射单元测试：断链/超时/契约违规 → 稳定错误码（SPEC §5.4 / API §15）。

禁止把 404、HTML 错误页、无字段 JSON、未知状态静默当成成功。
"""

import httpx
import pytest

from app.rag.rag_errors import (
    RAG_BAD_RESPONSE_CODE,
    RAG_TIMEOUT_CODE,
    RAG_UNAVAILABLE_CODE,
    map_http_error,
    parse_json_response,
    rag_bad_response,
)


def _response(
    status_code: int = 200, content_type: str = "application/json", text: str = "{}"
) -> httpx.Response:
    return httpx.Response(
        status_code=status_code, headers={"content-type": content_type}, text=text
    )


class TestMapHttpError:
    def test_connect_error_maps_to_unavailable(self):
        exc = httpx.ConnectError("connection refused", request=httpx.Request("GET", "http://x"))
        err = map_http_error(exc)
        assert err.code == RAG_UNAVAILABLE_CODE
        assert err.status_code == 503
        assert err.retryable is True

    def test_timeout_maps_to_timeout(self):
        exc = httpx.ReadTimeout("read timed out", request=httpx.Request("GET", "http://x"))
        err = map_http_error(exc)
        assert err.code == RAG_TIMEOUT_CODE
        assert err.status_code == 504
        assert err.retryable is True

    def test_other_http_error_maps_to_unavailable(self):
        # 连接超时也属于超时类 → RAG_TIMEOUT；非超时的传输错误 → RAG_UNAVAILABLE
        exc = httpx.ConnectTimeout("connect timed out", request=httpx.Request("GET", "http://x"))
        err = map_http_error(exc)
        assert err.code == RAG_TIMEOUT_CODE

        protocol_err = httpx.RemoteProtocolError(
            "server closed connection", request=httpx.Request("GET", "http://x")
        )
        err2 = map_http_error(protocol_err)
        assert err2.code == RAG_UNAVAILABLE_CODE


class TestParseJsonResponse:
    def test_valid_json_returns_dict(self):
        payload = parse_json_response(_response(text='{"dataset_id": "d1"}'))
        assert payload == {"dataset_id": "d1"}

    def test_html_response_raises_bad_response(self):
        html = _response(content_type="text/html", text="<html><body>Gateway Error</body></html>")
        with pytest.raises(Exception) as exc_info:
            parse_json_response(html)
        assert exc_info.value.code == RAG_BAD_RESPONSE_CODE
        assert exc_info.value.status_code == 502

    def test_invalid_json_raises_bad_response(self):
        bad = _response(text="not-a-json-{")
        with pytest.raises(Exception) as exc_info:
            parse_json_response(bad)
        assert exc_info.value.code == RAG_BAD_RESPONSE_CODE

    def test_json_array_not_dict_raises_bad_response(self):
        arr = _response(text="[1,2,3]")
        with pytest.raises(Exception) as exc_info:
            parse_json_response(arr)
        assert exc_info.value.code == RAG_BAD_RESPONSE_CODE


class TestClientStatusHandling:
    """客户端层：404/非预期状态不得静默当成成功（Spec §5.4）。"""

    def test_404_is_explicit_not_found_not_success(self):
        async def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(404, json={"detail": "dataset 不存在"})

        from app.rag.rag_import_client import RagImportClient

        client = RagImportClient(base_url="http://stub", transport=httpx.MockTransport(handler))

        async def run() -> None:
            result = await client.get_dataset("missing", service_user="svc_admin")
            assert result is None  # 明确“不存在”信号，而不是成功数据

        import asyncio

        asyncio.run(run())

    def test_500_is_never_silent_success(self):
        async def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(500, text="<html>Internal Server Error</html>")

        from app.rag.rag_import_client import RagImportClient

        client = RagImportClient(base_url="http://stub", transport=httpx.MockTransport(handler))

        async def run() -> None:
            with pytest.raises(Exception) as exc_info:
                await client.get_dataset("d1", service_user="svc_admin")
            assert exc_info.value.code == RAG_BAD_RESPONSE_CODE

        import asyncio

        asyncio.run(run())


class TestRagBadResponse:
    def test_rag_bad_response_semantics(self):
        err = rag_bad_response("上游返回了无法识别的响应")
        assert err.code == RAG_BAD_RESPONSE_CODE
        assert err.status_code == 502
        assert err.retryable is True
