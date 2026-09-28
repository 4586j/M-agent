"""worker 注册表：注册、心跳保活、按角色轮询/按负载选择。"""
import itertools
import threading
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
        # 注册/心跳/派发来自不同线程（FastAPI 线程池 + 后台 sweeper），用 RLock
        # 因为 _alive 等内部方法会被持锁方法嵌套调用
        self._lock = threading.RLock()

    def register(self, worker: WorkerInfo) -> None:
        with self._lock:
            worker.last_heartbeat = self._now()
            self._workers[worker.worker_id] = worker
            self._rr.pop(worker.role, None)  # 重建轮询器

    def unregister(self, worker_id: str) -> bool:
        """移除 worker；返回是否确实移除了一个。"""
        with self._lock:
            w = self._workers.pop(worker_id, None)
            if w is not None:
                self._rr.pop(w.role, None)
            return w is not None

    def heartbeat(self, worker_id: str, load: int = 0) -> None:
        with self._lock:
            w = self._workers.get(worker_id)
            if w is None:
                return
            w.last_heartbeat = self._now()
            w.load = load

    def _alive(self) -> list[WorkerInfo]:
        now = self._now()
        with self._lock:
            return [w for w in self._workers.values() if now - w.last_heartbeat <= self.heartbeat_timeout]

    def active_workers(self) -> list[WorkerInfo]:
        return self._alive()

    def all_workers(self) -> list[WorkerInfo]:
        """全部在册 worker（含已超时未剔除的），供产物代理等查询。"""
        with self._lock:
            return list(self._workers.values())

    def dead_workers(self) -> list[WorkerInfo]:
        """返回心跳超时的 worker（在册但不在活跃列表中）。"""
        with self._lock:
            alive_ids = {w.worker_id for w in self._alive()}
            return [w for w in self._workers.values() if w.worker_id not in alive_ids]

    def get_worker(self, worker_id: str) -> WorkerInfo | None:
        """按 ID 查询 worker（不论是否存活）。"""
        with self._lock:
            return self._workers.get(worker_id)

    def select_worker(self, role: str = "executor",
                      strategy: str = "round_robin") -> WorkerInfo | None:
        """选择 worker。strategy: round_robin（默认，兼容）| least_load（按 load 升序）。"""
        alive = [w for w in self._alive() if w.role == role]
        if not alive:
            return None
        if strategy == "least_load":
            return min(alive, key=lambda w: (w.load, w.last_heartbeat))
        with self._lock:
            if role not in self._rr:
                self._rr[role] = itertools.cycle(alive)
                return next(self._rr[role])
            # 轮询但跳过已失联的
            for _ in range(len(alive)):
                cand = next(self._rr[role])
                if cand in alive:
                    return cand
        return alive[0]

    def resolve_worker(self, role: str = "executor",
                       strategy: str = "round_robin",
                       worker_id: str | None = None) -> WorkerInfo:
        """定点解析：worker_id 非空时按 ID 定点，否则按 role+strategy 选择。

        定点失败（从未注册 / 注册过但心跳超时）抛 RuntimeError，不静默回退。
        两种错误消息刻意区分——「从没注册」和「注册过但掉线」是运维要排查的
        两件事。调用方（dispatcher）在 task_store.write 之前调用，保证失败时
        零任务落库。
        """
        if worker_id is not None:
            w = self.get_worker(worker_id)
            if w is None:
                raise RuntimeError(f"节点从未注册: {worker_id}")
            alive = self._alive()
            alive_ids = {x.worker_id for x in alive}
            if worker_id not in alive_ids:
                detail = ", ".join(
                    f"{x.worker_id}({x.role}@{x.host}:{x.port})" for x in alive)
                raise RuntimeError(
                    f"节点心跳超时: {worker_id}；当前活跃节点: [{detail or '无'}]")
            return w
        w = self.select_worker(role, strategy=strategy)
        if w is None:
            raise RuntimeError(f"无可用 {role} worker")
        return w
