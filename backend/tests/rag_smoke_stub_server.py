"""阶段 3 冒烟用独立 RAG stub 服务（uvicorn 可启动）。

包装 tests/integration/fake_rag_server.FakeRag 的完整文档/Chunk 管理契约，
并增加“任务状态自动推进”逻辑，使真实 HTTP 冒烟无需手工控制上游状态：
- GET /status/{task_id} 时推进：pending → running → succeeded；
- 任务 succeeded：文档标记 completed；若文档无 chunk 则自动 seed 5 个 chunk；
- rebuild 任务 succeeded：把该文档既有 chunk 的 index_version 升到当前版本
  （模拟上游重建完成后 chunk 属于新版本）。

启动：uvicorn tests.rag_smoke_stub_server:app --port 8001
"""

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import Response

from tests.integration.fake_rag_server import FakeRag

app = FastAPI(title="rag-smoke-stub")

_fake = FakeRag()


def _advance_task(task_id: str) -> None:
    task = _fake.tasks.get(task_id)
    if not task or task["status"] in ("completed", "failed", "cancelled"):
        return
    doc_id = task.get("document_id")
    if task["status"] == "pending":
        task["status"] = "processing"
        task["running_list"] = ["upload_file", "parse_document", "index_chunks"]
        task["done_list"] = []
        return
    # processing → completed（上游合法终态，平台 TASK_STATUS_MAP 只认 completed）
    task["status"] = "completed"
    task["running_list"] = []
    task["done_list"] = ["upload_file", "parse_document", "index_chunks"]
    if not doc_id:
        return
    doc = _fake.documents.get(doc_id)
    if not doc:
        return
    doc["status"] = "completed"
    doc["parse_status"] = "completed"
    doc["index_status"] = "completed"
    version = int(doc.get("index_version", 1))
    if int(doc.get("chunk_count", 0) or 0) == 0:
        doc["chunk_count"] = 5
        _fake.seed_chunks(doc_id, 5, index_version=version)
    else:
        # rebuild/replace 完成：既有 chunk 升到当前版本（模拟重新索引）
        for (d_id, _idx), chunk in _fake.chunks.items():
            if d_id == doc_id:
                chunk["index_version"] = version


@app.api_route("/{path:path}", methods=["GET", "POST", "PATCH", "DELETE", "PUT"])
async def dispatch(request: Request, path: str) -> Response:
    if request.method == "GET" and path.startswith("status/"):
        _advance_task(path.split("/")[1])
    query = f"?{request.url.query}" if request.url.query else ""
    upstream = httpx.Request(
        method=request.method,
        url=f"http://stub/{path}{query}",
        headers={k: v for k, v in request.headers.items() if k.lower() != "host"},
        content=await request.body(),
    )
    resp = await _fake.handler(upstream)
    return Response(content=resp.content, status_code=resp.status_code, headers=dict(resp.headers))


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8001, log_level="warning")
