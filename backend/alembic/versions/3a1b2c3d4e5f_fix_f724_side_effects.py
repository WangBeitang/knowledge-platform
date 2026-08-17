"""fix f724 side effects: restore NOT NULL / defaults, rename FKs explicit

Revision ID: 3a1b2c3d4e5f
Revises: f724d5b84454
Create Date: 2026-08-16 15:10:00.000000

背景：f724（早期版本）的 alter_column 未显式保留 nullable/server_default，
导致 MySQL MODIFY 将 22 个 NOT NULL 时间字段改可空、8 个 created_at/updated_at
丢失 now() 默认值；外键为 MySQL 自动命名（*_ibfk_N）。

本迁移（0003）修复当前已执行的数据库：
1. 恢复全部 DATETIME(6) 列的 nullable 与 server_default（MODIFY 幂等）；
2. 动态删除已存在的自动命名外键，重建为明确命名 fk_*（兼容任意环境）。

注意：upgrade 内含 information_schema 查询，仅在 online 模式（--sql 离线模式不可用）；
downgrade 全部使用明确命名，离线回滚可完整生成 SQL。
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import mysql

from alembic import op

revision: str = "3a1b2c3d4e5f"
down_revision: str | None = "f724d5b84454"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# 与 f724 一致的约束映射（与 app/models 相同事实）
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

# 明确命名外键（与 f724 一致）
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


def _alter_datetime_column(
    table: str,
    col_name: str,
    nullable: bool,
    default: str | None,
) -> None:
    op.alter_column(
        table,
        col_name,
        type_=mysql.DATETIME(fsp=6),
        nullable=nullable,
        server_default=sa.text(default) if default else None,
    )


def upgrade() -> None:
    # 1) 恢复时间列 nullable / server_default（对已正确列幂等）
    for table, cols in _DATETIME_COLS.items():
        for col_name, nullable, default in cols:
            _alter_datetime_column(table, col_name, nullable, default)

    # 2) 动态删除该表上引用目标表的外键（兼容 MySQL 自动命名 *_ibfk_N）
    conn = op.get_bind()
    for _name, table, ref, _cols, _refcols in _FOREIGN_KEYS:
        rows = conn.execute(
            sa.text(
                "SELECT CONSTRAINT_NAME FROM information_schema.KEY_COLUMN_USAGE "
                "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = :t AND REFERENCED_TABLE_NAME = :r"
            ),
            {"t": table, "r": ref},
        ).fetchall()
        for (cname,) in rows:
            op.drop_constraint(cname, table, type_="foreignkey")

    # 3) 重建为明确命名外键
    for name, table, ref, cols, refcols in _FOREIGN_KEYS:
        op.create_foreign_key(name, table, ref, cols, refcols, ondelete="RESTRICT")


def downgrade() -> None:
    # 无操作：修正后的 f724 本身就是目标结构（时间列保留 nullable/server_default、
    # 外键为 fk_* 明确命名）。0003 只用于修复"旧版 f724 执行后的坏库"；
    # 回退到 f724 时数据库已与该版本结构一致，无需也不应再删外键或破坏时间列约束。
    pass
