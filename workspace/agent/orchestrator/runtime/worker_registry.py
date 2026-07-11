"""worker 注册表：注册、心跳保活、按角色轮询/按负载选择。"""
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
    load: int = 0


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

    def unregister(self, worker_id: str) -> bool:
        """移除 worker；返回是否确实移除了一个。"""
        w = self._workers.pop(worker_id, None)
        if w is not None:
            self._rr.pop(w.role, None)
        return w is not None

    def heartbeat(self, worker_id: str, load: int = 0) -> None:
        w = self._workers.get(worker_id)
        if w is None:
            return
        w.last_heartbeat = self._now()
        w.load = load

    def _alive(self) -> list[WorkerInfo]:
        now = self._now()
        return [w for w in self._workers.values() if now - w.last_heartbeat <= self.heartbeat_timeout]

    def active_workers(self) -> list[WorkerInfo]:
        return list(self._alive())

    def all_workers(self) -> list[WorkerInfo]:
        """全部在册 worker（含已超时未剔除的），供产物代理等查询。"""
        return list(self._workers.values())

    def select_worker(self, role: str = "executor",
                      strategy: str = "round_robin") -> WorkerInfo | None:
        """选择 worker。strategy: round_robin（默认，兼容）| least_load（按 load 升序）。"""
        alive = [w for w in self._alive() if w.role == role]
        if not alive:
            return None
        if strategy == "least_load":
            return min(alive, key=lambda w: (w.load, w.last_heartbeat))
        if role not in self._rr:
            self._rr[role] = itertools.cycle(alive)
            return next(self._rr[role])
        # 轮询但跳过已失联的
        for _ in range(len(alive)):
            cand = next(self._rr[role])
            if cand in alive:
                return cand
        return alive[0]
