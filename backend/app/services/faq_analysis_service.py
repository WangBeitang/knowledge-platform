"""FAQ 候选分析服务：管理员手动 analyze 日志 → faq_candidates（冻结 §2.3 / §4.9）。

规则（冻结）：
- 仅管理员手动触发，非定时、非问答链路自动生成；
- 来源只允许 qa_access_logs；
- 按归一化精确值（scope + hash）聚合，第一版不声称语义聚类；
- 已发布标准问题（faqs 表存在 published 同 hash）不重复创建候选；
- 重复 analyze 遵循去重语义：同 scope+同 hash 覆盖式更新统计，不重复建行；
- 不修改 Stage 4 问答链路（本服务只读 qa_access_logs）。
"""

from app.core.enums import FaqStatus
from app.core.time import utc_now_naive
from app.repositories.faq_candidate_repository import FaqCandidateRepository
from app.repositories.faq_repository import FaqRepository
from app.repositories.qa_access_log_repository import QaAccessLogRepository


class FaqAnalysisService:
    def __init__(
        self,
        *,
        logs: QaAccessLogRepository,
        candidates: FaqCandidateRepository,
        faqs: FaqRepository,
    ) -> None:
        self.logs = logs
        self.candidates = candidates
        self.faqs = faqs

    async def analyze_logs(self) -> dict:
        """全量聚合访问日志并覆盖式 upsert 候选。

        返回统计：created / updated / skipped_published（已发布 FAQ 跳过数）。
        """
        groups = await self.logs.aggregate_by_hash()
        created = 0
        updated = 0
        skipped_published = 0
        now = utc_now_naive()
        for group in groups:
            published = await self.faqs.find_by_scope_hash(
                knowledge_scope=group["knowledge_scope"],
                normalized_question_hash=group["normalized_question_hash"],
            )
            if published is not None and published.status == FaqStatus.published.value:
                # 已发布标准问题：不重复创建候选（hit_count 语义由 lookup 驱动，此处不触碰）
                skipped_published += 1
                continue
            existed = await self.candidates.get_by_scope_hash(
                knowledge_scope=group["knowledge_scope"],
                normalized_question_hash=group["normalized_question_hash"],
            )
            await self.candidates.upsert_aggregate(
                knowledge_scope=group["knowledge_scope"],
                normalized_question=group["normalized_question"],
                normalized_question_hash=group["normalized_question_hash"],
                ask_count=group["ask_count"],
                sample_questions=group["sample_questions"],
                source_log_ids=group["source_log_ids"],
                generated_at=now,
            )
            if existed is None:
                created += 1
            else:
                updated += 1
        return {
            "created": created,
            "updated": updated,
            "skipped_published": skipped_published,
        }
