"""优先级任务队列单元测试。"""
import time
import threading

import pytest

from agent_core.priority_queue import PriorityTaskQueue, TaskStatus


class TestPriorityTaskQueue:
    def test_submit_and_get_status(self):
        q = PriorityTaskQueue(num_workers=1)
        q.start()
        try:
            task_id = q.submit(lambda: "result", priority=5)
            status = q.get_status(task_id)
            assert status is not None
            assert status["task_id"] == task_id
            assert status["priority"] == 5
            assert status["status"] in ("pending", "running", "completed")
        finally:
            q.stop()

    def test_priority_ordering(self):
        """高优先级任务先执行。"""
        q = PriorityTaskQueue(num_workers=1)
        results = []
        lock = threading.Lock()

        def make_task(name, delay=0.01):
            def task():
                with lock:
                    results.append(name)
                time.sleep(delay)
                return name
            return task

        # 先暂停 worker，提交多个任务后再启动
        q._workers = []  # 不启动 worker
        q.submit(make_task("low"), priority=10)
        q.submit(make_task("high"), priority=1)
        q.submit(make_task("medium"), priority=5)

        # 启动 worker
        q.start()
        try:
            # 等待所有任务完成
            time.sleep(0.5)
            assert results == ["high", "medium", "low"]
        finally:
            q.stop()

    def test_cancel_pending_task(self):
        q = PriorityTaskQueue(num_workers=1)
        # 不启动 worker，任务保持 pending
        task_id = q.submit(lambda: "result")
        assert q.cancel(task_id) is True
        status = q.get_status(task_id)
        assert status["status"] == "cancelled"

    def test_cancel_completed_task_fails(self):
        q = PriorityTaskQueue(num_workers=1)
        q.start()
        try:
            task_id = q.submit(lambda: "result")
            time.sleep(0.1)  # 等待完成
            assert q.cancel(task_id) is False
        finally:
            q.stop()

    def test_cancel_nonexistent_task(self):
        q = PriorityTaskQueue()
        assert q.cancel("nonexistent") is False

    def test_get_result_completed(self):
        q = PriorityTaskQueue(num_workers=1)
        q.start()
        try:
            task_id = q.submit(lambda: 42)
            result = q.get_result(task_id, timeout=1.0)
            assert result == 42
        finally:
            q.stop()

    def test_get_result_failed(self):
        q = PriorityTaskQueue(num_workers=1)
        q.start()
        try:
            task_id = q.submit(lambda: (_ for _ in ()).throw(ValueError("fail")))
            with pytest.raises(ValueError, match="fail"):
                q.get_result(task_id, timeout=1.0)
        finally:
            q.stop()

    def test_get_result_timeout(self):
        q = PriorityTaskQueue(num_workers=1)
        # 不启动 worker，任务永远不会完成
        task_id = q.submit(lambda: time.sleep(100))
        with pytest.raises(TimeoutError):
            q.get_result(task_id, timeout=0.1)

    def test_get_result_cancelled(self):
        q = PriorityTaskQueue(num_workers=1)
        task_id = q.submit(lambda: "result")
        q.cancel(task_id)
        with pytest.raises(RuntimeError, match="cancelled"):
            q.get_result(task_id)

    def test_get_status_nonexistent(self):
        q = PriorityTaskQueue()
        assert q.get_status("nonexistent") is None

    def test_custom_task_id(self):
        q = PriorityTaskQueue(num_workers=1)
        q.start()
        try:
            task_id = q.submit(lambda: "ok", task_id="my-custom-id")
            assert task_id == "my-custom-id"
            status = q.get_status(task_id)
            assert status["task_id"] == "my-custom-id"
        finally:
            q.stop()

    def test_duplicate_task_id_raises(self):
        q = PriorityTaskQueue(num_workers=1)
        q.submit(lambda: "ok", task_id="dup-id")
        with pytest.raises(ValueError, match="already exists"):
            q.submit(lambda: "ok2", task_id="dup-id")

    def test_args_and_kwargs(self):
        q = PriorityTaskQueue(num_workers=1)
        q.start()
        try:
            task_id = q.submit(lambda x, y=10: x + y, 5, y=20)
            result = q.get_result(task_id, timeout=1.0)
            assert result == 25
        finally:
            q.stop()

    def test_pending_count(self):
        q = PriorityTaskQueue(num_workers=1)
        # 不启动 worker
        q.submit(lambda: "a")
        q.submit(lambda: "b")
        assert q.pending_count == 2

    def test_total_count(self):
        q = PriorityTaskQueue(num_workers=1)
        q.start()
        try:
            q.submit(lambda: "a")
            q.submit(lambda: "b")
            time.sleep(0.2)
            assert q.total_count == 2
        finally:
            q.stop()

    def test_concurrent_submit(self):
        """多线程并发提交任务。"""
        q = PriorityTaskQueue(num_workers=2)
        q.start()
        try:
            task_ids = []
            errors = []

            def submitter(i):
                try:
                    tid = q.submit(lambda: i, priority=i)
                    task_ids.append(tid)
                except Exception as e:
                    errors.append(e)

            threads = [threading.Thread(target=submitter, args=(i,)) for i in range(10)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

            assert not errors
            assert len(task_ids) == 10
            time.sleep(0.5)
            assert q.total_count == 10
        finally:
            q.stop()
