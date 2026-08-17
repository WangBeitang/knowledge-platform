"""进程内原 RAG Query Fake（MockTransport）：Stage 4 问答契约模拟。

模拟语义与真实原 RAG（query_server.py）一致：
- 所有请求必须携带 X-User-Id；
- POST /query → {session_id, message}（记录调用参数供断言）；
- GET /stream/{session_id} → SSE（ready/progress/delta/final/error）；
- GET /status/{session_id} → QueryTaskStatusResponse 结构；
- GET /traces/{trace_id} → project_trace_summary 结构（含 token_usage）。

测试控制：
- seed_stream / seed_status / seed_trace 预设数据；
- stream_delay：让 SSE 响应延迟（用于并发 409 时序测试）；
- fail_submit：注入提交异常（503/504/502）；
- drop_stream：模拟无终态断开（流结束但没有 final/error）。
"""

import asyncio
import json

import httpx

SSE_HEADERS = {"Content-Type": "text/event-stream"}


def _sse_pack(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


class FakeQueryRag:
    def __init__(self) -> None:
        self.query_calls: list[dict] = []
        self.streams: dict[str, list[tuple[str, dict]]] = {}
        self.default_stream: list[tuple[str, dict]] | None = None  # 任意未知 session 的兜底流
        self.statuses: dict[str, dict] = {}
        self.traces: dict[str, dict] = {}
        self.stream_delay: float = 0.0
        self.status_delay: float = 0.0  # /status 响应延迟（用于并发恢复竞态测试）
        self.fail_submit: str | None = None  # "unavailable" | "timeout" | "bad"
        self.submit_network_error: bool = False  # POST /query 连接层断链（接受性歧义）
        self.drop_stream: bool = False  # 流结束但无终态
        self.stream_network_error: bool = False  # 流式连接抛网络错误（模拟 socket 断流）
        self.submit_session_id_override: str | None = (
            None  # 契约错误：返回与请求不一致的 session_id
        )
        self.status_calls: list[str] = []  # 记录 /status 触达的 session_id（含重试）

    # ---------- 测试控制 ----------

    def seed_stream(self, session_id: str, events: list[tuple[str, dict]]) -> None:
        self.streams[session_id] = events

    def seed_default_stream(self, events: list[tuple[str, dict]]) -> None:
        """为测试内未知 session（路由动态生成的 UUID）提供兜底 SSE 流。"""
        self.default_stream = events

    def seed_status(self, session_id: str, payload: dict) -> None:
        self.statuses[session_id] = payload

    def seed_trace(self, trace_id: str, token_usage: dict) -> None:
        self.traces[trace_id] = {
            "trace_id": trace_id,
            "session_id": "session-x",
            "owner_user_id": "svc_knowledge_employee",
            "dataset_ids": ["securities_internal_shared"],
            "original_query": "q",
            "status": "completed",
            "planner_type": "rule",
            "policy_version": "v1",
            "retrieval_config_version": "v1",
            "planner_steps": [],
            "channel_hits": [],
            "final_citations": [],
            "total_duration_ms": 10,
            "terminal_action": "answer",
            "terminal_reason_code": "completed",
            "execution_source": "chat",
            "token_usage": token_usage,
        }

    # ---------- HTTP handler ----------

    async def handler(self, request: httpx.Request) -> httpx.Response:
        if not request.headers.get("X-User-Id", "").strip():
            return httpx.Response(400, json={"detail": "缺少 X-User-Id 请求头"})
        path = request.url.path
        method = request.method
        parts = [p for p in path.split("/") if p]

        if method == "POST" and path == "/query":
            return await self._handle_query(request)
        if method == "GET" and len(parts) == 2 and parts[0] == "stream":
            return await self._handle_stream(parts[1])
        if method == "GET" and len(parts) == 2 and parts[0] == "status":
            return await self._handle_status(parts[1])
        if method == "GET" and len(parts) == 2 and parts[0] == "traces":
            return self._handle_trace(parts[1])
        return httpx.Response(404, json={"detail": "not found"})

    async def _handle_query(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        self.query_calls.append(
            {
                "query": body.get("query"),
                "session_id": body.get("session_id"),
                "is_stream": body.get("is_stream"),
                "dataset_ids": list(body.get("dataset_ids") or []),
                "service_user": request.headers["X-User-Id"],
            }
        )
        if self.fail_submit == "unavailable":
            return httpx.Response(503, json={"detail": "upstream unavailable"})
        if self.fail_submit == "timeout":
            return httpx.Response(504, json={"detail": "upstream timeout"})
        if self.fail_submit == "bad":
            return httpx.Response(200, content=b"<html>not json</html>")
        if self.submit_network_error:
            # 连接层断链：上游是否已接受本请求存在不确定性（acceptance-ambiguous）
            raise httpx.ConnectError("simulated submit socket error", request=request)
        returned_session_id = self.submit_session_id_override or body.get("session_id")
        return httpx.Response(
            200, json={"message": "查询任务已开始", "session_id": returned_session_id}
        )

    async def _handle_stream(self, session_id: str) -> httpx.Response:
        if self.stream_network_error:
            # 模拟流式 socket 断开（连接层异常，不是 HTTP 状态码）
            raise httpx.ConnectError("simulated stream socket error", request=None)
        if self.stream_delay > 0:
            await asyncio.sleep(self.stream_delay)
        events = self.streams.get(session_id, self.default_stream or [])
        body = "".join(_sse_pack(event, data) for event, data in events)
        return httpx.Response(200, content=body, headers=SSE_HEADERS)

    async def _handle_status(self, session_id: str) -> httpx.Response:
        self.status_calls.append(session_id)
        if self.status_delay > 0:
            await asyncio.sleep(self.status_delay)
        status = self.statuses.get(session_id)
        if status is None:
            return httpx.Response(404, json={"detail": f"session {session_id} 不存在"})
        return httpx.Response(200, json={"code": 200, **status})

    def _handle_trace(self, trace_id: str) -> httpx.Response:
        trace = self.traces.get(trace_id)
        if trace is None:
            return httpx.Response(404, json={"detail": f"trace {trace_id} 不存在"})
        return httpx.Response(200, json={"code": 200, **trace})
