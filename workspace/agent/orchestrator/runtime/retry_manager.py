"""失败重试 + Worker 自愈。后台 sweeper 标记 lost 任务并按指数退避重试。"""
import logging
import threading
import time
from typing import Callable

from task_store import TaskStore, TaskRecord
from worker_registry import WorkerRegistry
from dispatcher import Dispatcher

log = logging.getLogger(__name__)


class RetryManager:
    def __init__(
        self,
        task_store: TaskStore,
        registry: WorkerRegistry,
        dispatcher: Dispatcher,
        sweeper_interval: int = 15,
        retry_base_delay: float = 1.0,
        now_fn: Callable[[], float] | None = None,
        ts_fn: Callable[[], str] | None = None,
    ):
        self.task_store = task_store
        self.registry = registry
        self.dispatcher = dispatcher
        self.sweeper_interval = sweeper_interval
        self.retry_base_delay = retry_base_delay
        self._now = now_fn or time.time
        self._ts = ts_fn or (lambda: str(int(time.time())))
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._sweep_loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _sweep_loop(self) -> None:
        while not self._stop.wait(self.sweeper_interval):
            try:
                self.sweep_once()
            except Exception:
                log.exception("sweep_once failed")

    def sweep_once(self) -> dict:
        """一轮扫描：标记 dead worker 的 running 任务为 lost，重试符合条件的任务。"""
        marked = self._mark_lost_from_dead_workers()
        retried = self._retry_eligible()
        return {"marked_lost": marked, "retried": retried}

    def _mark_lost_from_dead_workers(self) -> int:
        dead = self.registry.dead_workers()
        count = 0
        for dw in dead:
            for rec in self.task_store.list_by_worker(dw.worker_id):
                if rec.status == "running":
                    self.task_store.update(
                        rec.task_id, status="lost",
                        error=f"worker {dw.worker_id} heartbeat timeout",
                        updated_at=self._ts(),
                    )
                    count += 1
        return count

    def _retry_eligible(self) -> int:
        now = self._now()
        count = 0
        for rec in self.task_store.list_retryable():
            if not self._backoff_elapsed(rec, now):
                continue
            try:
                self.dispatcher.dispatch(
                    role=rec.role, prompt=rec.prompt,
                )
                self.task_store.update(
                    rec.task_id,
                    retry_count=rec.retry_count + 1,
                    last_retry_at=self._ts(),
                    updated_at=self._ts(),
                )
                count += 1
            except Exception:
                log.exception("retry dispatch failed for %s", rec.task_id)
        return count

    def _backoff_elapsed(self, rec: TaskRecord, now: float) -> bool:
        delay = self.retry_base_delay * (2 ** rec.retry_count)
        if not rec.last_retry_at:
            return True
        try:
            last = float(rec.last_retry_at)
        except (ValueError, TypeError):
            return True
        return (now - last) >= delay

    def check_worker_healing(self, worker_id: str) -> int:
        """Worker 重新注册时调用：将其 lost 任务重新派发。"""
        w = self.registry.get_worker(worker_id)
        if w is None:
            return 0
        lost = [r for r in self.task_store.list_by_worker(worker_id)
                if r.status == "lost"]
        count = 0
        for rec in lost:
            try:
                self.dispatcher.dispatch(role=rec.role, prompt=rec.prompt)
                self.task_store.update(
                    rec.task_id, status="running",
                    worker_id=worker_id,
                    retry_count=rec.retry_count + 1,
                    last_retry_at=self._ts(),
                    updated_at=self._ts(),
                )
                count += 1
            except Exception:
                log.exception("healing dispatch failed for %s", rec.task_id)
        return count
