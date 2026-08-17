"""FAQ 精确缓存键确定性测试（冻结《数据对象设计》§6.1）。

Key 格式：faq:v1:{knowledge_scope}:{normalized_question_hash}
- 必须包含 knowledge_scope（禁止跨范围查询）；
- hash 必须是 normalize_question + question_hash 的单一事实来源；
- 固定输入 → 固定 key（测试固定 key）。
"""

from app.core.normalizer import normalize_question, question_hash
from app.services.faq_service import FAQ_CACHE_PREFIX, faq_cache_key


def test_fixed_key_deterministic():
    scope = "internal_shared"
    normalized = normalize_question("客户如何办理风险测评？")
    digest = question_hash(normalized)
    key = faq_cache_key(scope, digest)
    assert key == f"faq:v1:internal_shared:{digest}"
    # 固定输入必须产生固定输出
    assert key == faq_cache_key(scope, digest)


def test_key_contains_scope():
    hash_value = "a" * 64
    assert faq_cache_key("admin_private", hash_value).startswith(
        f"{FAQ_CACHE_PREFIX}:admin_private:"
    )
    assert faq_cache_key("internal_shared", hash_value).startswith(
        f"{FAQ_CACHE_PREFIX}:internal_shared:"
    )
    assert faq_cache_key("external_public", hash_value).startswith(
        f"{FAQ_CACHE_PREFIX}:external_public:"
    )


def test_same_hash_different_scope_different_key():
    """同一归一化问题在不同 scope 下 key 不同（禁止跨范围命中）。"""
    normalized = normalize_question("如何重置密码")
    digest = question_hash(normalized)
    key_internal = faq_cache_key("internal_shared", digest)
    key_external = faq_cache_key("external_public", digest)
    assert key_internal != key_external


def test_normalized_equivalence_same_key():
    """标点/大小写/空白归一化后相同的两个问题必须命中同一 FAQ key。"""
    q1 = normalize_question("客户如何办理风险测评？")
    q2 = normalize_question(" 客户如何办理风险测评 ")
    assert q1 == q2
    assert faq_cache_key("internal_shared", question_hash(q1)) == faq_cache_key(
        "internal_shared", question_hash(q2)
    )


def test_hash_sha256_hex():
    digest = question_hash("abc")
    assert len(digest) == 64
    assert digest == question_hash("abc")


def test_trailing_question_marks_removed_before_hash():
    q1 = normalize_question("什么是双录？")
    q2 = normalize_question("什么是双录")
    assert q1 == q2
    assert question_hash(q1) == question_hash(q2)
