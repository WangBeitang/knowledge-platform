"""归一化规则单元测试（《数据对象设计》§9）。"""

from app.core.normalizer import normalize_question, question_hash


class TestNormalizeQuestion:
    def test_trim_and_collapse_whitespace(self):
        assert normalize_question("  如何 办理   风险测评  ") == "如何 办理 风险测评"

    def test_lowercase_english(self):
        assert normalize_question("What is Risk Level") == "what is risk level"

    def test_unify_punctuation(self):
        assert normalize_question("如何办理风险测评？") == "如何办理风险测评"
        # 设计只删除句尾问号和句号，感叹号保留
        assert normalize_question("如何办理风险测评!") == "如何办理风险测评!"
        assert normalize_question("如何，办理。风险测评") == "如何,办理.风险测评"

    def test_fullwidth_to_halfwidth(self):
        assert normalize_question("ＡＢＣ１２３") == "abc123"

    def test_keep_entity_and_codes(self):
        q = "订单ORD-2026-001如何退款？"
        assert normalize_question(q) == "订单ord-2026-001如何退款"

    def test_empty_input(self):
        assert normalize_question("") == ""
        assert normalize_question("   ") == ""

    def test_only_question_marks(self):
        assert normalize_question("？？？") == ""


class TestQuestionHash:
    def test_hash_stable(self):
        a = normalize_question("如何办理风险测评？")
        assert question_hash(a) == question_hash("如何办理风险测评")

    def test_hash_length(self):
        assert len(question_hash("x")) == 64
