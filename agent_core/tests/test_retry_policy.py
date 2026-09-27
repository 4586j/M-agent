"""重试策略单元测试。"""
import asyncio
import time
from unittest.mock import MagicMock, patch

import pytest

from agent_core.retry_policy import RetryPolicy


class TestRetryPolicy:
    def test_no_retry_on_success(self):
        policy = RetryPolicy(max_retries=3)
        func = MagicMock(return_value="ok")
        result = policy.execute(func)
        assert result == "ok"
        assert func.call_count == 1

    def test_no_retry_on_non_retryable_exception(self):
        policy = RetryPolicy(max_retries=3)
        func = MagicMock(side_effect=ValueError("bad value"))
        with pytest.raises(ValueError, match="bad value"):
            policy.execute(func)
        assert func.call_count == 1  # 不重试

    def test_retry_on_timeout_error(self):
        policy = RetryPolicy(max_retries=2, backoff_seconds=0.01, jitter=False)
        func = MagicMock(side_effect=[TimeoutError("timeout"), TimeoutError("timeout"), "ok"])
        result = policy.execute(func)
        assert result == "ok"
        assert func.call_count == 3

    def test_retry_on_connection_error(self):
        policy = RetryPolicy(max_retries=2, backoff_seconds=0.01, jitter=False)
        func = MagicMock(side_effect=[ConnectionError("conn"), "ok"])
        result = policy.execute(func)
        assert result == "ok"
        assert func.call_count == 2

    def test_max_retries_exhausted(self):
        policy = RetryPolicy(max_retries=2, backoff_seconds=0.01, jitter=False)
        func = MagicMock(side_effect=TimeoutError("always fail"))
        with pytest.raises(TimeoutError, match="always fail"):
            policy.execute(func)
        assert func.call_count == 3  # 1 + 2 retries

    def test_exponential_backoff(self):
        policy = RetryPolicy(
            max_retries=3, backoff_seconds=0.1, backoff_multiplier=2.0, jitter=False
        )
        # 验证延迟计算
        assert policy._calculate_delay(0) == 0.1
        assert policy._calculate_delay(1) == 0.2
        assert policy._calculate_delay(2) == 0.4

    def test_max_backoff(self):
        policy = RetryPolicy(
            max_retries=5,
            backoff_seconds=1.0,
            backoff_multiplier=10.0,
            max_backoff=5.0,
            jitter=False,
        )
        assert policy._calculate_delay(0) == 1.0
        assert policy._calculate_delay(1) == 5.0  # capped
        assert policy._calculate_delay(2) == 5.0  # capped

    def test_jitter_adds_randomness(self):
        policy = RetryPolicy(backoff_seconds=1.0, jitter=True)
        delays = [policy._calculate_delay(0) for _ in range(100)]
        # 有抖动时延迟不完全相同
        assert len(set(delays)) > 1
        # 范围在 0.5-1.5 之间
        assert all(0.5 <= d <= 1.5 for d in delays)

    def test_on_retry_callback(self):
        policy = RetryPolicy(max_retries=2, backoff_seconds=0.01, jitter=False)
        callback = MagicMock()
        side_effects = [TimeoutError(), TimeoutError(), "ok"]
        func = MagicMock(side_effect=side_effects)
        policy.execute(func, on_retry=callback)
        assert callback.call_count == 2
        assert callback.call_args_list[0][0][0] == 1  # attempt number

    def test_zero_retries(self):
        policy = RetryPolicy(max_retries=0)
        func = MagicMock(side_effect=TimeoutError("fail"))
        with pytest.raises(TimeoutError):
            policy.execute(func)
        assert func.call_count == 1

    def test_kwargs_passed_through(self):
        policy = RetryPolicy()
        func = MagicMock(return_value=30)
        result = policy.execute(func, 10, y=20)
        func.assert_called_once_with(10, y=20)
        assert result == 30


class TestRetryPolicyAsync:
    def test_async_no_retry_on_success(self):
        import asyncio
        policy = RetryPolicy(max_retries=3)

        async def async_func():
            return "ok"

        result = asyncio.run(policy.execute_async(async_func))
        assert result == "ok"

    def test_async_retry_on_timeout(self):
        import asyncio
        policy = RetryPolicy(max_retries=2, backoff_seconds=0.01, jitter=False)
        call_count = 0

        async def async_func():
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                raise TimeoutError("timeout")
            return "ok"

        result = asyncio.run(policy.execute_async(async_func))
        assert result == "ok"
        assert call_count == 3

    def test_async_no_retry_on_value_error(self):
        import asyncio
        policy = RetryPolicy(max_retries=3)

        async def async_func():
            raise ValueError("bad")

        with pytest.raises(ValueError):
            asyncio.run(policy.execute_async(async_func))
