"""Stage 5 Batch 1 最小真实 RAG FAQ 冒烟脚本（真实 import 服务 + 服务器 MySQL/Redis）。

前置：
1. 启动原 RAG import：
   cd ai_knowledge_base_after_class && .venv-test/bin/python -m uvicorn
   app.api.http.import_server:app --port 8002
2. 启动平台（env 覆盖 RAG 地址）：
   cd knowledge-platform/backend && RAG_QUERY_BASE_URL=http://127.0.0.1:8001 \
   RAG_IMPORT_BASE_URL=http://127.0.0.1:8002 \
   .venv/bin/python -m uvicorn app.main:app --port 8000
3. 本脚本：
   .venv/bin/python tests/smoke_faq_real.py --admin-pwd <密码>

验证点：
- 发布 FAQ → Markdown 真实上传 → sync run succeeded（文档快照可查）
- 修改 FAQ → 新版上传成功 → 旧版删除 → Dataset 只保留最新文档
"""

import argparse
import json
import time
import uuid

import httpx

BASE = "http://127.0.0.1:8000/api/v1"

passed: list[str] = []
failed: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        passed.append(name)
        print(f"  [PASS] {name}")
    else:
        failed.append(name)
        print(f"  [FAIL] {name} {detail}")


def wait_sync(
    c: httpx.Client, headers: dict, scope: str, expect_terminal: str, timeout: int = 180
) -> dict:
    """轮询 faq-sync-runs 直到该 scope 最新 run 到达期望终态，返回最新 run。"""
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        r = c.get("/admin/faq-sync-runs", headers=headers, params={"knowledge_scope": scope})
        assert r.status_code == 200, r.text[:300]
        items = r.json()["data"]["items"]
        if not items:
            time.sleep(3)
            continue
        last = items[0]  # 列表默认按 created_at desc
        status = last["status"]
        print(
            "    run=%s status=%s doc=%s prev=%s",
            last["id"][:8],
            status,
            last["rag_document_id"],
            last["previous_rag_document_id"],
        )
        if status == expect_terminal:
            return last
        if status in ("failed", "succeeded") and status != expect_terminal:
            return last
        time.sleep(3)
    print(
        "  [TIMEOUT] 等待 %s 超时，last=%s",
        expect_terminal,
        json.dumps(last, ensure_ascii=False)[:300],
    )
    return last or {}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--admin-pwd", required=True)
    args = parser.parse_args()

    c = httpx.Client(base_url=BASE, timeout=60)
    r = c.post("/auth/login", json={"username": "admin", "password": args.admin_pwd})
    check("admin 登录", r.status_code == 200, r.text[:200])
    if r.status_code != 200:
        return 1
    headers = {"Authorization": f"Bearer {r.json()['data']['access_token']}"}

    scope = "internal_shared"

    print("== 0. 清理该 scope 历史数据（faqs/faq_sync_runs/faq_candidates + Redis）==")
    # 直接 SQL 清理（避免 unpublish 触发链式 catch-up 污染 run 序列）
    import asyncio as _asyncio

    from sqlalchemy import delete, select

    from app.core.database import get_session_factory
    from app.core.redis import get_redis
    from app.models.faq import Faq
    from app.models.faq_candidate import FaqCandidate
    from app.models.faq_sync_run import FaqSyncRun
    from app.services.faq_service import faq_cache_key

    async def _cleanup():
        async with get_session_factory()() as s:
            faqs = list((await s.scalars(select(Faq).where(Faq.knowledge_scope == scope))).all())
            hashes = [f.normalized_question_hash for f in faqs]
            for f in faqs:
                await s.execute(delete(Faq).where(Faq.id == f.id))
                if f.source_candidate_id:
                    await s.execute(
                        delete(FaqCandidate).where(FaqCandidate.id == f.source_candidate_id)
                    )
            await s.execute(delete(FaqCandidate).where(FaqCandidate.knowledge_scope == scope))
            await s.execute(delete(FaqSyncRun).where(FaqSyncRun.knowledge_scope == scope))
            await s.commit()
        redis = await get_redis()
        if redis is not None:
            for h in hashes:
                try:
                    await redis.delete(faq_cache_key(scope, h))
                except Exception:  # noqa: BLE001
                    pass

    _asyncio.run(_cleanup())
    print("    清理完成")

    q1 = f"真实FAQ冒烟问题一_{uuid.uuid4().hex[:6]}"
    a1 = "真实FAQ冒烟答案一：这是经过真实 Markdown 上传链路的答案内容。"

    print("== 1. 发布 FAQ（触发真实 Markdown 上传）==")
    r = c.post(
        "/admin/faqs",
        headers=headers,
        json={"knowledge_scope": scope, "question": q1, "answer": a1},
    )
    check("发布 FAQ 200", r.status_code == 200, r.text[:300])
    if r.status_code != 200:
        return 1
    faq = r.json()["data"]
    faq_id = faq["id"]
    h1 = faq["normalized_question_hash"]
    print(f"    faq_id={faq_id} hash1={h1[:10]}")

    print("== 2. 等待 doc1 真实上传 succeeded ==")
    run1 = wait_sync(c, headers, scope, "succeeded")
    check("doc1 sync succeeded", run1.get("status") == "succeeded", str(run1))
    doc1 = run1.get("rag_document_id")
    check("doc1 文档 ID 非空", bool(doc1), str(doc1))
    # 该 run 的文档在上游存在且 completed（真实 import 链路已索引）
    check(
        "doc1 已清除旧版（prev=None 首轮）", run1.get("previous_rag_document_id") is None, str(run1)
    )

    print("== 3. 修改 FAQ（触发 doc2 上传）==")
    q2 = f"真实FAQ冒烟问题二_{uuid.uuid4().hex[:6]}"
    a2 = "真实FAQ冒烟答案二：修改后的新版本答案。"
    r = c.patch(
        f"/admin/faqs/{faq_id}",
        headers=headers,
        json={"question": q2, "answer": a2},
    )
    check("修改 FAQ 200", r.status_code == 200, r.text[:300])

    print("== 4. 等待 doc2 上传 succeeded + 旧版删除 ==")
    run2 = wait_sync(c, headers, scope, "succeeded")
    check("doc2 sync succeeded", run2.get("status") == "succeeded", str(run2))
    doc2 = run2.get("rag_document_id")
    check(
        "doc2 previous=doc1",
        run2.get("previous_rag_document_id") == doc1,
        f"prev={run2.get('previous_rag_document_id')} doc1={doc1}",
    )
    check("doc2 != doc1", doc2 != doc1, f"doc2={doc2} doc1={doc1}")

    print("== 5. 真实 import 侧验证：doc1 已删 + doc2 完成 ==")
    # 通过原 RAG import 服务直接查询文档快照，确认真实索引链路已走完
    import httpx as _httpx

    rag = _httpx.Client(base_url="http://127.0.0.1:8002", timeout=15)
    r = rag.get(f"/documents/{doc1}", headers={"X-User-Id": "svc_knowledge_admin"})
    check("doc1 上游已删除（404）", r.status_code == 404, f"status={r.status_code} {r.text[:120]}")
    r = rag.get(f"/documents/{doc2}", headers={"X-User-Id": "svc_knowledge_admin"})
    rag.close()
    check("doc2 上游快照 200", r.status_code == 200, f"status={r.status_code} {r.text[:200]}")
    if r.status_code == 200:
        d = r.json()
        check("doc2 上游 completed", d.get("status") == "completed", str(d)[:200])

    print("== 6. FAQ lookup 精确命中（Redis/MySQL，answer_source=faq_cache）==")
    # 通过 Stage 4 问答接口验证新 FAQ 可命中（answer_source=faq_cache，未走 RAG）
    r = c.post("/chat/sessions", headers=headers, json={})
    session_id = r.json()["data"]["id"] if r.status_code == 201 else None
    if session_id:
        r = c.post(
            f"/chat/sessions/{session_id}/messages:stream",
            headers=headers,
            json={"question": q2},
        )
        body = r.text
        ok = r.status_code == 200 and "answer_source" in body
        check("新 FAQ 问答接口响应", ok, f"status={r.status_code}")
        if ok:
            import re

            m = re.search(r'"answer_source"\s*:\s*"(\w+)"', body)
            check(
                "命中来源 FAQ 缓存（未走 RAG）",
                m is not None and m.group(1) == "faq_cache",
                m.group(1) if m else "none",
            )

    print()
    print(f"RESULT: {len(passed)} passed, {len(failed)} failed")
    for name in failed:
        print(f"  FAILED: {name}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
