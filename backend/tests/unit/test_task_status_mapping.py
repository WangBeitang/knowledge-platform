"""任务状态映射与安全错误摘要单测（Stage 3 §十八/§十九）。"""

from app.core.enums import IntegrationTaskStatus
from app.services.task_service import map_upstream_task_status, safe_error_summary


class TestTaskStatusMapping:
    def test_known_statuses(self):
        assert map_upstream_task_status("pending") == ("pending", "pending")
        assert map_upstream_task_status("processing") == ("running", "processing")
        assert map_upstream_task_status("completed") == ("succeeded", "completed")
        assert map_upstream_task_status("failed") == ("failed", "failed")

    def test_unknown_status_never_succeeded(self):
        # 未知状态 → running + 保留原文，绝不能映射 succeeded
        status, raw = map_upstream_task_status("weird_state")
        assert status == IntegrationTaskStatus.running.value
        assert status != IntegrationTaskStatus.succeeded.value
        assert raw == "weird_state"

    def test_empty_status_is_unknown_not_succeeded(self):
        status, raw = map_upstream_task_status("")
        assert status == IntegrationTaskStatus.running.value
        assert status != IntegrationTaskStatus.succeeded.value


class TestSafeErrorSummary:
    def test_strips_paths(self):
        text = "处理失败: C:\\Users\\x\\output\\task_1\\a.pdf 以及 /data/rag/local_dir/xx"
        cleaned = safe_error_summary(text)
        assert "<path>" in cleaned
        assert "C:\\Users" not in cleaned
        assert "/data/rag" not in cleaned

    def test_truncates_long_text(self):
        long_text = "错误信息" * 1000
        cleaned = safe_error_summary(long_text, limit=200)
        assert cleaned is not None
        assert len(cleaned) <= 200

    def test_none_and_blank(self):
        assert safe_error_summary(None) is None
        assert safe_error_summary("   ") is None

    def test_single_line_collapse(self):
        cleaned = safe_error_summary("第一行\n第二行\t空白", limit=100)
        assert "\n" not in cleaned
        assert "\t" not in cleaned
