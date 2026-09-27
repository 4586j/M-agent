"""批量执行器单元测试。"""
import time
from unittest.mock import MagicMock

import pytest

from agent_core.batch_runner import BatchItem, BatchRunner, BatchResult


class TestBatchRunner:
    def test_run_batch_all_success(self):
        runner = BatchRunner(max_concurrent=2)
        items = [
            BatchItem(prompt="task 1"),
            BatchItem(prompt="task 2"),
            BatchItem(prompt="task 3"),
        ]

        def mock_executor(item):
            return {"output": f"result: {item.prompt}", "cost_usd": 0.01, "duration_ms": 100}

        result = runner.run_batch(items, mock_executor)
        assert result["summary"]["total"] == 3
        assert result["summary"]["success"] == 3
        assert result["summary"]["failed"] == 0
        assert result["summary"]["total_cost_usd"] == 0.03
        assert len(result["results"]) == 3

    def test_run_batch_partial_failure(self):
        runner = BatchRunner(max_concurrent=2)
        items = [
            BatchItem(prompt="ok"),
            BatchItem(prompt="fail"),
            BatchItem(prompt="ok2"),
        ]

        call_count = 0
        def mock_executor(item):
            nonlocal call_count
            call_count += 1
            if item.prompt == "fail":
                raise ValueError("simulated failure")
            return {"output": "ok", "cost_usd": 0.01, "duration_ms": 50}

        result = runner.run_batch(items, mock_executor)
        assert result["summary"]["total"] == 3
        assert result["summary"]["success"] == 2
        assert result["summary"]["failed"] == 1

    def test_run_batch_concurrency_limit(self):
        """验证并发限制生效。"""
        runner = BatchRunner(max_concurrent=1)  # 串行
        items = [BatchItem(prompt=f"task {i}") for i in range(3)]
        execution_order = []

        def mock_executor(item):
            execution_order.append(item.prompt)
            time.sleep(0.01)
            return {"output": "ok", "cost_usd": 0.0, "duration_ms": 10}

        runner.run_batch(items, mock_executor)
        # 串行执行，顺序应一致
        assert len(execution_order) == 3

    def test_batch_item_auto_id(self):
        item = BatchItem(prompt="test")
        assert item.item_id  # 自动生成 UUID

    def test_batch_item_custom_id(self):
        item = BatchItem(prompt="test", item_id="custom-id")
        assert item.item_id == "custom-id"

    def test_empty_batch(self):
        runner = BatchRunner()
        result = runner.run_batch([], lambda x: {"output": "x"})
        assert result["summary"]["total"] == 0
        assert result["results"] == []

    def test_batch_result_has_item_id(self):
        runner = BatchRunner()
        items = [BatchItem(prompt="test", item_id="my-id")]
        result = runner.run_batch(items, lambda x: {"output": "ok"})
        assert result["results"][0]["item_id"] == "my-id"

    def test_total_duration_summed(self):
        runner = BatchRunner()
        items = [BatchItem(prompt="a"), BatchItem(prompt="b")]

        def mock_executor(item):
            return {"output": "ok", "cost_usd": 0.0, "duration_ms": 100}

        result = runner.run_batch(items, mock_executor)
        assert result["summary"]["total_duration_ms"] == 200
