"""任务事件总线：进程内发布/订阅，支撑 SSE 实时流（阶段三可观测性）。

极简实现，不引入 Redis / OTel 等重依赖：
- 每个订阅者一个 asyncio.Queue（有界；满时丢最旧事件，防慢消费拖垮内存）；
- publish 线程安全：dispatcher / retry_manager / task_scheduler 可能运行在
  工作线程，通过 loop.call_soon_threadsafe 投递到订阅者所属事件循环；
- 事件类型：status（状态迁移/轮询进展）| callback（worker 回调到达）| done
  （任务终态，SSE 流收到后关闭）。state 字段承载具体任务状态
  （pending/running/done/failed/lost/cancelled），与事件名解耦。
"""
import asyncio
import itertools
import json
import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Callable

log = logging.getLogger(__name__)

TERMINAL_STATUSES = ("done", "failed", "lost", "cancelled")
MAX_QUEUE = 100           # 单订阅者队列上限（满则丢最旧，保最新）
SUMMARY_MAX_CHARS = 2000  # SSE 摘要截断上限


def truncate_summary(text, max_chars: int = SUMMARY_MAX_CHARS) -> str:
    """事件摘要限幅：非字符串转 str，超长截断并标注。"""
    s = text if isinstance(text, str) else ("" if text is None else str(text))
    if len(s) > max_chars:
        return s[:max_chars] + "…[已截断]"
    return s


@dataclass
class TaskEvent:
    event: str  # status | callback | done
    payload: dict = field(default_factory=dict)

    def sse_frame(self) -> str:
        """序列化为一条 text/event-stream 帧（event + data 两行，空行结尾）。"""
        data = json.dumps(self.payload, ensure_ascii=False, default=str)
        return f"event: {self.event}\ndata: {data}\n\n"


@dataclass
class _Sub:
    task_id: str | None  # None 订阅全部任务
    loop: asyncio.AbstractEventLoop
    queue: asyncio.Queue


class TaskEventBus:
    """内存事件总线。subscribe 须在事件循环内调用；publish 任意线程可调。

    on_publish：可选旁路回调（如 webhook 外发）。publish 时同步调用——
    实现自身必须 fire-and-forget/线程安全，且异常被吞掉（绝不影响 SSE 主路）。
    """

    def __init__(self, on_publish: Callable[[dict], None] | None = None):
        self._lock = threading.Lock()
        self._subs: dict[int, _Sub] = {}
        self._ids = itertools.count(1)
        self._on_publish = on_publish

    def subscribe(self, task_id: str | None = None) -> tuple[int, "asyncio.Queue[TaskEvent]"]:
        """注册订阅者，返回 (subscriber_id, 事件队列)。须在事件循环内调用。"""
        loop = asyncio.get_running_loop()
        q: asyncio.Queue = asyncio.Queue(maxsize=MAX_QUEUE)
        with self._lock:
            sid = next(self._ids)
            self._subs[sid] = _Sub(task_id=task_id, loop=loop, queue=q)
        return sid, q

    def unsubscribe(self, sid: int) -> None:
        with self._lock:
            self._subs.pop(sid, None)

    def publish(self, task_id: str, event: str, state: str | None = None,
                trace_id: str | None = None, summary: str | None = None,
                **extra) -> None:
        """广播任务事件。payload 固定含 task_id/trace_id/state/timestamp。"""
        payload = {
            "task_id": task_id, "trace_id": trace_id, "state": state,
            "timestamp": time.time(),
        }
        if summary is not None:
            payload["summary"] = truncate_summary(summary)
        payload.update(extra)
        ev = TaskEvent(event=event, payload=payload)
        with self._lock:
            subs = list(self._subs.values())
        if self._on_publish is not None:
            try:
                self._on_publish(dict(payload, event=event))
            except Exception:  # noqa: BLE001 - 旁路回调绝不影响事件主路
                log.warning("事件旁路回调失败 event=%s task=%s",
                            event, task_id, exc_info=True)
        for s in subs:
            if s.task_id is not None and s.task_id != task_id:
                continue
            self._offer(s, ev)

    @staticmethod
    def _offer(sub: _Sub, ev: TaskEvent) -> None:
        def _put():
            if sub.queue.full():
                try:
                    sub.queue.get_nowait()  # 丢最旧，保最新
                except asyncio.QueueEmpty:
                    pass
            sub.queue.put_nowait(ev)

        try:
            cur = asyncio.get_running_loop()
        except RuntimeError:
            cur = None
        if cur is sub.loop:
            _put()
            return
        try:
            sub.loop.call_soon_threadsafe(_put)
        except RuntimeError:
            pass  # 订阅者事件循环已关闭，静默丢弃


def publish_task_event(bus: TaskEventBus | None, task_id: str, *, event: str,
                       state: str | None = None, trace_id: str | None = None,
                       summary: str | None = None, **extra) -> None:
    """便捷入口：bus 为 None（未启用事件流）时静默跳过；发布失败不拖垮主流程。"""
    if bus is None:
        return
    try:
        bus.publish(task_id, event, state=state, trace_id=trace_id,
                    summary=summary, **extra)
    except Exception:  # noqa: BLE001 - 可观测性旁路，绝不影响派发主链路
        log.debug("publish task event failed for %s", task_id, exc_info=True)
