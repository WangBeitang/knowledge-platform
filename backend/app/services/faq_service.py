"""FAQ 精确短路服务（Stage 4 最小实现）。

处理链（冻结 API §9 / 数据对象 §6.1）：
- 按角色允许 scope 的优先级逐档查询（admin_private > internal_shared > external_public）；
- 只允许 status=published；
- Redis 优先 → miss / Redis 不可用 → MySQL → 命中后回填 Redis；
- Redis 故障必须降级 MySQL，不能让内部问答整体失败；
- 第一版不做 negative cache、不做 embedding / fuzzy / LLM 相似判断。

Cache Key（冻结 §6.1）：`faq:v1:{knowledge_scope}:{normalized_question_hash}`。
Cache Value：{faq_id, knowledge_scope, question, answer, version, updated_at}。

`lookup` 本身保持读取语义；hit_count 由实际成功交付 FAQ 后显式调用
`record_hit`（数据库原子自增），避免 Stage 5 语义提前泄漏。
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from app.core.redis import get_redis
from app.models.faq import Faq
from app.repositories.faq_repository import FaqRepository

logger = logging.getLogger("app.services.faq_service")

FAQ_CACHE_PREFIX = "faq:v1"


@dataclass
class FaqHit:
    faq_id: str
    knowledge_scope: str
    question: str
    answer: str
    updated_at: str | None = None


def faq_cache_key(knowledge_scope: str, normalized_question_hash: str) -> str:
    """确定性缓存键：包含 knowledge_scope + normalized_question_hash。"""
    return f"{FAQ_CACHE_PREFIX}:{knowledge_scope}:{normalized_question_hash}"


class FaqService:
    def __init__(self, repository: FaqRepository) -> None:
        self.repository = repository

    async def lookup_exact_faq(
        self,
        *,
        scopes: list[str],
        normalized_question: str,
        normalized_question_hash: str,
    ) -> FaqHit | None:
        """按 scope 优先级精确查询 published FAQ；未命中返回 None。

        normalized_question 仅用于回填 Redis 缓存值；命中判定只依赖哈希。
        """
        for scope in scopes:
            hit = await self._lookup_in_scope(scope, normalized_question_hash)
            if hit is not None:
                return hit
        return None

    async def _lookup_in_scope(self, scope: str, normalized_question_hash: str) -> FaqHit | None:
        cached = await self._get_cache(scope, normalized_question_hash)
        if cached is not None:
            return cached
        # Redis miss / 不可用 → MySQL
        faq = await self.repository.find_published_by_scope_hash(
            knowledge_scope=scope,
            normalized_question_hash=normalized_question_hash,
        )
        if faq is None:
            return None
        hit = _to_hit(faq)
        await self._backfill_cache(scope, normalized_question_hash, hit)
        return hit

    async def _get_cache(self, scope: str, normalized_question_hash: str) -> FaqHit | None:
        try:
            redis = await get_redis()
            if redis is None:
                return None
            raw = await redis.get(faq_cache_key(scope, normalized_question_hash))
        except Exception:  # noqa: BLE001 Redis 故障必须降级 MySQL
            logger.warning("FAQ Redis 读取失败，降级 MySQL scope=%s", scope)
            return None
        if not raw:
            return None
        try:
            payload = json.loads(raw)
            faq_id = str(payload.get("faq_id") or "")
            answer = str(payload.get("answer") or "")
            if not faq_id or not answer:
                return None
            return FaqHit(
                faq_id=faq_id,
                knowledge_scope=scope,
                question=str(payload.get("question") or ""),
                answer=answer,
                updated_at=str(payload.get("updated_at") or ""),
            )
        except (ValueError, TypeError):
            logger.warning("FAQ 缓存值损坏，忽略 scope=%s", scope)
            return None

    async def _backfill_cache(self, scope: str, normalized_question_hash: str, hit: FaqHit) -> None:
        try:
            redis = await get_redis()
            if redis is None:
                return
            value: dict[str, Any] = {
                "faq_id": hit.faq_id,
                "knowledge_scope": hit.knowledge_scope,
                "question": hit.question,
                "answer": hit.answer,
                "version": hit.updated_at or "",
                "updated_at": hit.updated_at,
            }
            cache_value = json.dumps(value, ensure_ascii=False)
            await redis.set(faq_cache_key(scope, normalized_question_hash), cache_value)
        except Exception:  # noqa: BLE001 回填失败不影响已命中的 MySQL 结果
            logger.warning("FAQ Redis 回填失败 scope=%s", scope)

    async def record_hit(self, faq_id: str) -> None:
        """成功交付 FAQ 后显式记录命中（数据库原子自增）。"""
        await self.repository.increment_hit_count(faq_id)


def _to_hit(faq: Faq) -> FaqHit:
    updated_at: str | None = None
    if faq.updated_at is not None:
        if isinstance(faq.updated_at, datetime):
            updated_at = faq.updated_at.isoformat(timespec="seconds")
        else:
            updated_at = str(faq.updated_at)
    return FaqHit(
        faq_id=faq.id,
        knowledge_scope=faq.knowledge_scope,
        question=faq.question,
        answer=faq.answer,
        updated_at=updated_at,
    )
