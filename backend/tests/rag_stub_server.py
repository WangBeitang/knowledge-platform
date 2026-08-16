"""本地 RAG 导入服务 stub（仅冒烟用，模拟原 RAG Dataset/member 行为）。

模拟语义（对照原 RAG 实际源码）：
- 所有请求必须携带 X-User-Id（缺失 → 400）；
- POST /datasets：创建，dataset_id 重复 → 500（上游不幂等，平台先查后建）；
- GET /datasets/{id}：存在返回 200，不存在 → 404；
- GET /datasets/{id}/members：成员列表；
- POST /datasets/{id}/members：upsert（幂等）；owner 不能通过 members 修改 → 403。
"""

import uuid

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

app = FastAPI(title="rag-import-stub")

DATASETS: dict[str, dict] = {}
MEMBERS: dict[tuple[str, str], dict] = {}


@app.middleware("http")
async def require_user_id(request: Request, call_next):
    if not request.headers.get("X-User-Id", "").strip():
        return JSONResponse(status_code=400, content={"detail": "缺少 X-User-Id 请求头"})
    return await call_next(request)


@app.post("/datasets")
async def create_dataset(request: Request):
    body = await request.json()
    dataset_id = body["dataset_id"]
    if dataset_id in DATASETS:
        return JSONResponse(status_code=500, content={"detail": "duplicate dataset_id"})
    dataset = {
        "dataset_id": dataset_id,
        "name": body.get("name", ""),
        "description": body.get("description", ""),
        "owner_user_id": request.headers["X-User-Id"],
        "visibility": body.get("visibility", "private"),
        "status": "active",
        "document_count": 0,
        "member_count": 0,
    }
    DATASETS[dataset_id] = dataset
    return dataset


@app.get("/datasets/{dataset_id}")
async def get_dataset(dataset_id: str):
    if dataset_id not in DATASETS:
        return JSONResponse(status_code=404, content={"detail": "dataset 不存在"})
    return DATASETS[dataset_id]


@app.get("/datasets/{dataset_id}/members")
async def list_members(dataset_id: str):
    if dataset_id not in DATASETS:
        return JSONResponse(status_code=404, content={"detail": "dataset 不存在"})
    items = [m for (ds, _u), m in MEMBERS.items() if ds == dataset_id]
    return {"code": 200, "dataset_id": dataset_id, "items": items}


@app.post("/datasets/{dataset_id}/members")
async def upsert_member(dataset_id: str, request: Request):
    if dataset_id not in DATASETS:
        return JSONResponse(status_code=404, content={"detail": "dataset 不存在"})
    body = await request.json()
    user_id = body["user_id"]
    if user_id == DATASETS[dataset_id]["owner_user_id"]:
        return JSONResponse(status_code=403, content={"detail": "owner 不能通过 members API 修改"})
    member = {
        "member_id": f"member_{uuid.uuid4().hex}",
        "dataset_id": dataset_id,
        "user_id": user_id,
        "role": body.get("role", "viewer"),
        "added_by_user_id": request.headers["X-User-Id"],
    }
    MEMBERS[(dataset_id, user_id)] = member
    return member


@app.delete("/datasets/{dataset_id}/members/{member_user_id}")
async def remove_member(dataset_id: str, member_user_id: str):
    MEMBERS.pop((dataset_id, member_user_id), None)
    return {"code": 200, "dataset_id": dataset_id, "user_id": member_user_id, "removed": True}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8011)
