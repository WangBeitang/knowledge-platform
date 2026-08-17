"""阶段 3 真实链路冒烟脚本（uvicorn 平台 + 独立 RAG stub + 服务器 MySQL/Redis）。

前置：
1. 启动 RAG stub：  .venv/bin/python -m uvicorn tests.rag_smoke_stub_server:app --port 8001
2. 启动平台：       .venv/bin/python -m uvicorn app.main:app --port 8000
3. 本脚本：         .venv/bin/python tests/smoke_stage3.py

验证点：登录 → 导入(PDF) → 任务轮询 succeeded → 文档列表/详情 → Chunk 分页
（offset 生效，第 2 页数据不同）→ Chunk 启停 → 替换（202+轮询，旧文档删除）→ 删除。
"""

import io
import sys
import time

import httpx

BASE = "http://127.0.0.1:8000/api/v1"

MINIMAL_PDF = b"%PDF-1.4\n1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 612 792]>>endobj\ntrailer<</Root 1 0 R>>\n%%EOF"

passed: list[str] = []
failed: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        passed.append(name)
        print(f"  [PASS] {name}")
    else:
        failed.append(name)
        print(f"  [FAIL] {name} {detail}")


def main() -> int:
    c = httpx.Client(base_url=BASE, timeout=30)
    print("== 1. 登录 ==")
    r = c.post("/auth/login", json={"username": "admin", "password": "AdminNew#2026"})
    check("admin 登录", r.status_code == 200, f"HTTP {r.status_code}: {r.text[:200]}")
    token = r.json()["data"]["access_token"]
    headers = {"Authorization": f"Bearer {token}"}

    print("== 2. 导入 PDF ==")
    r = c.post(
        "/admin/documents/import",
        headers=headers,
        data={"knowledge_scope": "internal_shared"},
        files={"files": ("demo_smoke.pdf", io.BytesIO(MINIMAL_PDF), "application/pdf")},
    )
    check("导入返回 200", r.status_code == 200, f"HTTP {r.status_code}: {r.text[:300]}")
    data = r.json()["data"]
    check("导入 submitted_count=1", data["submitted_count"] == 1, str(data))
    item = data["items"][0]
    check("导入 item pending 且含 task_id", item["status"] == "pending" and item["task_id"], str(item))
    task_id = item["task_id"]
    document_id = item["document_id"]

    print("== 3. 任务轮询 ==")
    task_status = None
    for _ in range(30):
        tr = c.get(f"/admin/integration/tasks/{task_id}", headers=headers)
        if tr.status_code == 200:
            task_status = tr.json()["data"]["status"]
            if task_status in ("succeeded", "failed", "cancelled"):
                break
        time.sleep(1)
    check("导入任务 succeeded", task_status == "succeeded", f"status={task_status}")

    print("== 4. 文档列表/详情 ==")
    r = c.get("/admin/documents", headers=headers)
    check("文档列表 200", r.status_code == 200)
    docs = r.json()["data"]["items"]
    check("列表含新文档且 active", any(d["id"] == document_id and d["platform_status"] == "active" for d in docs))
    r = c.get(f"/admin/documents/{document_id}", headers=headers)
    check("文档详情 200", r.status_code == 200)
    detail = r.json()["data"]
    check("详情 chunk_count=5", detail.get("chunk_count") == 5, f"chunk_count={detail.get('chunk_count')}")

    print("== 5. Chunk 分页（offset 生效）==")
    p1 = c.get(f"/admin/documents/{document_id}/chunks?page=1&page_size=2", headers=headers)
    p2 = c.get(f"/admin/documents/{document_id}/chunks?page=2&page_size=2", headers=headers)
    check("第1页 200", p1.status_code == 200)
    check("第2页 200", p2.status_code == 200)
    items1 = p1.json()["data"]["items"]
    items2 = p2.json()["data"]["items"]
    check("第1页 2 条", len(items1) == 2, f"len={len(items1)}")
    check("第2页 2 条且与第1页不同", len(items2) == 2 and items1[0]["chunk_id"] != items2[0]["chunk_id"],
          f"p1={[i['chunk_id'] for i in items1]} p2={[i['chunk_id'] for i in items2]}")
    check("total=5", p1.json()["data"]["total"] == 5, str(p1.json()["data"]["total"]))
    check("position 连续", items1[0]["position"] == 0 and items2[0]["position"] == 2,
          f"p1 pos={items1[0]['position']} p2 pos={items2[0]['position']}")

    print("== 6. Chunk 启停 ==")
    chunk = items1[0]
    r = c.patch(
        f"/admin/documents/{document_id}/chunks/{chunk['chunk_id']}/enabled",
        headers=headers,
        json={
            "enabled": False,
            "reason_code": "parse_error",
            "reason_text": "",
            "expected_index_version": chunk["index_version"],
        },
    )
    check("停用 chunk 200", r.status_code == 200, f"HTTP {r.status_code}: {r.text[:200]}")
    check("停用后 enabled=false", r.json()["data"]["enabled"] is False, str(r.json()["data"]))
    r = c.patch(
        f"/admin/documents/{document_id}/chunks/{chunk['chunk_id']}/enabled",
        headers=headers,
        json={
            "enabled": True,
            "reason_code": "human_misjudgment",
            "reason_text": "",
            "expected_index_version": chunk["index_version"],
        },
    )
    check("恢复 chunk 200", r.status_code == 200)
    check("恢复后 enabled=true", r.json()["data"]["enabled"] is True)

    print("== 7. 替换（202 + 轮询，旧文档删除）==")
    r = c.post(
        f"/admin/documents/{document_id}/replace",
        headers=headers,
        data={"knowledge_scope": "internal_shared"},
        files={"file": ("demo_smoke_v2.pdf", io.BytesIO(MINIMAL_PDF), "application/pdf")},
    )
    check("替换返回 202 + task_id", r.status_code == 202 and r.json()["data"].get("task_id"), f"HTTP {r.status_code}: {r.text[:300]}")
    replace_task_id = r.json()["data"]["task_id"]
    new_document_id = r.json()["data"]["new_document_id"]
    replace_status = None
    for _ in range(30):
        tr = c.get(f"/admin/integration/tasks/{replace_task_id}", headers=headers)
        if tr.status_code == 200:
            replace_status = tr.json()["data"]["status"]
            if replace_status in ("succeeded", "failed", "cancelled"):
                break
        time.sleep(1)
    check("替换任务 succeeded", replace_status == "succeeded", f"status={replace_status}")
    r = c.get(f"/admin/documents/{document_id}", headers=headers)
    check("旧文档详情 404（终态不可操作）", r.status_code == 404, f"HTTP {r.status_code}: {r.text[:200]}")
    r = c.get("/admin/documents?platform_status=replaced", headers=headers)
    docs = r.json()["data"]["items"]
    old_view = next((d for d in docs if d["id"] == document_id), None)
    check("旧文档列表标记 replaced", old_view is not None and old_view["platform_status"] == "replaced",
          f"old_view={old_view}")
    r = c.get(f"/admin/documents/{new_document_id}", headers=headers)
    check("新文档 active", r.status_code == 200 and r.json()["data"]["platform_status"] == "active")

    print("== 8. 删除新文档 ==")
    r = c.delete(f"/admin/documents/{new_document_id}", headers=headers)
    check("删除 200", r.status_code == 200, f"HTTP {r.status_code}: {r.text[:200]}")
    r = c.get(f"/admin/documents/{new_document_id}", headers=headers)
    check("删除后详情 404", r.status_code == 404, f"HTTP {r.status_code}: {r.text[:200]}")
    r = c.get("/admin/documents?platform_status=deleted", headers=headers)
    docs = r.json()["data"]["items"]
    new_view = next((d for d in docs if d["id"] == new_document_id), None)
    check("删除后列表标记 deleted", new_view is not None and new_view["platform_status"] == "deleted",
          f"new_view={new_view}")

    print()
    print(f"通过 {len(passed)} 项，失败 {len(failed)} 项")
    if failed:
        print("失败项：", failed)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
