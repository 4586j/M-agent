"""异步派发器：asyncio + httpx，立即返回 task_id，后台轮询或回调写入结果。"""
import asyncio
import logging
import os
import time
import uuid
from typing import Any, Awaitable, Callable

from task_store import TaskStore, TaskRecord, new_trace_id
from worker_registry import WorkerRegistry
from experience_store import ExperienceStore
from memory_store import MemoryStore
from dispatch_common import (is_text_artifact, enrich_experience,
                             write_result_note, put_provider_name, provider_body)
from audit_log import record_verdict_event
from event_bus import TaskEventBus, TERMINAL_STATUSES, publish_task_event

log = logging.getLogger(__name__)

AsyncHttpCall = Callable[..., Awaitable[dict]]

# 不允许被迟到 callback / 轮询终态化改写的状态：终态之外，awaiting_approval
# （人工裁决挂起）与 dispatching（approve 派发占位）也不可被改写为 done/failed，
# 否则迟到回调会复活已取消/裁决中的任务并向 SSE 广播矛盾事件。
_NON_FINALIZABLE = tuple(TERMINAL_STATUSES) + ("awaiting_approval", "dispatching")


class AsyncRemoteDispatcher:
    """异步派发：dispatch_async 立即返回 task_id，后台 asyncio.Task 轮询 worker。

    双保险：Worker 完成后可主动 POST 回调（/tasks/{id}/callback），
    后台轮询仍在运行但会在 finalize 时检查状态避免覆盖。
    """

    def __init__(
        self,
        task_store: TaskStore,
        registry: WorkerRegistry,
        async_http: AsyncHttpCall,
        now_fn: Callable[[], str] | None = None,
        select_strategy: str = "round_robin",
        product_check: Callable[[str], dict] | None = None,
        experience_store: ExperienceStore | None = None,
        memory_store: MemoryStore | None = None,
        max_retries: int = 0,
        check_max_chars: int = 6000,
        poll_interval: float = 1.0,
        poll_max: int = 600,
        brain_url: str = "http://127.0.0.1:7000",
        event_bus: TaskEventBus | None = None,
    ):
        self.task_store = task_store
        self.registry = registry
        self.async_http = async_http
        self._now = now_fn or (lambda: str(int(time.time())))
        self.select_strategy = select_strategy
        self.product_check = product_check
        self.experience_store = experience_store
        self.memory_store = memory_store
        self.max_retries = max_retries
        self.check_max_chars = check_max_chars
        self.poll_interval = poll_interval
        self.poll_max = poll_max
        self.brain_url = brain_url
        self.event_bus = event_bus
        self._tasks: dict[str, asyncio.Task] = {}
        # 后台轮询并发限流：避免海量异步任务同时轮询打满连接/线程资源
        max_concurrent = int(os.environ.get("ORCH_ASYNC_MAX_CONCURRENT", "20"))
        self._semaphore = asyncio.Semaphore(max_concurrent)

    async def dispatch_async(
        self, role: str, prompt: str,
        context_memory: str | None = None,
        model: str | None = None,
        use_experience: bool = True,
        enable_subagents: bool | None = None,
        worker_id: str | None = None,
        provider: dict | None = None,
    ) -> str:
        """异步派发：写 pending、POST worker、起后台轮询，立即返回 task_id。

        use_experience=False 时硬消融经验读写（Slice 4）：
        - 读关：不检索/不注入历史经验，context_memory 原样透传；
        - 写关：标记入 meta，_finalize 轮询/回调两条终态化路径据此跳过
          experience_store.record_from_task，与同步 RemoteDispatcher 对齐。
        enable_subagents：远程异步模式下每次请求覆盖暂不转发到 worker，
        worker 侧统一受全局 AGENT_/ORCH_SUBAGENTS_ENABLED 闸（kernel._build_options）控制。
        worker_id 非空时定点派发到该节点（resolve_worker 抛错则零任务落库）。
        """
        worker = self.registry.resolve_worker(
            role, strategy=self.select_strategy, worker_id=worker_id)
        # 定点时以节点真实 role 为准（同同步 RemoteDispatcher，保下游数据真实）
        pinned = worker_id is not None
        if pinned:
            role = worker.role
        task_id = f"task-{uuid.uuid4().hex[:8]}"
        trace_id = new_trace_id()
        ts = self._now()
        meta = {**({"model": model} if model else {}),
                "use_experience": bool(use_experience)}
        if pinned:
            meta["pinned"] = True
        # 只记 provider 名进 meta（密钥绝不落盘）；三元组走瞬时 body
        put_provider_name(meta, provider)
        self.task_store.write(TaskRecord(
            task_id=task_id, role=role, prompt=prompt,
            worker_id=worker.worker_id, status="pending",
            created_at=ts, updated_at=ts,
            max_retries=self.max_retries,
            trace_id=trace_id,
            meta=meta,
        ))
        log.info("[trace=%s] async dispatch role=%s -> worker=%s task=%s",
                 trace_id, role, worker.worker_id, task_id)
        publish_task_event(self.event_bus, task_id, event="status",
                           state="pending", trace_id=trace_id)
        base = f"http://{worker.host}:{worker.port}"
        enriched = (enrich_experience(self.experience_store, prompt, role, context_memory)
                    if use_experience else (context_memory or ""))
        body: dict[str, Any] = {
            "task_id": task_id, "prompt": prompt,
            "context_memory": enriched or "",
            "callback_url": f"{self.brain_url}/tasks/{task_id}/callback",
            "trace_id": trace_id,
        }
        if model:
            body["model"] = model
        # provider 三元组瞬时转发 worker（内存仅本次任务；不落任何持久化）
        body.update(provider_body(provider))
        resp = await self.async_http("POST", f"{base}/task", json=body)
        if resp.get("status_code") != 200:
            log.warning("[trace=%s] task=%s post failed: %s", trace_id, task_id, resp)
            self.task_store.update(task_id, status="failed",
                                   error=f"post failed: {resp}")
            publish_task_event(self.event_bus, task_id, event="done",
                               state="failed", trace_id=trace_id,
                               summary=f"post failed: {resp}")
            return task_id
        self.task_store.update(task_id, status="running")
        publish_task_event(self.event_bus, task_id, event="status",
                           state="running", trace_id=trace_id)
        poll_task = asyncio.create_task(
            self._limited_poll(task_id, base, worker.worker_id, role, prompt, ts),
        )
        self._tasks[task_id] = poll_task
        return task_id

    async def handle_callback(self, task_id: str, data: dict) -> bool:
        """Worker 主动回调任务结果；已终结/裁决中/派发占位中的任务一律拒绝，返回是否确实处理了。"""
        rec = self.task_store.get(task_id)
        if rec is None or rec.status in _NON_FINALIZABLE:
            # 迟到/矛盾回调：不改写状态、不广播事件，记日志供排查
            if rec is not None:
                log.warning(
                    "[trace=%s] task=%s 迟到 callback 被拒绝（status=%s, callback_status=%s）",
                    rec.trace_id, task_id, rec.status, data.get("status"))
            return False
        log.info("[trace=%s] callback received for task=%s status=%s",
                 rec.trace_id, task_id, data.get("status"))
        # callback 到达先广播（含结果摘要，SSE payload 截断 2000 字符），再终态化
        publish_task_event(self.event_bus, task_id, event="callback",
                           state=rec.status, trace_id=rec.trace_id,
                           summary=data.get("output"), status=data.get("status"))
        await self._finalize(task_id, rec.worker_id, rec.role, rec.prompt,
                             rec.created_at, data)
        return True

    async def _limited_poll(self, task_id: str, base: str, worker_id: str,
                            role: str, prompt: str, ts: str) -> None:
        """在并发信号量限制下轮询 worker，控制同时运行的后台轮询数量。"""
        async with self._semaphore:
            await self._poll_worker(task_id, base, worker_id, role, prompt, ts)

    async def _poll_worker(self, task_id: str, base: str, worker_id: str,
                           role: str, prompt: str, ts: str) -> None:
        """后台轮询 worker 任务状态，done/failed 时写入结果。"""
        for i in range(self.poll_max):
            await asyncio.sleep(self.poll_interval)
            rec = self.task_store.get(task_id)
            if rec is None or rec.status in _NON_FINALIZABLE:
                return
            if i % 10 == 0:
                # 轮询进展心跳（每 10 轮约 10s 一次，避免刷屏）；无订阅者时零开销
                publish_task_event(self.event_bus, task_id, event="status",
                                   state="running", trace_id=rec.trace_id,
                                   message=f"polling {i}")
            try:
                r = await self.async_http("GET", f"{base}/task/{task_id}")
            except Exception:
                log.exception("async poll http error for %s (trace=%s)",
                              task_id, rec.trace_id)
                continue
            data = r.get("json", {})
            st = data.get("status")
            if st in ("done", "failed"):
                await self._finalize(task_id, worker_id, role, prompt, ts, data)
                return
        log.warning("[trace=%s] task=%s async poll timeout",
                    rec.trace_id if rec else None, task_id)
        if rec is None or rec.status in _NON_FINALIZABLE:
            return  # 已被其他路径终态化/裁决中：不覆盖为 failed
        self.task_store.update(task_id, status="failed", error="async poll timeout")
        publish_task_event(self.event_bus, task_id, event="done", state="failed",
                           trace_id=rec.trace_id if rec else None,
                           summary="async poll timeout")

    async def _finalize(self, task_id: str, worker_id: str, role: str,
                        prompt: str, ts: str, data: dict) -> None:
        """写入最终结果（产物检验 + 经验记录 + meta 入库）。

        幂等：终态/awaiting_approval/dispatching 状态一律拒绝改写。
        """
        rec = self.task_store.get(task_id)
        if rec is None or rec.status in _NON_FINALIZABLE:
            if rec is not None:
                log.warning(
                    "[trace=%s] task=%s 终态化被拒绝（status=%s，不覆盖）",
                    rec.trace_id, task_id, rec.status)
            return
        st = data.get("status", "failed")
        out = data.get("output")
        arts = data.get("artifacts", [])
        err = data.get("error")
        verdict = None
        if st == "done" and self.product_check:
            base = self._worker_base(worker_id)
            verdict = await _check_product_async(
                base, task_id, out, arts,
                self.product_check, self.async_http,
                max_chars=self.check_max_chars,
            )
            # 产物后置关卡审计（写入失败不阻断）
            record_verdict_event(task_id, verdict)
        # worker 回传 meta（usage/cost_usd/num_turns）并入 TaskRecord（brain 侧
        # 已记录路由 model，callback 同名字段以 worker 为准），供 /tasks 与 /stats 透出
        meta = {**(rec.meta or {}), **(data.get("meta") or {})}
        self.task_store.update(
            task_id, status=st, output=out,
            artifacts=arts, error=err, verdict=verdict, meta=meta,
        )
        log.info("[trace=%s] task=%s terminal status=%s", rec.trace_id, task_id, st)
        publish_task_event(self.event_bus, task_id, event="done", state=st,
                           trace_id=rec.trace_id,
                           summary=out if out is not None else err)
        if self.experience_store and (rec.meta or {}).get("use_experience", True):
            self.experience_store.record_from_task(
                task_id=task_id, role=role, prompt=prompt,
                output=out, success=(st == "done"),
                verdict=verdict, artifacts=arts, created_at=ts,
            )
        write_result_note(self.memory_store, task_id, st, out, err)
        self._tasks.pop(task_id, None)

    def _worker_base(self, worker_id: str) -> str:
        w = self.registry.get_worker(worker_id)
        if w is None:
            return ""
        return f"http://{w.host}:{w.port}"

    def get_background_task(self, task_id: str) -> asyncio.Task | None:
        return self._tasks.get(task_id)


async def _check_product_async(
    base: str, task_id: str, output: Any, artifacts: list,
    product_check: Callable[[str], dict],
    async_http: AsyncHttpCall,
    max_chars: int = 6000,
) -> dict | None:
    """拉取文本 artifact 内容，合并 output 过产物检验（输入截断到 max_chars）。"""
    combined = output or ""
    for art in artifacts or []:
        if not is_text_artifact(art):
            continue
        try:
            ar = await async_http("GET", f"{base}/artifact/{task_id}",
                                  params={"path": art})
            if ar.get("status_code") == 200:
                combined += f"\n\n--- artifact: {art} ---\n" + \
                            ar.get("json", {}).get("content", "")
        except Exception:
            continue
    return product_check(combined[:max_chars])
