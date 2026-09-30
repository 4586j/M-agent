"""worker 连续失败熔断（派发层）：复用 agent_core.CircuitBreaker 状态机。

按 worker_id 维护独立熔断器：
- CLOSED：正常派发；任务终态 failed/派发失败 → record_failure，done → record_success
- 连续失败 N 次（默认 5）→ OPEN：派发解析自动跳过该节点
- recovery_timeout（默认 60s）后 HALF_OPEN：放行试探，成功恢复、失败回 OPEN

与单任务预算（task_budget_usd）互补：那个管「一次执行烧多少钱」，这个管
「一个节点坏了继续往它派发」。开关节点用环境变量
ORCH_CIRCUIT_BREAKER_ENABLED=0 可整体关闭。
"""
import logging
import threading
import time
from typing import Callable

from agent_core.circuit_breaker import CircuitBreaker

log = logging.getLogger(__name__)


class BreakerBoard:
    """per-worker 熔断板（线程安全：dispatch 线程池并发调用）。"""

    def __init__(self, failure_threshold: int = 5, recovery_timeout: float = 60.0,
                 enabled: bool = True,
                 on_open: Callable[[str, int], None] | None = None):
        self._threshold = max(1, failure_threshold)
        self._recovery = max(0.1, recovery_timeout)
        self._enabled = enabled
        self._on_open = on_open  # (worker_id, failure_count)：触发熔断边沿回调
        self._lock = threading.Lock()
        self._breakers: dict[str, CircuitBreaker] = {}

    def _breaker_for(self, worker_id: str) -> CircuitBreaker:
        with self._lock:
            cb = self._breakers.get(worker_id)
            if cb is None:
                cb = CircuitBreaker(failure_threshold=self._threshold,
                                    recovery_timeout=self._recovery)
                self._breakers[worker_id] = cb
            return cb

    def allow(self, worker_id: str) -> tuple[bool, float]:
        """是否允许向该节点派发。返回 (allowed, retry_after_seconds)。

        未启用/未知节点一律放行（熔断器惰性创建——不预建，避免为从未失败
        的节点保留状态）。
        """
        if not self._enabled:
            return True, 0.0
        with self._lock:
            cb = self._breakers.get(worker_id)
        if cb is None or cb.allow():
            return True, 0.0
        return False, cb.retry_after

    def record_success(self, worker_id: str) -> None:
        if not self._enabled or not worker_id:
            return
        self._breaker_for(worker_id).record_success()

    def record_failure(self, worker_id: str) -> None:
        """记录一次节点失败；触发熔断边沿时回调 on_open（外发事件）。"""
        if not self._enabled or not worker_id:
            return
        opened = self._breaker_for(worker_id).record_failure()
        if opened:
            log.warning("节点熔断开启 worker=%s threshold=%d recovery=%.0fs",
                        worker_id, self._threshold, self._recovery)
            if self._on_open is not None:
                try:
                    self._on_open(worker_id, self._threshold)
                except Exception as e:  # noqa: BLE001 - 回调失败不影响熔断
                    log.warning("breaker on_open 回调失败 worker=%s: %r",
                                worker_id, e)

    def status(self) -> dict:
        """运维视图：各节点熔断状态（供 /stats 或排障端点）。"""
        with self._lock:
            items = list(self._breakers.items())
        return {wid: cb.get_status() for wid, cb in items}
