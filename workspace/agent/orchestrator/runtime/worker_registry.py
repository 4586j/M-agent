"""worker 注册表：注册、心跳保活、按角色轮询选择。"""
import itertools
import time
from dataclasses import dataclass
from typing import Callable


@dataclass
class WorkerInfo:
    worker_id: str
    role: str
    host: str
    port: int
    capabilities: dict
    last_heartbeat: float


class WorkerRegistry:
    def __init__(self, heartbeat_timeout: int = 45, now_fn: Callable[[], float] | None = None):
        self.heartbeat_timeout = heartbeat_timeout
        self._now = now_fn or time.time
        self._workers: dict[str, WorkerInfo] = {}
        self._rr: dict[str, itertools.cycle] = {}

    def register(self, worker: WorkerInfo) -> None:
        worker.last_heartbeat = self._now()
        self._workers[worker.worker_id] = worker
        self._rr.pop(worker.role, None)  # 重建轮询器

    def heartbeat(self, worker_id: str, load: int = 0) -> None:
        w = self._workers.get(worker_id)
        if w is None:
            return
        w.last_heartbeat = self._now()

    def _alive(self) -> list[WorkerInfo]:
        now = self._now()
        return [w for w in self._workers.values() if now - w.last_heartbeat <= self.heartbeat_timeout]

    def active_workers(self) -> list[WorkerInfo]:
        return list(self._alive())

    def select_worker(self, role: str = "executor") -> WorkerInfo | None:
        alive = [w for w in self._alive() if w.role == role]
        if not alive:
            return None
        if role not in self._rr:
            self._rr[role] = itertools.cycle(alive)
            return next(self._rr[role])
        # 轮询但跳过已失联的
        for _ in range(len(alive)):
            cand = next(self._rr[role])
            if cand in alive:
                return cand
        return alive[0]
