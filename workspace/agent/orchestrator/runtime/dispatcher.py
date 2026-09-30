"""派发抽象。RemoteDispatcher 走 HTTP 到 worker（首选）；LocalDispatcher 为 V1 兼容。"""
import logging
import time
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Callable

from dispatch_common import (is_text_artifact, enrich_experience,
                             write_result_note, put_provider_name, provider_body)
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
                 enable_subagents: bool | None = None,
                 worker_id: str | None = None,
                 provider: dict | None = None) -> DispatchResult:
        """worker_id 非空时定点派发到该节点（失败抛 RuntimeError，不静默回退）。

        provider：解析后的 {name, model, base_url, auth_token} 或 None。派发层
        只把 name 进 TaskRecord.meta（不含密钥）、把三元组放进瞬时 worker body。
        """

    @abstractmethod
    def redispatch(self, task_id: str, model: str | None = None,
                   worker_id: str | None = None,
                   provider: dict | None = None) -> DispatchResult:
        """按已存在任务的 role/prompt 重新派发，返回新 DispatchResult。"""


class LocalDispatcher(Dispatcher):
    """[deprecated] V1 兼容：记录任务并标记 done。

    仅主会话 Agent 工具直连的历史约定保留（tests/test_dispatcher 覆盖其
    本地派发语义）；生产装配一律走 RemoteDispatcher / LocalExecutorDispatcher。
    本地真实执行请用 LocalExecutorDispatcher（进程内跑 AgentKernel），不要新接本类。
    """

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
                 enable_subagents: bool | None = None,
                 worker_id: str | None = None,
                 provider: dict | None = None) -> DispatchResult:
        # V1 本地派发无 worker 概念：只能接受伪定点 "local"。
        if worker_id is not None and worker_id != "local":
            raise RuntimeError(
                f"local 派发不支持定点节点 {worker_id}（本地执行没有独立 worker）")
        task_id = f"task-{uuid.uuid4().hex[:8]}"
        trace_id = new_trace_id()
        ts = self._now()
        meta: dict = {}
        if model:
            meta["model"] = model
        put_provider_name(meta, provider)
        rec = TaskRecord(
            task_id=task_id, role=role, prompt=prompt, worker_id="local",
            status="done", created_at=ts, updated_at=ts,
            output="本地派发：由主会话 Agent 工具执行", artifacts=[], error=None,
            trace_id=trace_id, meta=meta,
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

    def redispatch(self, task_id: str, model: str | None = None,
                   worker_id: str | None = None,
                   provider: dict | None = None) -> DispatchResult:
        rec = self.task_store.get(task_id)
        if rec is None:
            raise KeyError(f"task not found: {task_id}")
        return self.dispatch(role=rec.role, prompt=rec.prompt,
                             context_memory=None, model=model,
                             worker_id=worker_id, provider=provider)


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
                 event_bus: TaskEventBus | None = None,
                 on_spend: Callable[[float], None] | None = None,
                 breaker_board=None):
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
        self.on_spend = on_spend  # 预算记账钩子：任务终态时上报 cost_usd（可 None）
        self.breaker_board = breaker_board  # per-worker 熔断板（可 None=不启用）

    def _resolve_with_breaker(self, role: str, worker_id: str | None):
        """节点解析 + 熔断规避。

        定点派发：节点熔断中直接报错（明确指定不给换，由人工处置）。
        自动选节点：最多尝试 4 个不同节点，跳过熔断中的；全部熔断则报错。
        """
        if worker_id is not None:
            w = self.registry.resolve_worker(role, strategy=self.select_strategy,
                                             worker_id=worker_id)
            if self.breaker_board is not None:
                allowed, retry_after = self.breaker_board.allow(w.worker_id)
                if not allowed:
                    raise RuntimeError(
                        f"节点 {w.worker_id} 熔断中（约 {retry_after:.0f}s 后自动试探恢复）；"
                        "如确认节点正常，可重启 brain 或设 ORCH_CIRCUIT_BREAKER_ENABLED=0")
            return w
        tried: set[str] = set()
        for _ in range(4):
            try:
                w = self.registry.resolve_worker(role, strategy=self.select_strategy,
                                                 exclude=tried)
            except RuntimeError as e:
                if tried:  # 剩余候选全在熔断中，消息要能指导运维
                    raise RuntimeError(
                        f"无可用 {role} worker（未重试节点均在熔断中: {sorted(tried)}）") from e
                raise
            if self.breaker_board is None:
                return w
            allowed, _retry = self.breaker_board.allow(w.worker_id)
            if allowed:
                return w
            tried.add(w.worker_id)
            log.warning("节点熔断中，换节点重试 worker=%s tried=%s", w.worker_id, sorted(tried))
        raise RuntimeError(
            f"无可用 {role} worker（连续 {len(tried)} 个候选节点均在熔断中: {sorted(tried)}）")

    def _record_breaker(self, worker_id: str | None, ok: bool) -> None:
        """节点健康记账：done → success，failed/派发失败 → failure。"""
        if self.breaker_board is None or not worker_id:
            return
        if ok:
            self.breaker_board.record_success(worker_id)
        else:
            self.breaker_board.record_failure(worker_id)

    def _report_spend(self, meta: dict) -> None:
        """把回传 meta 里的 cost_usd 上报预算账本；失败只记日志不阻断收尾。"""
        if self.on_spend is None or not isinstance(meta, dict):
            return
        cost = meta.get("cost_usd")
        if not isinstance(cost, (int, float)) or isinstance(cost, bool) or cost <= 0:
            return
        try:
            self.on_spend(float(cost))
        except Exception:  # noqa: BLE001 - 记账失败不影响任务结果
            log.warning("预算记账失败 cost=%s: %r", cost, exc_info=True)

    def dispatch(self, role: str, prompt: str,
                 context_memory: str | None = None, model: str | None = None,
                 use_experience: bool = True,
                 enable_subagents: bool | None = None,
                 worker_id: str | None = None,
                 provider: dict | None = None) -> DispatchResult:
        # enable_subagents：远程模式下每次请求覆盖暂不转发到 worker，
        # worker 侧统一受全局 AGENT_/ORCH_SUBAGENTS_ENABLED 闸（kernel._build_options）控制。
        # worker_id 定点：resolve_worker 会抛 RuntimeError（从未注册/心跳超时），
        # 在落库之前，保证失败零任务产生。熔断中的节点会被跳过（定点则报错）。
        worker = self._resolve_with_breaker(role, worker_id)
        # 定点时以节点的真实 role 为准（覆盖请求 role）：下游经验库/成本聚合/
        # _route_model 按 TaskRecord.role 消费，写错会污染数据。
        pinned = worker_id is not None
        if pinned:
            role = worker.role
        task_id = f"task-{uuid.uuid4().hex[:8]}"
        trace_id = new_trace_id()
        ts = self._now()
        meta = {"model": model} if model else {}
        meta["use_experience"] = bool(use_experience)  # 与 async/local 统一口径
        if pinned:
            meta["pinned"] = True
        # 只记 provider 名进 meta（密钥绝不落盘）；三元组走瞬时 body
        put_provider_name(meta, provider)
        self.task_store.write(TaskRecord(
            task_id=task_id, role=role, prompt=prompt, worker_id=worker.worker_id,
            status="pending", created_at=ts, updated_at=ts,
            output=None, artifacts=[], error=None,
            max_retries=self.max_retries,
            trace_id=trace_id, meta=meta,
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
        # provider 三元组瞬时转发 worker（内存仅本次任务；不落任何持久化）
        body.update(provider_body(provider))
        resp = self.http_call("POST", f"{base}/task", json=body)
        if resp.get("status_code") != 200:
            log.warning("[trace=%s] task=%s post failed: %s", trace_id, task_id, resp)
            self._record_breaker(worker.worker_id, ok=False)
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
                # brain 侧已记录路由 model/callback 同名字段以 worker 为准；定点标记与
                # use_experience 须保留（redispatch 继承 / 消融状态口径靠它判别）
                meta = {"model": model} if model else {}
                meta["use_experience"] = bool(use_experience)
                if pinned:
                    meta["pinned"] = True
                put_provider_name(meta, provider)
                meta = {**meta, **(data.get("meta") or {})}
                self.task_store.update(
                    task_id, status=st, output=out,
                    artifacts=arts, error=data.get("error"), verdict=verdict,
                    meta=meta,
                )
                self._report_spend(meta)
                # 节点健康记账：failed 计入连击（含任务级失败——保守口径），
                # done 重置；重试链路的最终成败经此自然反馈给熔断器
                self._record_breaker(worker.worker_id, ok=(st == "done"))
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

    def redispatch(self, task_id: str, model: str | None = None,
                   worker_id: str | None = None,
                   provider: dict | None = None) -> DispatchResult:
        """重派：显式 worker_id 优先；原任务若定点派发则沿用原节点。"""
        rec = self.task_store.get(task_id)
        if rec is None:
            raise KeyError(f"task not found: {task_id}")
        # 定点继承：显式 worker_id 优先；否则原任务是定点派发（meta.pinned）
        # 则沿用原节点，保持「重派 = 同一节点」语义。轮询派发的任务仍轮询。
        if worker_id is None and rec.meta.get("pinned"):
            worker_id = rec.worker_id
        return self.dispatch(role=rec.role, prompt=rec.prompt,
                             context_memory=None, model=model,
                             worker_id=worker_id, provider=provider)
