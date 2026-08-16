"""集中枚举定义（《数据对象设计》§2）。ORM 列存字符串值，不用 MySQL ENUM。"""

from enum import StrEnum


class UserRole(StrEnum):
    admin = "admin"
    employee = "employee"


class UserStatus(StrEnum):
    active = "active"
    disabled = "disabled"


class KnowledgeScope(StrEnum):
    external_public = "external_public"
    internal_shared = "internal_shared"
    admin_private = "admin_private"


class AccessChannel(StrEnum):
    internal_web = "internal_web"
    external_api = "external_api"


class AnswerSource(StrEnum):
    faq_cache = "faq_cache"
    rag = "rag"
    none = "none"


class IntegrationOperation(StrEnum):
    dataset_bootstrap = "dataset_bootstrap"
    document_import = "document_import"
    document_rebuild = "document_rebuild"
    document_replace = "document_replace"
    faq_sync = "faq_sync"


class IntegrationTaskStatus(StrEnum):
    pending = "pending"
    running = "running"
    succeeded = "succeeded"
    failed = "failed"
    cancelled = "cancelled"


class ManagedDocumentStatus(StrEnum):
    importing = "importing"
    active = "active"
    import_failed = "import_failed"
    replaced = "replaced"
    deleted = "deleted"


class FaqCandidateStatus(StrEnum):
    pending_review = "pending_review"
    published = "published"
    rejected = "rejected"


class FaqStatus(StrEnum):
    published = "published"
    unpublished = "unpublished"


class RagSyncStatus(StrEnum):
    pending = "pending"
    syncing = "syncing"
    succeeded = "succeeded"
    failed = "failed"


class KnowledgeGapStatus(StrEnum):
    pending_review = "pending_review"
    ignored = "ignored"
    resolved = "resolved"


class MessageRole(StrEnum):
    user = "user"
    assistant = "assistant"


class MessageStatus(StrEnum):
    pending = "pending"
    streaming = "streaming"
    completed = "completed"
    failed = "failed"


class SessionStatus(StrEnum):
    active = "active"
    archived = "archived"
    deleted = "deleted"


class ReplacementStatus(StrEnum):
    pending = "pending"
    completed = "completed"
    failed = "failed"


class LogStatus(StrEnum):
    succeeded = "succeeded"
    failed = "failed"


class SourceKind(StrEnum):
    manual_upload = "manual_upload"
    faq_generated = "faq_generated"
