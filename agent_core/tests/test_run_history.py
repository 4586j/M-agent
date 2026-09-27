"""RunHistoryStore 单元测试。"""
import json
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from agent_core.run_history import RunHistoryStore, build_run_record


@pytest.fixture
def tmp_history_dir(tmp_path):
    return str(tmp_path / "history")


@pytest.fixture
def store(tmp_history_dir):
    return RunHistoryStore(tmp_history_dir, max_records=10)


def _make_record(is_error=False, cost=0.01, duration=1000, turns=1, ts=None):
    rec = {
        "run_id": f"run-{id(is_error)}",
        "is_error": is_error,
        "error_type": "timeout" if is_error else "",
        "cost_usd": cost,
        "duration_ms": duration,
        "turns": turns,
    }
    if ts:
        rec["timestamp"] = ts
    return rec


class TestRunHistoryStore:
    def test_append_creates_file(self, store, tmp_history_dir):
        rec = _make_record()
        store.append(rec)
        assert store._path.is_file()

    def test_append_and_query(self, store):
        rec = _make_record()
        store.append(rec)
        records, total = store.query()
        assert total == 1
        assert len(records) == 1
        assert records[0]["run_id"] == rec["run_id"]

    def test_query_pagination(self, store):
        for i in range(5):
            store.append(_make_record())
        records, total = store.query(limit=2, offset=0)
        assert total == 5
        assert len(records) == 2
        records, total = store.query(limit=2, offset=3)
        assert len(records) == 2

    def test_query_filter_success(self, store):
        store.append(_make_record(is_error=False))
        store.append(_make_record(is_error=True))
        store.append(_make_record(is_error=False))
        records, total = store.query(status="success")
        assert total == 2
        records, total = store.query(status="error")
        assert total == 1

    def test_max_records_truncation(self, tmp_history_dir):
        store = RunHistoryStore(tmp_history_dir, max_records=5)
        for i in range(10):
            store.append({"run_id": f"run-{i}", "is_error": False})
        records, total = store.query()
        assert total == 5
        # 保留最新的 5 条
        assert records[0]["run_id"] == "run-5"
        assert records[-1]["run_id"] == "run-9"

    def test_get_by_run_id(self, store):
        store.append({"run_id": "abc-123", "is_error": False})
        store.append({"run_id": "def-456", "is_error": True})
        rec = store.get_by_run_id("abc-123")
        assert rec is not None
        assert rec["run_id"] == "abc-123"
        rec = store.get_by_run_id("nonexistent")
        assert rec is None

    def test_get_records_since(self, store):
        now = datetime.now(timezone.utc)
        old_ts = (now - timedelta(hours=2)).isoformat()
        new_ts = (now - timedelta(minutes=5)).isoformat()
        store.append({"run_id": "old", "timestamp": old_ts, "is_error": False})
        store.append({"run_id": "new", "timestamp": new_ts, "is_error": False})
        records = store.get_records_since(now - timedelta(hours=1))
        assert len(records) == 1
        assert records[0]["run_id"] == "new"

    def test_empty_store_query(self, store):
        records, total = store.query()
        assert total == 0
        assert records == []

    def test_corrupted_line_skipped(self, store):
        store.append({"run_id": "good-1", "is_error": False})
        # 手动写入一行损坏 JSON
        store._dir.mkdir(parents=True, exist_ok=True)
        with open(store._path, "a", encoding="utf-8") as f:
            f.write("{broken json\n")
        store.append({"run_id": "good-2", "is_error": False})
        records, total = store.query()
        assert total == 2
        ids = [r["run_id"] for r in records]
        assert "good-1" in ids
        assert "good-2" in ids

    def test_append_auto_generates_run_id_and_timestamp(self, store):
        rec = {"is_error": False}
        store.append(rec)
        records, _ = store.query()
        assert records[0].get("run_id")
        assert records[0].get("timestamp")

    def test_append_dir_creation_failure_silent(self, tmp_path):
        # 使用一个不可写的路径（如果可能）
        store = RunHistoryStore(str(tmp_path / "nonexistent" / "deep" / "dir"),
                                max_records=10)
        # 不应该抛异常
        store.append({"run_id": "test", "is_error": False})


class TestBuildRunRecord:
    def test_basic(self):
        result = {
            "is_error": False,
            "error_type": "",
            "cost_usd": 0.05,
            "duration_ms": 3000,
            "turns": 2,
            "meta": {"trace": {"steps": [1, 2]}},
        }
        rec = build_run_record("hello world", "executor", "claude-3", result)
        assert rec["prompt_preview"] == "hello world"
        assert rec["persona"] == "executor"
        assert rec["model"] == "claude-3"
        assert rec["cost_usd"] == 0.05
        assert rec["has_trace"] is True
        assert rec["trace"] == {"steps": [1, 2]}
        assert rec["run_id"]
        assert rec["timestamp"]

    def test_prompt_truncation(self):
        long_prompt = "x" * 500
        rec = build_run_record(long_prompt, "executor", "claude-3", {})
        assert len(rec["prompt_preview"]) == 200

    def test_no_trace(self):
        rec = build_run_record("test", "executor", "claude-3", {"meta": {}})
        assert rec["has_trace"] is False
        assert rec["trace"] is None
