# NOTE: 预留模块（仅被 agent_core/tests 引用，无生产代码引用）。
# 未接入内核的执行路径；若未来启用需接线到 kernel/worker，且删除时
# 仅删本文件与对应测试。是否有必要保留由项目组评估。

"""重试策略（Retry Policy）。

失败任务自动重试，支持指数退避。区分可重试异常和不可重试异常。
"""
import asyncio
import logging
import random
import time
from typing import Any, Callable, Type

logger = logging.getLogger(__name__)


# 可重试的异常类型（网络/超时/临时错误）
RETRYABLE_EXCEPTIONS: tuple[Type[Exception], ...] = (
    TimeoutError,
    ConnectionError,
    OSError,
    # SDK 临时错误可在此添加
)

# 不可重试的异常（业务逻辑错误）
NON_RETRYABLE_EXCEPTIONS: tuple[Type[Exception], ...] = (
    ValueError,
    PermissionError,
    KeyboardInterrupt,
)


class RetryPolicy:
    """重试策略配置与执行。

    Args:
        max_retries: 最大重试次数（不含首次调用）
        backoff_seconds: 退避基数（秒）
        backoff_multiplier: 退避乘数（指数退避）
        max_backoff: 最大退避时间（秒）
        jitter: 是否添加随机抖动（避免惊群效应）
    """

    def __init__(
        self,
        max_retries: int = 2,
        backoff_seconds: float = 1.0,
        backoff_multiplier: float = 2.0,
        max_backoff: float = 60.0,
        jitter: bool = True,
    ):
        self.max_retries = max(0, max_retries)
        self.backoff_seconds = max(0.0, backoff_seconds)
        self.backoff_multiplier = max(1.0, backoff_multiplier)
        self.max_backoff = max(0.0, max_backoff)
        self.jitter = jitter

    def _calculate_delay(self, attempt: int) -> float:
        """计算第 attempt 次重试的等待时间。"""
        delay = self.backoff_seconds * (self.backoff_multiplier ** attempt)
        delay = min(delay, self.max_backoff)
        if self.jitter:
            delay *= 0.5 + random.random()  # 50%-150% 随机
        return delay

    def _is_retryable(self, exc: Exception) -> bool:
        """判断异常是否可重试。"""
        # 不可重试异常优先检查
        if isinstance(exc, NON_RETRYABLE_EXCEPTIONS):
            return False
        # 可重试异常列表
        if isinstance(exc, RETRYABLE_EXCEPTIONS):
            return True
        # 默认不重试未知异常
        return False

    def execute(
        self,
        func: Callable[..., Any],
        *args: Any,
        on_retry: Callable[[int, Exception], None] | None = None,
        **kwargs: Any,
    ) -> Any:
        """同步执行函数，失败时按策略重试。

        Args:
            func: 要执行的函数
            on_retry: 重试回调 (attempt, exception) -> None

        Returns:
            函数返回值

        Raises:
            最后一次尝试的异常
        """
        last_exception: Exception | None = None

        for attempt in range(self.max_retries + 1):
            try:
                return func(*args, **kwargs)
            except Exception as e:
                last_exception = e

                # 不可重试，直接抛出
                if not self._is_retryable(e):
                    raise

                # 已达最大重试次数
                if attempt >= self.max_retries:
                    raise

                # 计算退避时间并等待
                delay = self._calculate_delay(attempt)
                logger.warning(
                    "重试 %d/%d（等待 %.2f 秒）：%s",
                    attempt + 1,
                    self.max_retries,
                    delay,
                    e,
                )
                if on_retry:
                    on_retry(attempt + 1, e)
                time.sleep(delay)

        # 理论上不会到这里
        raise last_exception  # type: ignore[misc]

    async def execute_async(
        self,
        func: Callable[..., Any],
        *args: Any,
        on_retry: Callable[[int, Exception], None] | None = None,
        **kwargs: Any,
    ) -> Any:
        """异步执行函数，失败时按策略重试。"""
        last_exception: Exception | None = None

        for attempt in range(self.max_retries + 1):
            try:
                result = func(*args, **kwargs)
                # 如果返回的是 coroutine，需要 await
                if asyncio.iscoroutine(result):
                    result = await result
                return result
            except Exception as e:
                last_exception = e

                if not self._is_retryable(e):
                    raise

                if attempt >= self.max_retries:
                    raise

                delay = self._calculate_delay(attempt)
                logger.warning(
                    "异步重试 %d/%d（等待 %.2f 秒）：%s",
                    attempt + 1,
                    self.max_retries,
                    delay,
                    e,
                )
                if on_retry:
                    on_retry(attempt + 1, e)
                await asyncio.sleep(delay)

        raise last_exception  # type: ignore[misc]
