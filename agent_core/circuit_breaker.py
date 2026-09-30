"""熔断器（Circuit Breaker）。

防止 LLM API / 节点连续失败时无效调用烧钱。状态机：
- CLOSED（正常）：调用通过，失败计数+1
- OPEN（熔断）：直接拒绝，返回 CircuitBreakerOpenError
- HALF_OPEN（试探）：允许少量调用试探恢复

连续失败 N 次 → OPEN；等待 recovery_timeout 后 → HALF_OPEN；
试探成功 M 次 → CLOSED；试探失败 → OPEN。

接线点：runtime/dispatch_breaker.BreakerBoard 按 worker_id 持有独立实例，
派发层自动跳过熔断中的节点（P1 运维可靠性批次）。
"""
import enum
import threading
import time
from typing import Any, Callable


class CircuitBreakerError(Exception):
    """熔断器基础异常。"""


class CircuitBreakerOpenError(CircuitBreakerError):
    """熔断器已打开，拒绝调用。"""

    def __init__(self, retry_after: float):
        self.retry_after = retry_after
        super().__init__(f"Circuit breaker is open. Retry after {retry_after:.1f}s")


class State(enum.Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class CircuitBreaker:
    """熔断器实现。

    Args:
        failure_threshold: 连续失败次数阈值，达到后触发熔断
        recovery_timeout: 熔断后等待时间（秒），超时后进入 HALF_OPEN 试探
        success_threshold: HALF_OPEN 状态下连续成功次数，达到后恢复 CLOSED
    """

    def __init__(
        self,
        failure_threshold: int = 5,
        recovery_timeout: float = 60.0,
        success_threshold: int = 2,
    ):
        self._failure_threshold = max(1, failure_threshold)
        self._recovery_timeout = max(0.1, recovery_timeout)
        self._success_threshold = max(1, success_threshold)

        self._state = State.CLOSED
        self._failure_count = 0
        self._success_count = 0
        self._last_failure_time: float = 0.0
        self._lock = threading.Lock()

    @property
    def state(self) -> State:
        """当前状态（线程安全）。"""
        with self._lock:
            if self._state == State.OPEN:
                # 检查是否超过恢复时间，自动转为 HALF_OPEN
                if time.monotonic() - self._last_failure_time >= self._recovery_timeout:
                    self._state = State.HALF_OPEN
                    self._success_count = 0
            return self._state

    @property
    def failure_count(self) -> int:
        with self._lock:
            return self._failure_count

    @property
    def retry_after(self) -> float:
        """OPEN 状态下还需等待多久进入 HALF_OPEN（秒，非负；非 OPEN 为 0）。"""
        with self._lock:
            if self._state is not State.OPEN:
                return 0.0
            return max(0.0, self._recovery_timeout
                       - (time.monotonic() - self._last_failure_time))

    def call(self, func: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        """通过熔断器调用函数。

        - CLOSED：正常调用
        - OPEN：直接抛 CircuitBreakerOpenError
        - HALF_OPEN：试探调用，成功则计数，失败则回到 OPEN
        """
        current_state = self.state  # 自动处理 OPEN → HALF_OPEN 转换

        if current_state == State.OPEN:
            retry_after = self._recovery_timeout - (
                time.monotonic() - self._last_failure_time
            )
            raise CircuitBreakerOpenError(max(0.0, retry_after))

        try:
            result = func(*args, **kwargs)
            self._on_success()
            return result
        except Exception as e:
            self._on_failure()
            raise

    def _on_success(self) -> None:
        """调用成功处理。"""
        with self._lock:
            if self._state == State.HALF_OPEN:
                self._success_count += 1
                if self._success_count >= self._success_threshold:
                    # 恢复 CLOSED
                    self._state = State.CLOSED
                    self._failure_count = 0
                    self._success_count = 0
            elif self._state == State.CLOSED:
                # CLOSED 状态下成功，重置失败计数
                self._failure_count = 0

    def _on_failure(self) -> None:
        """调用失败处理。"""
        with self._lock:
            self._failure_count += 1
            self._last_failure_time = time.monotonic()

            if self._state == State.HALF_OPEN:
                # HALF_OPEN 失败，回到 OPEN
                self._state = State.OPEN
                self._success_count = 0
            elif self._state == State.CLOSED:
                if self._failure_count >= self._failure_threshold:
                    # 达到阈值，触发熔断
                    self._state = State.OPEN

    def reset(self) -> None:
        """手动重置熔断器到 CLOSED 状态。"""
        with self._lock:
            self._state = State.CLOSED
            self._failure_count = 0
            self._success_count = 0
            self._last_failure_time = 0.0

    def allow(self) -> bool:
        """是否放行一次调用（OPEN 拒绝；OPEN 超时自动转 HALF_OPEN 放行试探）。"""
        return self.state is not State.OPEN

    def record_success(self) -> None:
        """外部调用成功后上报（HALF_OPEN 试探计数 / CLOSED 重置失败连击）。"""
        self._on_success()

    def record_failure(self) -> bool:
        """外部调用失败后上报。返回是否本次触发了熔断（CLOSED→OPEN 边沿）。"""
        was_open = self.state is State.OPEN
        self._on_failure()
        return not was_open and self.state is State.OPEN

    def get_status(self) -> dict:
        """获取熔断器状态信息。"""
        with self._lock:
            state = self._state
            if state == State.OPEN:
                elapsed = time.monotonic() - self._last_failure_time
                if elapsed >= self._recovery_timeout:
                    state = State.HALF_OPEN
            return {
                "state": state.value,
                "failure_count": self._failure_count,
                "failure_threshold": self._failure_threshold,
                "recovery_timeout": self._recovery_timeout,
                "success_count": self._success_count if state == State.HALF_OPEN else 0,
                "success_threshold": self._success_threshold,
            }
