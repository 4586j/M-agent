"""派发抽象。LocalDispatcher 保留 V1；RemoteDispatcher 走 HTTP 到 worker。"""
import time
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from task_store import TaskStore, TaskRecord
from worker_registry import WorkerRegistry

_POLL_MAX = 1000  # 轮询上限，防死循环

# 产物检验时拉取的文本类扩展名（二进制跳过，避免拉取巨大/无意义内容）
_TEXT_ARTIFACT_EXT = {".md", ".txt", ".py", ".json", ".yaml", ".yml", ".sh",
                      ".ts", ".tsx", ".js", ".jsx", ".csv", ".log", ".ini",
                      ".toml", ".env", ".xml", ".html", ".css"}


def _is_text_artifact(path: str) -> bool:
    return Path(path).suffix.lower() in _TEXT_ARTIFACT_EXT


@dataclass
class DispatchResult:
    task_id: str
    status: str
    output: Any = None
    artifacts: list = field(default_factory=list)
    error: Any = None
    verdict: dict | None = None  # security-guard 产物裁决（done 且命中规则时存在）


class Dispatcher(ABC):
    @abstractmethod
    def dispatch(self, role: str, prompt: str,
                 context_memory: str | None = None,
                 model: str | None = None) -> DispatchResult: ...

    @abstractmethod
    def redispatch(self, task_id: str, model: str | None = None) -> DispatchResult:
        """按已存在任务的 role/prompt 重新派发，返回新 DispatchResult。"""


class LocalDispatcher(Dispatcher):
    """V1 兼容：记录任务并标记 done。真实本地执行由主会话 Agent 工具完成。"""

    def __init__(self, task_store: TaskStore, now_fn: Callable[[], str] | None = None,
                 product_check: Callable[[str], dict] | None = None):
        self.task_store = task_store
        self._now = now_fn or (lambda: str(int(time.time())))
        self.product_check = product_check

    def dispatch(self, role: str, prompt: str,
                 context_memory: str | None = None, model: str | None = None) -> DispatchResult:
        task_id = f"task-{uuid.uuid4().hex[:8]}"
        ts = self._now()
        rec = TaskRecord(
            task_id=task_id, role=role, prompt=prompt, worker_id="local",
            status="done", created_at=ts, updated_at=ts,
            output="本地派发：由主会话 Agent 工具执行", artifacts=[], error=None,
        )
        if self.product_check:
            rec.verdict = self.product_check(rec.output or "")
        self.task_store.write(rec)
        return DispatchResult(task_id=task_id, status="done",
                              output=rec.output, artifacts=[], error=None,
                              verdict=rec.verdict)

    def redispatch(self, task_id: str, model: str | None = None) -> DispatchResult:
        rec = self.task_store.get(task_id)
        if rec is None:
            raise KeyError(f"task not found: {task_id}")
        return self.dispatch(role=rec.role, prompt=rec.prompt,
                             context_memory=None, model=model)


class RemoteDispatcher(Dispatcher):
    """HTTP 派发到 worker：选 worker、写 pending、POST /task、轮询 GET /task/{id}。"""

    def __init__(self, task_store: TaskStore, registry: WorkerRegistry,
                 http_call: Callable[..., dict], now_fn: Callable[[], str] | None = None,
                 select_strategy: str = "round_robin",
                 product_check: Callable[[str], dict] | None = None):
        self.task_store = task_store
        self.registry = registry
        self.http_call = http_call  # (method, url, **kwargs) -> {"status_code", "json"}
        self._now = now_fn or (lambda: str(int(time.time())))
        self.select_strategy = select_strategy
        self.product_check = product_check

    def dispatch(self, role: str, prompt: str,
                 context_memory: str | None = None, model: str | None = None) -> DispatchResult:
        worker = self.registry.select_worker(role, strategy=self.select_strategy)
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
        # 轮询：每 0.5s 拉一次，直到 done/failed 或达 _POLL_MAX 上限
        for _ in range(_POLL_MAX):
            r = self.http_call("GET", f"{base}/task/{task_id}")
            data = r.get("json", {})
            st = data.get("status")
            if st in ("done", "failed"):
                out = data.get("output")
                arts = data.get("artifacts", [])
                verdict = None
                if st == "done" and self.product_check:
                    verdict = self._check_product(base, task_id, out, arts)
                self.task_store.update(
                    task_id, status=st, output=out,
                    artifacts=arts, error=data.get("error"), verdict=verdict,
                )
                return DispatchResult(
                    task_id, st, out, arts, data.get("error"), verdict,
                )
            time.sleep(0.5)
        self.task_store.update(task_id, status="failed", error="poll timeout")
        return DispatchResult(task_id, "failed", error="poll timeout")

    def _check_product(self, base: str, task_id: str, output: Any,
                       artifacts: list) -> dict | None:
        """合并 output + 文本 artifacts 内容过产物检验；二进制产物跳过。"""
        combined = output or ""
        for art in artifacts or []:
            if not _is_text_artifact(art):
                continue
            try:
                ar = self.http_call("GET", f"{base}/artifact/{task_id}", params={"path": art})
                if ar.get("status_code") == 200:
                    combined += f"\n\n--- artifact: {art} ---\n" + ar.get("json", {}).get("content", "")
            except Exception:
                continue
        return self.product_check(combined)

    def redispatch(self, task_id: str, model: str | None = None) -> DispatchResult:
        rec = self.task_store.get(task_id)
        if rec is None:
            raise KeyError(f"task not found: {task_id}")
        return self.dispatch(role=rec.role, prompt=rec.prompt,
                             context_memory=None, model=model)
