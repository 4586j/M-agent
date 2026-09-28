"""失败重试 + Worker 自愈。后台 sweeper 标记 lost 任务并按指数退避重试。"""
import logging
import threading
import time
from typing import Callable

from dispatcher import Dispatcher
from event_bus import TaskEventBus, publish_task_event
from worker_registry import WorkerRegistry

from task_store import TaskRecord, TaskStore

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
        event_bus: TaskEventBus | None = None,
        provider_resolver: Callable[[str], dict | None] | None = None,
    ):
        self.task_store = task_store
        self.registry = registry
        self.dispatcher = dispatcher
        self.sweeper_interval = sweeper_interval
        self.retry_base_delay = retry_base_delay
        self.event_bus = event_bus
        # provider_resolver：provider 名 → 解析后的 {name, model, base_url, auth_token}。
        # 注入 brain 的 ProviderStore.resolve，使重试能继承原任务的 provider（同名重试，
        # 密钥重新从环境解析，不落盘）。None 时按无 provider 处理（不阻断重试）。
        self.provider_resolver = provider_resolver
        self._now = now_fn or time.time
        self._ts = ts_fn or (lambda: str(int(time.time())))
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def _resume_overrides(self, rec: TaskRecord) -> dict:
        """从原任务 meta 恢复派发覆盖（model / provider 三元组）。

        顺带修复历史缺陷：此前重试派发不传 model，原任务的模型覆盖在重试时丢失，
        重试跑成默认模型。provider 同名经 resolver 重新解析（密钥不落 meta）。
        返回 dispatcher.dispatch 的 kwargs。
        """
        meta = rec.meta if isinstance(rec.meta, dict) else {}
        kwargs: dict = {}
        model = meta.get("model")
        if model:
            kwargs["model"] = model
        pname = meta.get("provider")
        if pname and self.provider_resolver:
            resolved = self.provider_resolver(str(pname))
            if resolved is not None:
                kwargs["provider"] = resolved
            else:
                log.warning("[trace=%s] retry provider '%s' 不可解析，按无 provider 重试",
                            rec.trace_id, pname)
        return kwargs

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
        if not dead:
            return 0
        dead_ids = {dw.worker_id for dw in dead}
        count = 0
        # 一次取全表扫描，命中任一 dead worker 的 running 任务即标 lost：
        # 避免对每个 dead worker 各扫一遍全表（旧实现 O(dead×N) → O(N)）。
        for rec in self.task_store.list_all():
            if rec.status != "running" or rec.worker_id not in dead_ids:
                continue
            self.task_store.update(
                rec.task_id, status="lost",
                error=f"worker {rec.worker_id} heartbeat timeout",
                updated_at=self._ts(),
            )
            log.info("[trace=%s] task=%s marked lost (worker=%s heartbeat timeout)",
                     rec.trace_id, rec.task_id, rec.worker_id)
            # lost 属终态：发布 done 事件让 SSE 订阅流关闭
            publish_task_event(self.event_bus, rec.task_id, event="done",
                               state="lost", trace_id=rec.trace_id,
                               summary=f"worker {rec.worker_id} heartbeat timeout")
            count += 1
        return count

    @staticmethod
    def _retry_context(rec: TaskRecord) -> str:
        """从 TaskRecord.meta 提取反思摘要，作为重试任务的 context（上次失败教训）。

        阶段四反思回流：worker 侧 kernel 反思产物经 callback 存入
        TaskRecord.meta["reflection"]；重试派发时注入 context_memory，
        让重试任务带着「上次为什么失败」的教训执行。缺失返回空串。
        """
        meta = rec.meta if isinstance(rec.meta, dict) else {}
        reflection = str(meta.get("reflection") or "").strip()
        if not reflection:
            return ""
        return f"[上次失败教训（自动反思摘要，供参考改进）]\n{reflection[:800]}"

    def _retry_eligible(self) -> int:
        now = self._now()
        count = 0
        for rec in self.task_store.list_retryable():
            if not self._backoff_elapsed(rec, now):
                continue
            try:
                res = self.dispatcher.dispatch(
                    role=rec.role, prompt=rec.prompt,
                    context_memory=self._retry_context(rec),
                    **self._resume_overrides(rec),
                )
                self.task_store.update(
                    rec.task_id,
                    retry_count=rec.retry_count + 1,
                    last_retry_at=self._ts(),
                    updated_at=self._ts(),
                )
                log.info("[trace=%s] retry task=%s -> new task=%s (trace=%s, attempt=%s)",
                         rec.trace_id, rec.task_id, res.task_id, res.trace_id,
                         rec.retry_count + 1)
                count += 1
            except Exception:
                log.exception("retry dispatch failed for %s (trace=%s)",
                              rec.task_id, rec.trace_id)
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
                # 修复节点一致性：新派发定点到刚恢复的 worker（仅当 role 一致，
                # 否则轮询选节点），因为下面 old 记录的 worker_id 会改写为该节点——
                # 新任务若落到别处，任务记录与事实不符。
                heal_pin = worker_id if w.role == rec.role else None
                res = self.dispatcher.dispatch(
                    role=rec.role, prompt=rec.prompt,
                    context_memory=self._retry_context(rec),
                    worker_id=heal_pin,
                    **self._resume_overrides(rec),
                )
                self.task_store.update(
                    rec.task_id, status="running",
                    worker_id=worker_id,
                    retry_count=rec.retry_count + 1,
                    last_retry_at=self._ts(),
                    updated_at=self._ts(),
                )
                log.info("[trace=%s] healing task=%s -> new task=%s (trace=%s)",
                         rec.trace_id, rec.task_id, res.task_id, res.trace_id)
                count += 1
            except Exception:
                log.exception("healing dispatch failed for %s (trace=%s)",
                              rec.task_id, rec.trace_id)
        return count
