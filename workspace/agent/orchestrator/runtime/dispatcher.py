"""派发抽象。LocalDispatcher 保留 V1；RemoteDispatcher 走 HTTP 到 worker。"""
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Callable

from task_store import TaskStore, TaskRecord
from worker_registry import WorkerRegistry

_POLL_MAX = 1000  # 轮询上限，防死循环


@dataclass
class DispatchResult:
    task_id: str
    status: str
    output: Any = None
    artifacts: list = field(default_factory=list)
    error: Any = None


class Dispatcher(ABC):
    @abstractmethod
    def dispatch(self, role: str, prompt: str,
                 context_memory: str | None = None,
                 model: str | None = None) -> DispatchResult: ...


class LocalDispatcher(Dispatcher):
    """V1 兼容：记录任务并标记 done。真实本地执行由主会话 Agent 工具完成。"""

    def __init__(self, task_store: TaskStore, now_fn: Callable[[], str] | None = None):
        self.task_store = task_store
        self._now = now_fn or (lambda: "t0")

    def dispatch(self, role: str, prompt: str,
                 context_memory: str | None = None, model: str | None = None) -> DispatchResult:
        task_id = f"task-{uuid.uuid4().hex[:8]}"
        ts = self._now()
        rec = TaskRecord(
            task_id=task_id, role=role, prompt=prompt, worker_id="local",
            status="done", created_at=ts, updated_at=ts,
            output="本地派发：由主会话 Agent 工具执行", artifacts=[], error=None,
        )
        self.task_store.write(rec)
        return DispatchResult(task_id=task_id, status="done",
                              output=rec.output, artifacts=[], error=None)


class RemoteDispatcher(Dispatcher):
    """HTTP 派发到 worker：选 worker、写 pending、POST /task、轮询 GET /task/{id}。"""

    def __init__(self, task_store: TaskStore, registry: WorkerRegistry,
                 http_call: Callable[..., dict], now_fn: Callable[[], str] | None = None):
        self.task_store = task_store
        self.registry = registry
        self.http_call = http_call  # (method, url, **kwargs) -> {"status_code", "json"}
        self._now = now_fn or (lambda: "t0")

    def dispatch(self, role: str, prompt: str,
                 context_memory: str | None = None, model: str | None = None) -> DispatchResult:
        worker = self.registry.select_worker(role)
        if worker is None:
            raise RuntimeError(f"无可用 {role} worker")
        task_id = f"task-{uuid.uuid4().hex[:8]}"
        ts = self._now()
        self.task_store.write(TaskRecord(
            task_id=task_id, role=role, prompt=prompt, worker_id=worker.worker_id,
            status="pending", created_at=ts, updated_at=ts,
            output=None, artifacts=[], error=None,
        ))
        base = f"http://{worker.host}:{worker.port}"
        body = {"task_id": task_id, "prompt": prompt,
                "context_memory": context_memory or ""}
        if model:
            body["model"] = model
        resp = self.http_call("POST", f"{base}/task", json=body)
        if resp.get("status_code") != 200:
            self.task_store.update(task_id, status="failed", error=f"post failed: {resp}")
            return DispatchResult(task_id, "failed", error=f"post failed: {resp}")
        self.task_store.update(task_id, status="running")
        # 轮询
        for _ in range(_POLL_MAX):
            r = self.http_call("GET", f"{base}/task/{task_id}")
            data = r.get("json", {})
            st = data.get("status")
            if st in ("done", "failed"):
                self.task_store.update(
                    task_id, status=st, output=data.get("output"),
                    artifacts=data.get("artifacts", []), error=data.get("error"),
                )
                return DispatchResult(
                    task_id, st, data.get("output"),
                    data.get("artifacts", []), data.get("error"),
                )
        self.task_store.update(task_id, status="failed", error="poll timeout")
        return DispatchResult(task_id, "failed", error="poll timeout")
