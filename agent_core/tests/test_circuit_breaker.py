"""熔断器单元测试。"""
import time
from unittest.mock import MagicMock

import pytest

from agent_core.circuit_breaker import (
    CircuitBreaker,
    CircuitBreakerOpenError,
    State,
)


class TestCircuitBreaker:
    def test_initial_state_closed(self):
        cb = CircuitBreaker()
        assert cb.state == State.CLOSED

    def test_success_resets_failure_count(self):
        cb = CircuitBreaker(failure_threshold=3)
        # 模拟 2 次失败
        for _ in range(2):
            with pytest.raises(ValueError):
                cb.call(lambda: (_ for _ in ()).throw(ValueError("fail")))
        assert cb.failure_count == 2
        # 成功调用，重置计数
        cb.call(lambda: "ok")
        assert cb.failure_count == 0
        assert cb.state == State.CLOSED

    def test_failure_threshold_trips_to_open(self):
        cb = CircuitBreaker(failure_threshold=3)
        for _ in range(3):
            with pytest.raises(ValueError):
                cb.call(lambda: (_ for _ in ()).throw(ValueError("fail")))
        assert cb.state == State.OPEN

    def test_open_state_rejects_calls(self):
        cb = CircuitBreaker(failure_threshold=2, recovery_timeout=10.0)
        # 触发熔断
        for _ in range(2):
            with pytest.raises(ValueError):
                cb.call(lambda: (_ for _ in ()).throw(ValueError("fail")))
        assert cb.state == State.OPEN
        # 后续调用被拒绝
        with pytest.raises(CircuitBreakerOpenError) as exc_info:
            cb.call(lambda: "ok")
        assert exc_info.value.retry_after > 0

    def test_half_open_after_recovery_timeout(self):
        cb = CircuitBreaker(failure_threshold=2, recovery_timeout=0.1)
        # 触发熔断
        for _ in range(2):
            with pytest.raises(ValueError):
                cb.call(lambda: (_ for _ in ()).throw(ValueError("fail")))
        assert cb.state == State.OPEN
        # 等待恢复时间
        time.sleep(0.15)
        assert cb.state == State.HALF_OPEN

    def test_half_open_success_recovers_to_closed(self):
        cb = CircuitBreaker(
            failure_threshold=2, recovery_timeout=0.1, success_threshold=2
        )
        # 触发熔断
        for _ in range(2):
            with pytest.raises(ValueError):
                cb.call(lambda: (_ for _ in ()).throw(ValueError("fail")))
        time.sleep(0.15)
        assert cb.state == State.HALF_OPEN
        # HALF_OPEN 成功 2 次 → CLOSED
        cb.call(lambda: "ok1")
        cb.call(lambda: "ok2")
        assert cb.state == State.CLOSED
        assert cb.failure_count == 0

    def test_half_open_failure_returns_to_open(self):
        cb = CircuitBreaker(failure_threshold=2, recovery_timeout=0.1)
        # 触发熔断
        for _ in range(2):
            with pytest.raises(ValueError):
                cb.call(lambda: (_ for _ in ()).throw(ValueError("fail")))
        time.sleep(0.15)
        assert cb.state == State.HALF_OPEN
        # HALF_OPEN 失败 → OPEN
        with pytest.raises(ValueError):
            cb.call(lambda: (_ for _ in ()).throw(ValueError("fail again")))
        assert cb.state == State.OPEN

    def test_reset(self):
        cb = CircuitBreaker(failure_threshold=2)
        for _ in range(2):
            with pytest.raises(ValueError):
                cb.call(lambda: (_ for _ in ()).throw(ValueError("fail")))
        assert cb.state == State.OPEN
        cb.reset()
        assert cb.state == State.CLOSED
        assert cb.failure_count == 0

    def test_get_status(self):
        cb = CircuitBreaker(failure_threshold=5, recovery_timeout=60.0)
        status = cb.get_status()
        assert status["state"] == "closed"
        assert status["failure_count"] == 0
        assert status["failure_threshold"] == 5
        assert status["recovery_timeout"] == 60.0

    def test_get_status_open(self):
        cb = CircuitBreaker(failure_threshold=2, recovery_timeout=10.0)
        for _ in range(2):
            with pytest.raises(ValueError):
                cb.call(lambda: (_ for _ in ()).throw(ValueError("fail")))
        status = cb.get_status()
        assert status["state"] == "open"
        assert status["failure_count"] == 2

    def test_kwargs_passed_through(self):
        cb = CircuitBreaker()
        result = cb.call(lambda x, y=10: x + y, 5, y=20)
        assert result == 25

    def test_concurrent_access(self):
        """多线程并发访问不应导致状态不一致。"""
        import threading

        cb = CircuitBreaker(failure_threshold=100)
        errors = []

        def worker():
            try:
                for _ in range(10):
                    cb.call(lambda: "ok")
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=worker) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors
        assert cb.state == State.CLOSED
