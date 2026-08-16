"""模型汇总：导入全部表模型（供 Alembic autogenerate 收集）。"""

from app.models.audit_log import AuditLog
from app.models.auth_session import AuthSession
from app.models.base import Base
from app.models.chat_message import ChatMessage
from app.models.chat_session import ChatSession
from app.models.document_replacement import DocumentReplacement
from app.models.faq import Faq
from app.models.faq_candidate import FaqCandidate
from app.models.faq_sync_run import FaqSyncRun
from app.models.knowledge_gap_candidate import KnowledgeGapCandidate
from app.models.managed_document import ManagedDocument
from app.models.qa_access_log import QaAccessLog
from app.models.rag_integration_task import RagIntegrationTask
from app.models.user import User

__all__ = [
    "AuditLog",
    "AuthSession",
    "Base",
    "ChatMessage",
    "ChatSession",
    "DocumentReplacement",
    "Faq",
    "FaqCandidate",
    "FaqSyncRun",
    "KnowledgeGapCandidate",
    "ManagedDocument",
    "QaAccessLog",
    "RagIntegrationTask",
    "User",
]
