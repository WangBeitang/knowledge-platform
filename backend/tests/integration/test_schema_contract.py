"""数据库 Schema 契约测试：防止结构偏离冻结设计（《数据对象设计》）。

直接查询 information_schema 断言关键契约（autogenerate 对 MySQL fsp 有盲区，
因此以真实库结构为准）。数据库不可达时跳过（本地 CI 无 DB 场景）。
"""

import re
import subprocess
import sys
from pathlib import Path

import pymysql
import pytest

from app.core.config import get_settings

pytestmark = pytest.mark.integration

# 冻结契约：10 个 RESTRICT 外键（users/auth_sessions/chat_sessions/chat_messages/
# qa_access_logs/managed_documents/rag_integration_tasks/document_replacements×2/faqs/audit_logs）
EXPECTED_FK_COUNT = 10

# 冻结契约：DATETIME(6) 列 22 个 NOT NULL / 14 个 NULL
EXPECTED_DATETIME_NOT_NULL = 22
EXPECTED_DATETIME_NULLABLE = 14

# 冻结契约：应带 CURRENT_TIMESTAMP(6) 默认值的 (表, 列)
EXPECTED_DATETIME_DEFAULTS = {
    ("users", "created_at"),
    ("users", "updated_at"),
    ("chat_sessions", "created_at"),
    ("chat_sessions", "updated_at"),
    ("managed_documents", "created_at"),
    ("managed_documents", "updated_at"),
    ("rag_integration_tasks", "created_at"),
    ("rag_integration_tasks", "updated_at"),
}


def _connect():
    s = get_settings()
    return pymysql.connect(
        host=s.db_host,
        port=s.db_port,
        user=s.db_user,
        password=s.db_password,
        database=s.db_name,
        connect_timeout=5,
        charset="utf8mb4",
    )


def _cursor():
    try:
        conn = _connect()
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"数据库不可达，跳过 Schema 契约测试: {exc}")
    return conn, conn.cursor()


class TestSchemaContract:
    def test_all_datetime_columns_have_fsp6(self):
        conn, cur = _cursor()
        try:
            cur.execute(
                "SELECT TABLE_NAME, COLUMN_NAME, DATETIME_PRECISION "
                "FROM information_schema.COLUMNS "
                "WHERE TABLE_SCHEMA=%s AND DATA_TYPE='datetime'",
                (conn.db.decode() if isinstance(conn.db, bytes) else conn.db,),
            )
            rows = cur.fetchall()
            assert rows, "应存在 DATETIME 列"
            bad = [(r[0], r[1], r[2]) for r in rows if r[2] != 6]
            assert not bad, f"存在非 DATETIME(6) 的列: {bad}"
        finally:
            cur.close()
            conn.close()

    def test_longtext_fields(self):
        conn, cur = _cursor()
        try:
            cases = [
                ("chat_messages", "content"),
                ("faq_candidates", "suggested_answer"),
                ("faqs", "answer"),
            ]
            for table, column in cases:
                cur.execute(
                    "SELECT COLUMN_TYPE FROM information_schema.COLUMNS "
                    "WHERE TABLE_SCHEMA=%s AND TABLE_NAME=%s AND COLUMN_NAME=%s",
                    (conn.db.decode() if isinstance(conn.db, bytes) else conn.db, table, column),
                )
                row = cur.fetchone()
                assert row and row[0] == "longtext", f"{table}.{column} 应为 LONGTEXT，实际 {row}"
        finally:
            cur.close()
            conn.close()

    def test_faqs_hit_count_bigint_unsigned(self):
        conn, cur = _cursor()
        try:
            cur.execute(
                "SELECT COLUMN_TYPE FROM information_schema.COLUMNS "
                "WHERE TABLE_SCHEMA=%s AND TABLE_NAME='faqs' AND COLUMN_NAME='hit_count'",
                (conn.db.decode() if isinstance(conn.db, bytes) else conn.db,),
            )
            row = cur.fetchone()
            assert row and row[0] == "bigint unsigned", (
                f"faqs.hit_count 应为 BIGINT UNSIGNED，实际 {row}"
            )
        finally:
            cur.close()
            conn.close()

    def test_faq_candidates_unique_scope_hash(self):
        conn, cur = _cursor()
        try:
            cur.execute(
                "SELECT CONSTRAINT_NAME FROM information_schema.TABLE_CONSTRAINTS "
                "WHERE TABLE_SCHEMA=%s AND TABLE_NAME='faq_candidates' "
                "AND CONSTRAINT_TYPE='UNIQUE'",
                (conn.db.decode() if isinstance(conn.db, bytes) else conn.db,),
            )
            uniques = [r[0] for r in cur.fetchall()]
            assert any("scope_hash" in name or "unique" in name.lower() for name in uniques), (
                "faq_candidates 缺少 UNIQUE(knowledge_scope, normalized_question_hash)，"
                f"实际 {uniques}"
            )
        finally:
            cur.close()
            conn.close()

    def test_foreign_keys_count_and_restrict(self):
        conn, cur = _cursor()
        try:
            schema = conn.db.decode() if isinstance(conn.db, bytes) else conn.db
            cur.execute(
                "SELECT COUNT(*) FROM information_schema.TABLE_CONSTRAINTS "
                "WHERE TABLE_SCHEMA=%s AND CONSTRAINT_TYPE='FOREIGN KEY'",
                (schema,),
            )
            assert cur.fetchone()[0] == EXPECTED_FK_COUNT, "外键数量与冻结契约不一致"
            cur.execute(
                "SELECT TABLE_NAME, DELETE_RULE FROM information_schema.REFERENTIAL_CONSTRAINTS "
                "WHERE CONSTRAINT_SCHEMA=%s",
                (schema,),
            )
            rules = {r[0]: r[1] for r in cur.fetchall()}
            bad = {t: r for t, r in rules.items() if r != "RESTRICT"}
            assert not bad, f"存在非 RESTRICT 外键删除规则: {bad}"
        finally:
            cur.close()
            conn.close()

    def test_datetime_nullability_contract(self):
        """22 个时间字段必须 NOT NULL，14 个必须可空（冻结契约）。"""
        conn, cur = _cursor()
        try:
            schema = conn.db.decode() if isinstance(conn.db, bytes) else conn.db
            cur.execute(
                "SELECT IS_NULLABLE, COUNT(*) FROM information_schema.COLUMNS "
                "WHERE TABLE_SCHEMA=%s AND DATA_TYPE='datetime' GROUP BY IS_NULLABLE",
                (schema,),
            )
            counts = dict(cur.fetchall())
            assert counts.get("NO", 0) == EXPECTED_DATETIME_NOT_NULL, (
                f"NOT NULL 时间列数量不符，实际 {counts}"
            )
            assert counts.get("YES", 0) == EXPECTED_DATETIME_NULLABLE, (
                f"可空时间列数量不符，实际 {counts}"
            )
        finally:
            cur.close()
            conn.close()

    def test_datetime_server_defaults(self):
        """created_at/updated_at 必须保留 CURRENT_TIMESTAMP(6) 默认值。"""
        conn, cur = _cursor()
        try:
            schema = conn.db.decode() if isinstance(conn.db, bytes) else conn.db
            cur.execute(
                "SELECT TABLE_NAME, COLUMN_NAME, COLUMN_DEFAULT "
                "FROM information_schema.COLUMNS "
                "WHERE TABLE_SCHEMA=%s AND DATA_TYPE='datetime' AND COLUMN_DEFAULT IS NOT NULL",
                (schema,),
            )
            rows = {(r[0], r[1]): r[2] for r in cur.fetchall()}
            missing = EXPECTED_DATETIME_DEFAULTS - set(rows.keys())
            assert not missing, f"以下列缺少默认值: {missing}"
            bad = {
                (t, c): v
                for (t, c), v in rows.items()
                if (t, c) in EXPECTED_DATETIME_DEFAULTS and "CURRENT_TIMESTAMP" not in (v or "")
            }
            assert not bad, f"默认值不是 CURRENT_TIMESTAMP: {bad}"
        finally:
            cur.close()
            conn.close()

    def test_foreign_keys_explicit_names(self):
        """外键必须全部使用 fk_* 明确命名（禁止 MySQL 自动匿名命名）。"""
        conn, cur = _cursor()
        try:
            schema = conn.db.decode() if isinstance(conn.db, bytes) else conn.db
            cur.execute(
                "SELECT CONSTRAINT_NAME FROM information_schema.TABLE_CONSTRAINTS "
                "WHERE TABLE_SCHEMA=%s AND CONSTRAINT_TYPE='FOREIGN KEY'",
                (schema,),
            )
            names = [r[0] for r in cur.fetchall()]
            anonymous = [n for n in names if not n.startswith("fk_")]
            assert not anonymous, f"存在匿名外键: {anonymous}"
            assert len(names) == EXPECTED_FK_COUNT, f"外键数量不符: {len(names)}"
        finally:
            cur.close()
            conn.close()


class TestMigrationRollbackSql:
    """完整回滚链路（3a1b → f532）离线 SQL 必须可生成且无重复删除。"""

    def test_full_rollback_sql_generates_without_dup_fk(self):
        backend_dir = Path(__file__).resolve().parents[2]
        proc = subprocess.run(
            [sys.executable, "-m", "alembic", "downgrade", "3a1b2c3d4e5f:f532646fe643", "--sql"],
            cwd=backend_dir,
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert proc.returncode == 0, f"完整回滚 SQL 生成失败:\n{proc.stderr}"
        # 0003 回退应为无操作（修正版 f724 已是目标结构）；f724 回退只删一次外键
        drops = re.findall(r"DROP FOREIGN KEY (\w+)", proc.stdout)
        assert len(drops) == EXPECTED_FK_COUNT, (
            f"外键删除次数异常（期望每个只删一次={EXPECTED_FK_COUNT}）: {len(drops)}"
        )
        assert len(set(drops)) == EXPECTED_FK_COUNT, f"存在重复删除的外键: {drops}"
        assert "_ibfk" not in proc.stdout, "回滚 SQL 不应出现匿名外键名"
