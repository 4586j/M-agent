"""派发抽象。LocalDispatcher 保留 V1；RemoteDispatcher 走 HTTP 到 worker。"""
import logging
import time
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Callable

from dispatch_common import is_text_artifact, enrich_experience, write_result_note
from event_bus import TaskEventBus, publish_task_event
from task_store import TaskStore, TaskRecord, new_trace_id
from worker_registry import WorkerRegistry
from experience_store import ExperienceStore
from memory_store import MemoryStore
from audit_log import record_verdict_event

_POLL_MAX = 1000  # 轮询上限，防死循环

log = logging.getLogger(__name__)


@dataclass
class DispatchResult:
    task_id: str
    status: str
    output: Any = None
    artifacts: list = field(default_factory=list)
    error: Any = None
    verdict: dict | None = None  # security-guard 产物裁决（done 且命中规则时存在）
    trace_id: str | None = None  # 本次派发生成的链路追踪 ID


class Dispatcher(ABC):
    @abstractmethod
    def dispatch(self, role: str, prompt: str,
                 context_memory: str | None = None,
                 model: str | None = None,
                 use_experience: bool = True,
                 enable_subagents: bool | None = None) -> DispatchResult: ...

    @abstractmethod
    def redispatch(self, task_id: str, model: str | None = None) -> DispatchResult:
        """按已存在任务的 role/prompt 重新派发，返回新 DispatchResult。"""


class LocalDispatcher(Dispatcher):
    """V1 兼容：记录任务并标记 done。真实本地执行由主会话 Agent 工具完成。"""

    def __init__(self, task_store: TaskStore, now_fn: Callable[[], str] | None = None,
                 product_check: Callable[[str], dict] | None = None,
                 event_bus: TaskEventBus | None = None):
        self.task_store = task_store
        self._now = now_fn or (lambda: str(int(time.time())))
        self.product_check = product_check
        self.event_bus = event_bus

    def dispatch(self, role: str, prompt: str,
                 context_memory: str | None = None, model: str | None = None,
                 use_experience: bool = True,
                 enable_subagents: bool | None = None) -> DispatchResult:
        task_id = f"task-{uuid.uuid4().hex[:8]}"
        trace_id = new_trace_id()
        ts = self._now()
        rec = TaskRecord(
            task_id=task_id, role=role, prompt=prompt, worker_id="local",
            status="done", created_at=ts, updated_at=ts,
            output="本地派发：由主会话 Agent 工具执行", artifacts=[], error=None,
            trace_id=trace_id, meta={"model": model} if model else {},
        )
        if self.product_check:
            rec.verdict = self.product_check(rec.output or "")
            # 产物后置关卡审计（写入失败不阻断）
            record_verdict_event(task_id, rec.verdict)
        self.task_store.write(rec)
        log.info("[trace=%s] local dispatch role=%s task=%s", trace_id, role, task_id)
        publish_task_event(self.event_bus, task_id, event="done", state="done",
                           trace_id=trace_id, summary=rec.output)
        return DispatchResult(task_id=task_id, status="done",
                              output=rec.output, artifacts=[], error=None,
                              verdict=rec.verdict, trace_id=trace_id)

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
                 product_check: Callable[[str], dict] | None = None,
                 experience_store: ExperienceStore | None = None,
                 memory_store: MemoryStore | None = None,
                 max_retries: int = 0,
                 check_max_chars: int = 6000,
                 event_bus: TaskEventBus | None = None):
        self.task_store = task_store
        self.registry = registry
        self.http_call = http_call  # (method, url, **kwargs) -> {"status_code", "json"}
        self._now = now_fn or (lambda: str(int(time.time())))
        self.select_strategy = select_strategy
        self.product_check = product_check
        self.experience_store = experience_store
        self.memory_store = memory_store
        self.max_retries = max_retries
        self.check_max_chars = check_max_chars
        self.event_bus = event_bus

    def dispatch(self, role: str, prompt: str,
                 context_memory: str | None = None, model: str | None = None,
                 use_experience: bool = True,
                 enable_subagents: bool | None = None) -> DispatchResult:
        # enable_subagents：远程模式下每次请求覆盖暂不转发到 worker，
        # worker 侧统一受全局 AGENT_/ORCH_SUBAGENTS_ENABLED 闸（kernel._build_options）控制。
        worker = self.registry.select_worker(role, strategy=self.select_strategy)
        if worker is None:
            raise RuntimeError(f"无可用 {role} worker")
        task_id = f"task-{uuid.uuid4().hex[:8]}"
        trace_id = new_trace_id()
        ts = self._now()
        self.task_store.write(TaskRecord(
            task_id=task_id, role=role, prompt=prompt, worker_id=worker.worker_id,
            status="pending", created_at=ts, updated_at=ts,
            output=None, artifacts=[], error=None,
            max_retries=self.max_retries,
            trace_id=trace_id, meta={"model": model} if model else {},
        ))
        log.info("[trace=%s] dispatch role=%s -> worker=%s task=%s",
                 trace_id, role, worker.worker_id, task_id)
        publish_task_event(self.event_bus, task_id, event="status",
                           state="pending", trace_id=trace_id)
        base = f"http://{worker.host}:{worker.port}"
        # 消融硬关：use_experience=False 时不检索/不注入历史经验（读关）。
        enriched_ctx = (self._enrich_with_experience(prompt, role, context_memory)
                        if use_experience else (context_memory or ""))
        body = {"task_id": task_id, "prompt": prompt,
                "context_memory": enriched_ctx or "",
                "trace_id": trace_id}
        if model:
            body["model"] = model
        resp = self.http_call("POST", f"{base}/task", json=body)
        if resp.get("status_code") != 200:
            log.warning("[trace=%s] task=%s post failed: %s", trace_id, task_id, resp)
            self.task_store.update(task_id, status="failed", error=f"post failed: {resp}")
            publish_task_event(self.event_bus, task_id, event="done",
                               state="failed", trace_id=trace_id,
                               summary=f"post failed: {resp}")
            return DispatchResult(task_id, "failed", error=f"post failed: {resp}",
                                  trace_id=trace_id)
        self.task_store.update(task_id, status="running")
        publish_task_event(self.event_bus, task_id, event="status",
                           state="running", trace_id=trace_id)
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
                    # 产物后置关卡审计（写入失败不阻断）
                    record_verdict_event(task_id, verdict)
                # worker 回传 meta（usage/cost_usd/num_turns）并入 TaskRecord；
                # brain 侧已记录路由 model，callback 同名字段以 worker 为准
                meta = {**({"model": model} if model else {}),
                        **(data.get("meta") or {})}
                self.task_store.update(
                    task_id, status=st, output=out,
                    artifacts=arts, error=data.get("error"), verdict=verdict,
                    meta=meta,
                )
                log.info("[trace=%s] task=%s terminal status=%s", trace_id, task_id, st)
                publish_task_event(self.event_bus, task_id, event="done", state=st,
                                   trace_id=trace_id,
                                   summary=out if out is not None else data.get("error"))
                if self.experience_store and use_experience:
                    self.experience_store.record_from_task(
                        task_id=task_id, role=role, prompt=prompt,
                        output=out, success=(st == "done"),
                        verdict=verdict, artifacts=arts, created_at=ts,
                    )
                write_result_note(self.memory_store, task_id, st, out,
                                  data.get("error"))
                return DispatchResult(
                    task_id, st, out, arts, data.get("error"), verdict, trace_id,
                )
            time.sleep(0.5)
        log.warning("[trace=%s] task=%s poll timeout", trace_id, task_id)
        self.task_store.update(task_id, status="failed", error="poll timeout")
        publish_task_event(self.event_bus, task_id, event="done", state="failed",
                           trace_id=trace_id, summary="poll timeout")
        return DispatchResult(task_id, "failed", error="poll timeout", trace_id=trace_id)

    def _check_product(self, base: str, task_id: str, output: Any,
                       artifacts: list) -> dict | None:
        """合并 output + 文本 artifacts 内容过产物检验；二进制产物跳过。

        合并输入截断到 check_max_chars：LLM 增强开启时这是直接烧钱点，
        全量产物内容会让检验成本随产物大小线性膨胀。
        """
        combined = output or ""
        for art in artifacts or []:
            if not is_text_artifact(art):
                continue
            try:
                ar = self.http_call("GET", f"{base}/artifact/{task_id}", params={"path": art})
                if ar.get("status_code") == 200:
                    combined += f"\n\n--- artifact: {art} ---\n" + ar.get("json", {}).get("content", "")
            except Exception:
                continue
        return self.product_check(combined[:self.check_max_chars])

    def _enrich_with_experience(self, prompt: str, role: str,
                                context_memory: str | None) -> str:
        return enrich_experience(self.experience_store, prompt, role, context_memory)

    def redispatch(self, task_id: str, model: str | None = None) -> DispatchResult:
        rec = self.task_store.get(task_id)
        if rec is None:
            raise KeyError(f"task not found: {task_id}")
        return self.dispatch(role=rec.role, prompt=rec.prompt,
                             context_memory=None, model=model)
