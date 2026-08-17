"""schema contract alignment

Revision ID: f724d5b84454
Revises: f532646fe643
Create Date: 2026-08-16 14:58:50.762963

v2（修正）：
- 时间列 DATETIME → DATETIME(6) 时显式保留 nullable 与 server_default，
  避免 MySQL MODIFY 把 NOT NULL 列改可空、丢失默认值；
- 10 个外键使用稳定明确命名（fk_*），downgrade 可生成完整 SQL。
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import mysql

from alembic import op

revision: str = "f724d5b84454"
down_revision: str | None = "f532646fe643"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# 全部 DATETIME 列约束（与 app/models 一致）：(列名, nullable, server_default)
_DATETIME_COLS: dict[str, list[tuple[str, bool, str | None]]] = {
    "users": [
        ("password_changed_at", False, None),
        ("last_login_at", True, None),
        ("disabled_at", True, None),
        ("created_at", False, "CURRENT_TIMESTAMP(6)"),
        ("updated_at", False, "CURRENT_TIMESTAMP(6)"),
    ],
    "auth_sessions": [
        ("issued_at", False, None),
        ("expires_at", False, None),
        ("revoked_at", True, None),
        ("created_at", False, None),
    ],
    "managed_documents": [
        ("created_at", False, "CURRENT_TIMESTAMP(6)"),
        ("updated_at", False, "CURRENT_TIMESTAMP(6)"),
        ("deleted_at", True, None),
    ],
    "rag_integration_tasks": [
        ("started_at", True, None),
        ("finished_at", True, None),
        ("created_at", False, "CURRENT_TIMESTAMP(6)"),
        ("updated_at", False, "CURRENT_TIMESTAMP(6)"),
    ],
    "document_replacements": [
        ("created_at", False, None),
        ("completed_at", True, None),
    ],
    "chat_sessions": [
        ("last_message_at", True, None),
        ("created_at", False, "CURRENT_TIMESTAMP(6)"),
        ("updated_at", False, "CURRENT_TIMESTAMP(6)"),
        ("deleted_at", True, None),
    ],
    "chat_messages": [
        ("created_at", False, None),
        ("completed_at", True, None),
    ],
    "qa_access_logs": [("created_at", False, None)],
    "faq_candidates": [
        ("generated_at", False, None),
        ("reviewed_at", True, None),
    ],
    "faqs": [
        ("published_at", False, None),
        ("updated_at", False, None),
        ("unpublished_at", True, None),
    ],
    "faq_sync_runs": [
        ("created_at", False, None),
        ("finished_at", True, None),
    ],
    "knowledge_gap_candidates": [
        ("created_at", False, None),
        ("last_seen_at", False, None),
        ("reviewed_at", True, None),
    ],
    "audit_logs": [("created_at", False, None)],
}

# 明确命名外键：(约束名, 表, 引用表, 列, 引用列)
_FOREIGN_KEYS: list[tuple[str, str, str, list[str], list[str]]] = [
    ("fk_audit_logs_operator_user", "audit_logs", "users", ["operator_user_id"], ["id"]),
    ("fk_auth_sessions_user", "auth_sessions", "users", ["user_id"], ["id"]),
    ("fk_chat_messages_session", "chat_messages", "chat_sessions", ["session_id"], ["id"]),
    ("fk_chat_sessions_user", "chat_sessions", "users", ["user_id"], ["id"]),
    (
        "fk_document_replacements_old",
        "document_replacements",
        "managed_documents",
        ["old_managed_document_id"],
        ["id"],
    ),
    (
        "fk_document_replacements_new",
        "document_replacements",
        "managed_documents",
        ["new_managed_document_id"],
        ["id"],
    ),
    ("fk_faqs_source_candidate", "faqs", "faq_candidates", ["source_candidate_id"], ["id"]),
    ("fk_managed_documents_creator", "managed_documents", "users", ["created_by_user_id"], ["id"]),
    ("fk_qa_access_logs_session", "qa_access_logs", "chat_sessions", ["session_id"], ["id"]),
    (
        "fk_rag_integration_tasks_document",
        "rag_integration_tasks",
        "managed_documents",
        ["managed_document_id"],
        ["id"],
    ),
]


def upgrade() -> None:
    # 1) DATETIME → DATETIME(6)，显式保留 nullable / server_default
    for table, cols in _DATETIME_COLS.items():
        for col_name, nullable, default in cols:
            op.alter_column(
                table,
                col_name,
                existing_type=mysql.DATETIME(),
                type_=mysql.DATETIME(fsp=6),
                nullable=nullable,
                server_default=sa.text(default) if default else None,
            )

    # 2) 长文本：TEXT → LONGTEXT
    op.alter_column(
        "chat_messages",
        "content",
        existing_type=mysql.TEXT(collation="utf8mb4_unicode_ci"),
        type_=mysql.LONGTEXT(),
        existing_nullable=False,
    )
    op.alter_column(
        "faq_candidates",
        "suggested_answer",
        existing_type=mysql.TEXT(collation="utf8mb4_unicode_ci"),
        type_=mysql.LONGTEXT(),
        existing_nullable=True,
    )
    op.alter_column(
        "faqs",
        "answer",
        existing_type=mysql.TEXT(collation="utf8mb4_unicode_ci"),
        type_=mysql.LONGTEXT(),
        existing_nullable=False,
    )

    # 3) hit_count：INT → BIGINT UNSIGNED
    op.alter_column(
        "faqs",
        "hit_count",
        existing_type=mysql.INTEGER(unsigned=True),
        type_=mysql.BIGINT(unsigned=True),
        existing_nullable=False,
    )

    # 4) faq_candidates 唯一约束
    op.create_index(
        "ix_fc_scope_hash",
        "faq_candidates",
        ["knowledge_scope", "normalized_question_hash"],
        unique=True,
    )

    # 5) 明确命名外键
    for name, table, ref, cols, refcols in _FOREIGN_KEYS:
        op.create_foreign_key(name, table, ref, cols, refcols, ondelete="RESTRICT")


def downgrade() -> None:
    # 命名外键删除（名称明确，可生成 SQL）
    for name, table, _ref, _cols, _refcols in reversed(_FOREIGN_KEYS):
        op.drop_constraint(name, table, type_="foreignkey")

    op.drop_index("ix_fc_scope_hash", table_name="faq_candidates")

    op.alter_column(
        "faqs",
        "hit_count",
        existing_type=mysql.BIGINT(unsigned=True),
        type_=mysql.INTEGER(unsigned=True),
        existing_nullable=False,
    )
    op.alter_column(
        "faqs",
        "answer",
        existing_type=mysql.LONGTEXT(),
        type_=mysql.TEXT(collation="utf8mb4_unicode_ci"),
        existing_nullable=False,
    )
    op.alter_column(
        "faq_candidates",
        "suggested_answer",
        existing_type=mysql.LONGTEXT(),
        type_=mysql.TEXT(collation="utf8mb4_unicode_ci"),
        existing_nullable=True,
    )
    op.alter_column(
        "chat_messages",
        "content",
        existing_type=mysql.LONGTEXT(),
        type_=mysql.TEXT(collation="utf8mb4_unicode_ci"),
        existing_nullable=False,
    )

    # 时间列还原为 DATETIME，同时保留每列原有的 nullable 与 server_default
    # （恢复 f532 中的 22 个 NOT NULL 字段与 8 个 now() 默认值，不能清成可空）
    for table, cols in _DATETIME_COLS.items():
        for col_name, nullable, default in cols:
            op.alter_column(
                table,
                col_name,
                existing_type=mysql.DATETIME(fsp=6),
                type_=mysql.DATETIME(),
                nullable=nullable,
                server_default=sa.text("now()") if default else None,
            )
