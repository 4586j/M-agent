"""异步派发器：asyncio + httpx，立即返回 task_id，后台轮询或回调写入结果。"""
import asyncio
import logging
import time
import uuid
from typing import Any, Awaitable, Callable

from task_store import TaskStore, TaskRecord
from worker_registry import WorkerRegistry
from experience_store import ExperienceStore
from memory_store import MemoryStore
from dispatch_common import is_text_artifact, enrich_experience, write_result_note

log = logging.getLogger(__name__)

AsyncHttpCall = Callable[..., Awaitable[dict]]


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
        self._tasks: dict[str, asyncio.Task] = {}

    async def dispatch_async(
        self, role: str, prompt: str,
        context_memory: str | None = None,
        model: str | None = None,
    ) -> str:
        """异步派发：写 pending、POST worker、起后台轮询，立即返回 task_id。"""
        worker = self.registry.select_worker(role, strategy=self.select_strategy)
        if worker is None:
            raise RuntimeError(f"无可用 {role} worker")
        task_id = f"task-{uuid.uuid4().hex[:8]}"
        ts = self._now()
        self.task_store.write(TaskRecord(
            task_id=task_id, role=role, prompt=prompt,
            worker_id=worker.worker_id, status="pending",
            created_at=ts, updated_at=ts,
            max_retries=self.max_retries,
        ))
        base = f"http://{worker.host}:{worker.port}"
        enriched = enrich_experience(self.experience_store, prompt, role, context_memory)
        body: dict[str, Any] = {
            "task_id": task_id, "prompt": prompt,
            "context_memory": enriched or "",
            "callback_url": f"{self.brain_url}/tasks/{task_id}/callback",
        }
        if model:
            body["model"] = model
        resp = await self.async_http("POST", f"{base}/task", json=body)
        if resp.get("status_code") != 200:
            self.task_store.update(task_id, status="failed",
                                   error=f"post failed: {resp}")
            return task_id
        self.task_store.update(task_id, status="running")
        poll_task = asyncio.create_task(
            self._poll_worker(task_id, base, worker.worker_id, role, prompt, ts),
        )
        self._tasks[task_id] = poll_task
        return task_id

    async def handle_callback(self, task_id: str, data: dict) -> bool:
        """Worker 主动回调任务结果；若任务已终结则忽略，返回是否确实处理了。"""
        rec = self.task_store.get(task_id)
        if rec is None or rec.status in ("done", "failed"):
            return False
        await self._finalize(task_id, rec.worker_id, rec.role, rec.prompt,
                             rec.created_at, data)
        return True

    async def _poll_worker(self, task_id: str, base: str, worker_id: str,
                           role: str, prompt: str, ts: str) -> None:
        """后台轮询 worker 任务状态，done/failed 时写入结果。"""
        for _ in range(self.poll_max):
            await asyncio.sleep(self.poll_interval)
            rec = self.task_store.get(task_id)
            if rec is None or rec.status in ("done", "failed"):
                return
            try:
                r = await self.async_http("GET", f"{base}/task/{task_id}")
            except Exception:
                log.exception("async poll http error for %s", task_id)
                continue
            data = r.get("json", {})
            st = data.get("status")
            if st in ("done", "failed"):
                await self._finalize(task_id, worker_id, role, prompt, ts, data)
                return
        self.task_store.update(task_id, status="failed", error="async poll timeout")

    async def _finalize(self, task_id: str, worker_id: str, role: str,
                        prompt: str, ts: str, data: dict) -> None:
        """写入最终结果（产物检验 + 经验记录）。幂等：已终结则跳过。"""
        rec = self.task_store.get(task_id)
        if rec is None or rec.status in ("done", "failed"):
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
        self.task_store.update(
            task_id, status=st, output=out,
            artifacts=arts, error=err, verdict=verdict,
        )
        if self.experience_store:
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
