"""FAQ 服务（Stage 4 精确短路最小实现 + Stage 5 管理闭环）。

处理链（冻结 API §9 / 数据对象 §6.1 / §4.9-4.10）：
- 查询侧：按角色允许 scope 优先级逐档（admin_private > internal_shared > external_public），
  只允许 published；Redis 优先 → miss/不可用 → MySQL → 命中后回填 Redis；
  Redis 故障必须降级 MySQL，不能让内部问答整体失败；不做 embedding / fuzzy / LLM。
- 管理侧（仅管理员）：候选列表/拒绝/审核发布；正式 FAQ 列表/创建/更新/下线/重发；
  发布/修改后写 Redis 精确缓存，下线后删缓存；缓存键必须含 knowledge_scope；
  事实源为 MySQL，Redis 只是可重建缓存。

Cache Key（冻结 §6.1）：`faq:v1:{knowledge_scope}:{normalized_question_hash}`。
Cache Value：{faq_id, knowledge_scope, question, answer, version, updated_at}。

RAG 文档同步由 FaqSyncService 承担（本服务只触发 submit，不做上传细节）。
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from app.core.enums import (
    AuditAction,
    FaqCandidateStatus,
    FaqStatus,
    KnowledgeScope,
    RagSyncStatus,
)
from app.core.errors import bad_request, conflict, not_found
from app.core.normalizer import normalize_question, question_hash
from app.core.redis import get_redis
from app.core.time import utc_now_naive
from app.models.faq import Faq
from app.models.faq_candidate import FaqCandidate
from app.models.user import User
from app.repositories.faq_candidate_repository import FaqCandidateRepository
from app.repositories.faq_repository import FaqRepository
from app.schemas.faq import FaqCandidateView, FaqView
from app.services.audit_service import AuditService

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


def candidate_view(candidate: FaqCandidate) -> FaqCandidateView:
    return FaqCandidateView(
        id=candidate.id,
        knowledge_scope=candidate.knowledge_scope,
        normalized_question=candidate.normalized_question,
        normalized_question_hash=candidate.normalized_question_hash,
        sample_questions=candidate.sample_questions_json or [],
        ask_count=candidate.ask_count,
        suggested_answer=candidate.suggested_answer,
        status=candidate.status,
        published_faq_id=candidate.published_faq_id,
        generated_at=candidate.generated_at,
        reviewed_by_user_id=candidate.reviewed_by_user_id,
        reviewed_at=candidate.reviewed_at,
    )


def faq_view(faq: Faq) -> FaqView:
    return FaqView(
        id=faq.id,
        knowledge_scope=faq.knowledge_scope,
        question=faq.question,
        normalized_question=faq.normalized_question,
        normalized_question_hash=faq.normalized_question_hash,
        answer=faq.answer,
        status=faq.status,
        source_candidate_id=faq.source_candidate_id,
        hit_count=faq.hit_count,
        rag_sync_status=faq.rag_sync_status,
        rag_sync_error=faq.rag_sync_error,
        created_by_user_id=faq.created_by_user_id,
        reviewed_by_user_id=faq.reviewed_by_user_id,
        published_at=faq.published_at,
        updated_at=faq.updated_at,
        unpublished_at=faq.unpublished_at,
    )


class FaqService:
    def __init__(
        self,
        repository: FaqRepository,
        candidates: FaqCandidateRepository | None = None,
        audit: AuditService | None = None,
        sync_service: Any | None = None,
    ) -> None:
        self.repository = repository
        self.candidates = candidates
        self.audit = audit
        self.sync_service = sync_service

    # ================= 查询侧（Stage 4，冻结不动） =================

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
        await self._write_cache(
            scope=scope,
            normalized_question_hash=normalized_question_hash,
            faq_id=hit.faq_id,
            question=hit.question,
            answer=hit.answer,
            updated_at=hit.updated_at,
        )

    async def record_hit(self, faq_id: str) -> None:
        """成功交付 FAQ 后显式记录命中（数据库原子自增）。"""
        await self.repository.increment_hit_count(faq_id)

    # ================= 缓存写删（Stage 5，key 含 scope） =================

    async def set_faq_cache(self, faq: Faq) -> None:
        """发布/修改/重发后写精确缓存（MySQL 为事实源，缓存可随时重建）。"""
        updated_at: str | None = None
        if isinstance(faq.updated_at, datetime):
            updated_at = faq.updated_at.isoformat(timespec="seconds")
        await self._write_cache(
            scope=faq.knowledge_scope,
            normalized_question_hash=faq.normalized_question_hash,
            faq_id=faq.id,
            question=faq.question,
            answer=faq.answer,
            updated_at=updated_at,
        )

    async def delete_faq_cache(self, knowledge_scope: str, normalized_question_hash: str) -> None:
        """下线后删除精确缓存（Redis 不可用不影响正式状态）。"""
        try:
            redis = await get_redis()
            if redis is None:
                return
            await redis.delete(faq_cache_key(knowledge_scope, normalized_question_hash))
        except Exception:  # noqa: BLE001
            logger.warning("FAQ Redis 缓存删除失败 scope=%s", knowledge_scope)

    async def _write_cache(
        self,
        *,
        scope: str,
        normalized_question_hash: str,
        faq_id: str,
        question: str,
        answer: str,
        updated_at: str | None,
    ) -> None:
        try:
            redis = await get_redis()
            if redis is None:
                return
            value: dict[str, Any] = {
                "faq_id": faq_id,
                "knowledge_scope": scope,
                "question": question,
                "answer": answer,
                "version": updated_at or "",
                "updated_at": updated_at,
            }
            await redis.set(
                faq_cache_key(scope, normalized_question_hash),
                json.dumps(value, ensure_ascii=False),
            )
        except Exception:  # noqa: BLE001 写缓存失败不影响 MySQL 正式状态
            logger.warning("FAQ Redis 缓存写入失败 scope=%s", scope)

    # ================= 候选管理（仅管理员） =================

    async def list_candidates(
        self,
        *,
        page: int,
        page_size: int,
        knowledge_scope: str | None,
        status: str | None,
        sort_by: str,
        sort_order: str,
    ) -> tuple[list[FaqCandidateView], int]:
        assert self.candidates is not None
        rows, total = await self.candidates.list_page(
            page=page,
            page_size=page_size,
            knowledge_scope=knowledge_scope,
            status=status,
            sort_by=sort_by,
            sort_order=sort_order,
        )
        return [candidate_view(row) for row in rows], total

    async def reject_candidate(
        self, *, candidate_id: str, operator: User, client_ip: str | None
    ) -> FaqCandidateView:
        assert self.candidates is not None and self.audit is not None
        candidate = await self.candidates.get_by_id(candidate_id)
        if candidate is None:
            raise not_found("候选不存在")
        if candidate.status != FaqCandidateStatus.pending_review.value:
            raise conflict("候选已处理，不能重复审核")
        now = utc_now_naive()
        await self.candidates.mark_rejected(
            candidate, reviewed_by_user_id=operator.id, reviewed_at=now
        )
        await self.audit.record(
            operator_user_id=operator.id,
            action=AuditAction.faq_candidate_rejected.value,
            resource_type="faq_candidate",
            resource_id=candidate.id,
            result="succeeded",
            after={
                "knowledge_scope": candidate.knowledge_scope,
                "normalized_question_hash": candidate.normalized_question_hash,
            },
            client_ip=client_ip,
        )
        return candidate_view(candidate)

    async def publish_candidate(
        self,
        *,
        candidate_id: str,
        knowledge_scope: str,
        question: str,
        answer: str,
        operator: User,
        client_ip: str | None,
    ) -> FaqView:
        """审核并发布：创建正式 FAQ → 审计 → 写缓存 → 触发范围同步。

        写事务顺序（冻结 API §12）：MySQL 业务状态（faq + candidate + audit）
        先真正 commit，commit 成功后才执行 Redis 更新与 submit_faq_sync；
        MySQL commit 失败时不写 Redis、不调 RAG；Redis/RAG 失败不回滚已提交 FAQ。
        """
        assert self.candidates is not None and self.audit is not None
        candidate = await self.candidates.get_by_id(candidate_id)
        if candidate is None:
            raise not_found("候选不存在")
        if candidate.status != FaqCandidateStatus.pending_review.value:
            raise conflict("候选已处理，不能重复审核")
        faq = await self._create_faq_inner(
            knowledge_scope=knowledge_scope,
            question=question,
            answer=answer,
            operator=operator,
            source_candidate_id=candidate.id,
        )
        await self.candidates.mark_published(
            candidate,
            published_faq_id=faq.id,
            reviewed_by_user_id=operator.id,
            reviewed_at=faq.published_at,
        )
        await self.audit.record(
            operator_user_id=operator.id,
            action=AuditAction.faq_candidate_published.value,
            resource_type="faq_candidate",
            resource_id=candidate.id,
            result="succeeded",
            after={"faq_id": faq.id, "knowledge_scope": faq.knowledge_scope},
            client_ip=client_ip,
        )
        # 冻结 §12：MySQL 业务事务先真正 commit（commit 失败 → 不写 Redis/不调 RAG）
        await self.repository.session.commit()
        # Redis 外部副作用在 MySQL 事务提交之后
        await self.set_faq_cache(faq)
        await self._trigger_sync(faq.knowledge_scope, operator.id)
        return faq_view(faq)

    # ================= 正式 FAQ 管理（仅管理员） =================

    async def list_faqs(
        self,
        *,
        page: int,
        page_size: int,
        knowledge_scope: str | None,
        status: str | None,
        sort_by: str,
        sort_order: str,
    ) -> tuple[list[FaqView], int]:
        rows, total = await self.repository.list_page(
            page=page,
            page_size=page_size,
            knowledge_scope=knowledge_scope,
            status=status,
            sort_by=sort_by,
            sort_order=sort_order,
        )
        return [faq_view(row) for row in rows], total

    async def create_faq(
        self,
        *,
        knowledge_scope: str,
        question: str,
        answer: str,
        operator: User,
        client_ip: str | None,
    ) -> FaqView:
        """直接创建并发布（POST /admin/faqs）。

        写事务顺序（冻结）：MySQL（faq + audit）先真正 commit 成功 →
        Redis → submit_faq_sync；commit 失败不写 Redis/不调 RAG。
        """
        assert self.audit is not None
        faq = await self._create_faq_inner(
            knowledge_scope=knowledge_scope,
            question=question,
            answer=answer,
            operator=operator,
            source_candidate_id=None,
        )
        await self.audit.record(
            operator_user_id=operator.id,
            action=AuditAction.faq_created.value,
            resource_type="faq",
            resource_id=faq.id,
            result="succeeded",
            after={
                "knowledge_scope": faq.knowledge_scope,
                "normalized_question_hash": faq.normalized_question_hash,
                "status": faq.status,
            },
            client_ip=client_ip,
        )
        # 冻结 §12：MySQL 业务事务先真正 commit（commit 失败 → 不写 Redis/不调 RAG）
        await self.repository.session.commit()
        await self.set_faq_cache(faq)
        await self._trigger_sync(faq.knowledge_scope, operator.id)
        return faq_view(faq)

    async def update_faq(
        self,
        *,
        faq_id: str,
        question: str,
        answer: str,
        operator: User,
        client_ip: str | None,
    ) -> FaqView:
        """更新完整可变字段：问题重算归一化与哈希，答案更新。

        写事务顺序（冻结）：MySQL（faq + audit）先真正 commit 成功 →
        Redis → 触发同步；commit 失败不写 Redis/不调 RAG。
        缓存规则（首轮复核）：
        - published：写新 hash cache；问题修改后删除旧 hash cache；
        - unpublished：禁止写 cache，且旧 hash 与新 hash 的 cache 都不存在；
        - MySQL status 保持不变。
        """
        assert self.audit is not None
        faq = await self.repository.get_by_id(faq_id)
        if faq is None:
            raise not_found("FAQ 不存在")
        normalized = normalize_question(question)
        if not normalized:
            raise bad_request("归一化后问题为空", code="EMPTY_QUESTION")
        if not answer or not answer.strip():
            raise bad_request("答案不能为空")
        new_hash = question_hash(normalized)
        # 同 scope 同 hash 冲突检查（排除自己）
        existing = await self.repository.find_by_scope_hash(
            knowledge_scope=faq.knowledge_scope,
            normalized_question_hash=new_hash,
        )
        if existing is not None and existing.id != faq.id:
            raise conflict("该知识范围内已存在相同归一化问题的 FAQ")
        old_hash = faq.normalized_question_hash
        now = utc_now_naive()
        await self.repository.update_faq_fields(
            faq,
            question=question.strip(),
            normalized_question=normalized,
            normalized_question_hash=new_hash,
            answer=answer.strip(),
            updated_at=now,
        )
        await self.audit.record(
            operator_user_id=operator.id,
            action=AuditAction.faq_updated.value,
            resource_type="faq",
            resource_id=faq.id,
            result="succeeded",
            after={
                "knowledge_scope": faq.knowledge_scope,
                "normalized_question_hash": new_hash,
            },
            client_ip=client_ip,
        )
        # 冻结 §12：MySQL 业务事务先真正 commit（commit 失败 → 不写 Redis/不调 RAG）
        await self.repository.session.commit()
        # Redis 外部副作用在 MySQL 事务提交之后
        if faq.status == FaqStatus.published.value:
            await self.set_faq_cache(faq)
            if old_hash != new_hash:
                await self.delete_faq_cache(faq.knowledge_scope, old_hash)
        else:
            # unpublished：禁止写 cache，新旧 hash 的 cache 都不存在
            await self.delete_faq_cache(faq.knowledge_scope, old_hash)
            if old_hash != new_hash:
                await self.delete_faq_cache(faq.knowledge_scope, new_hash)
        await self._trigger_sync(faq.knowledge_scope, operator.id)
        return faq_view(faq)

    async def unpublish_faq(self, *, faq_id: str, operator: User, client_ip: str | None) -> FaqView:
        """下线（不物理删除）：MySQL 状态 + 审计先 commit，再删缓存、触发范围重建。

        写事务顺序（冻结）：MySQL 先 commit 成功 → Redis 删除 → submit_faq_sync。
        """
        assert self.audit is not None
        faq = await self.repository.get_by_id(faq_id)
        if faq is None:
            raise not_found("FAQ 不存在")
        if faq.status != FaqStatus.published.value:
            raise conflict("FAQ 当前状态不允许下线")
        now = utc_now_naive()
        await self.repository.set_status(
            faq,
            status=FaqStatus.unpublished.value,
            updated_at=now,
            unpublished_at=now,
        )
        await self.audit.record(
            operator_user_id=operator.id,
            action=AuditAction.faq_unpublished.value,
            resource_type="faq",
            resource_id=faq.id,
            result="succeeded",
            after={"knowledge_scope": faq.knowledge_scope},
            client_ip=client_ip,
        )
        # 冻结 §12：MySQL 业务事务先真正 commit（commit 失败 → 不写 Redis/不调 RAG）
        await self.repository.session.commit()
        await self.delete_faq_cache(faq.knowledge_scope, faq.normalized_question_hash)
        await self._trigger_sync(faq.knowledge_scope, operator.id)
        return faq_view(faq)

    async def republish_faq(self, *, faq_id: str, operator: User, client_ip: str | None) -> FaqView:
        """重新发布：MySQL 状态 + 审计先 commit，再写缓存、触发范围重建。"""
        assert self.audit is not None
        faq = await self.repository.get_by_id(faq_id)
        if faq is None:
            raise not_found("FAQ 不存在")
        if faq.status != FaqStatus.unpublished.value:
            raise conflict("FAQ 当前状态不允许重新发布")
        now = utc_now_naive()
        await self.repository.set_status(
            faq,
            status=FaqStatus.published.value,
            updated_at=now,
            unpublished_at=None,
        )
        await self.audit.record(
            operator_user_id=operator.id,
            action=AuditAction.faq_republished.value,
            resource_type="faq",
            resource_id=faq.id,
            result="succeeded",
            after={"knowledge_scope": faq.knowledge_scope},
            client_ip=client_ip,
        )
        # 冻结 §12：MySQL 业务事务先真正 commit（commit 失败 → 不写 Redis/不调 RAG）
        await self.repository.session.commit()
        await self.set_faq_cache(faq)
        await self._trigger_sync(faq.knowledge_scope, operator.id)
        return faq_view(faq)

    # ================= 内部 =================

    async def _create_faq_inner(
        self,
        *,
        knowledge_scope: str,
        question: str,
        answer: str,
        operator: User,
        source_candidate_id: str | None,
    ) -> Faq:
        scope = _validate_scope(knowledge_scope)
        normalized = normalize_question(question)
        if not normalized:
            raise bad_request("归一化后问题为空", code="EMPTY_QUESTION")
        if not answer or not answer.strip():
            raise bad_request("答案不能为空")
        new_hash = question_hash(normalized)
        existing = await self.repository.find_by_scope_hash(
            knowledge_scope=scope,
            normalized_question_hash=new_hash,
        )
        if existing is not None:
            raise conflict("该知识范围内已存在相同归一化问题的 FAQ")
        now = utc_now_naive()
        faq = await self.repository.create_faq(
            id_value=None,  # 由 ORM default 生成 UUID
            knowledge_scope=scope,
            question=question.strip(),
            normalized_question=normalized,
            normalized_question_hash=new_hash,
            answer=answer.strip(),
            status=FaqStatus.published.value,
            source_candidate_id=source_candidate_id,
            hit_count=0,
            rag_sync_status=RagSyncStatus.pending.value,
            rag_sync_error=None,
            created_by_user_id=operator.id,
            reviewed_by_user_id=operator.id,
            published_at=now,
            updated_at=now,
            unpublished_at=None,
        )
        # 注意：不在此写 Redis 缓存——由调用方在审计（MySQL）之后统一触发
        return faq

    async def _trigger_sync(self, knowledge_scope: str, operator_user_id: str) -> None:
        """触发对应范围 FAQ 文档同步（请求内直接提交；失败不回滚已审核 FAQ）。

        同步失败仅影响 faq_sync_runs / faqs.rag_sync_status，不抛错打断主流程。
        """
        if self.sync_service is None:
            return
        try:
            await self.sync_service.submit_faq_sync(
                knowledge_scope=knowledge_scope,
                operator_user_id=operator_user_id,
            )
        except Exception:  # noqa: BLE001
            logger.exception("FAQ 文档同步触发失败 scope=%s", knowledge_scope)


def _validate_scope(scope: str) -> str:
    try:
        return KnowledgeScope(scope).value
    except ValueError:
        raise bad_request("非法知识范围") from None


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
