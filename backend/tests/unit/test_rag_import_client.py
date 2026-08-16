"""RAG 客户端单元测试：GET 有限重试、POST 不自动重发、共享 client 生命周期（§1/§2）。"""

import asyncio

import httpx
import pytest

from app.rag.rag_errors import RAG_BAD_RESPONSE_CODE, RAG_TIMEOUT_CODE, RAG_UNAVAILABLE_CODE
from app.rag.rag_import_client import (
    RagImportClient,
    close_rag_import_client,
    get_rag_import_client,
)

OK_JSON = {"dataset_id": "d1", "name": "d1"}
MEMBER_JSON = {"code": 200, "dataset_id": "d1", "items": []}


def _connect_error(request: httpx.Request) -> httpx.ConnectError:
    return httpx.ConnectError("connection refused", request=request)


class TestGetRetry:
    def test_retries_until_success(self):
        calls = {"n": 0}

        async def handler(request: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            if calls["n"] < 3:
                raise _connect_error(request)
            return httpx.Response(200, json=OK_JSON)

        client = RagImportClient(
            base_url="http://stub", transport=httpx.MockTransport(handler), retry_backoff_base=0.01
        )

        async def run() -> None:
            try:
                result = await client.get_dataset("d1", service_user="svc")
                assert result == OK_JSON
            finally:
                await client.aclose()

        asyncio.run(run())
        assert calls["n"] == 3  # 首次 + 2 次重试

    def test_connect_failure_beyond_limit_maps_to_unavailable(self):
        calls = {"n": 0}

        async def handler(request: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            raise _connect_error(request)

        client = RagImportClient(
            base_url="http://stub", transport=httpx.MockTransport(handler), retry_backoff_base=0.01
        )

        async def run() -> None:
            try:
                with pytest.raises(Exception) as exc_info:
                    await client.get_dataset("d1", service_user="svc")
                assert exc_info.value.code == RAG_UNAVAILABLE_CODE
            finally:
                await client.aclose()

        asyncio.run(run())
        assert calls["n"] == 3  # 到达上限

    def test_timeout_failure_beyond_limit_maps_to_timeout(self):
        async def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ReadTimeout("read timed out", request=request)

        client = RagImportClient(
            base_url="http://stub", transport=httpx.MockTransport(handler), retry_backoff_base=0.01
        )

        async def run() -> None:
            try:
                with pytest.raises(Exception) as exc_info:
                    await client.list_dataset_members("d1", service_user="svc")
                assert exc_info.value.code == RAG_TIMEOUT_CODE
            finally:
                await client.aclose()

        asyncio.run(run())

    def test_get_404_is_not_retried_as_success(self):
        # 404 是业务语义（不存在），不触发网络重试，也不映射为错误
        calls = {"n": 0}

        async def handler(request: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            return httpx.Response(404, json={"detail": "not found"})

        client = RagImportClient(
            base_url="http://stub", transport=httpx.MockTransport(handler), retry_backoff_base=0.01
        )

        async def run() -> None:
            try:
                assert await client.get_dataset("missing", service_user="svc") is None
            finally:
                await client.aclose()

        asyncio.run(run())
        assert calls["n"] == 1  # 不重试


class TestPostNoRetry:
    def test_create_does_not_auto_retry_on_network_error(self):
        calls = {"n": 0}

        async def handler(request: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            raise _connect_error(request)

        client = RagImportClient(
            base_url="http://stub", transport=httpx.MockTransport(handler), retry_backoff_base=0.01
        )

        async def run() -> None:
            try:
                with pytest.raises(Exception) as exc_info:
                    await client.create_dataset(dataset_id="d1", name="d1", service_user="svc")
                assert exc_info.value.code == RAG_UNAVAILABLE_CODE
            finally:
                await client.aclose()

        asyncio.run(run())
        assert calls["n"] == 1  # 连接断开不确定是否已接收，不自动重发

    def test_upsert_member_does_not_auto_retry(self):
        calls = {"n": 0}

        async def handler(request: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            raise _connect_error(request)

        client = RagImportClient(
            base_url="http://stub", transport=httpx.MockTransport(handler), retry_backoff_base=0.01
        )

        async def run() -> None:
            try:
                with pytest.raises(Exception) as exc_info:
                    await client.upsert_member(
                        dataset_id="d1",
                        member_user_id="u1",
                        role="viewer",
                        operator_service_user="svc",
                    )
                assert exc_info.value.code == RAG_UNAVAILABLE_CODE
            finally:
                await client.aclose()

        asyncio.run(run())
        assert calls["n"] == 1

    def test_unexpected_status_maps_bad_response(self):
        async def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(500, text="<html>boom</html>")

        client = RagImportClient(
            base_url="http://stub", transport=httpx.MockTransport(handler), retry_backoff_base=0.01
        )

        async def run() -> None:
            try:
                with pytest.raises(Exception) as exc_info:
                    await client.get_dataset("d1", service_user="svc")
                assert exc_info.value.code == RAG_BAD_RESPONSE_CODE
            finally:
                await client.aclose()

        asyncio.run(run())


class TestSharedClientLifecycle:
    def test_shared_client_singleton_and_close(self, monkeypatch):
        import app.rag.rag_import_client as mod

        monkeypatch.setattr(mod, "_shared_client", None)
        c1 = get_rag_import_client()
        c2 = get_rag_import_client()
        assert c1 is c2  # 正常应用路径复用同一实例

        async def close_and_recreate() -> None:
            await close_rag_import_client()
            assert mod._shared_client is None
            c3 = get_rag_import_client()
            assert c3 is not c1
            await close_rag_import_client()

        asyncio.run(close_and_recreate())
