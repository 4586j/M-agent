"""优先级任务队列（Priority Task Queue）。

异步任务支持优先级，高优先级任务先执行。
使用 heapq 实现最小堆，priority 越小越优先。
"""
import heapq
import threading
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable


class TaskStatus(Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass(order=True)
class TaskItem:
    """队列中的任务项。"""
    priority: int
    sequence: int  # 用于同优先级任务的 FIFO 排序
    task_id: str = field(compare=False)
    func: Callable[..., Any] = field(compare=False)
    args: tuple = field(compare=False, default=())
    kwargs: dict = field(compare=False, default_factory=dict)
    status: TaskStatus = field(compare=False, default=TaskStatus.PENDING)
    result: Any = field(compare=False, default=None)
    error: Exception | None = field(compare=False, default=None)
    created_at: float = field(compare=False, default_factory=time.time)
    started_at: float | None = field(compare=False, default=None)
    completed_at: float | None = field(compare=False, default=None)


class PriorityTaskQueue:
    """优先级任务队列。

    - submit() 提交任务，返回 task_id
    - 内部 worker 线程按优先级取任务执行
    - cancel() 可取消 pending 状态的任务
    - get_status() 查询任务状态
    """

    def __init__(self, num_workers: int = 1):
        self._heap: list[TaskItem] = []
        self._tasks: dict[str, TaskItem] = {}  # task_id -> TaskItem
        self._sequence = 0  # 全局序列号，保证同优先级 FIFO
        self._lock = threading.Lock()
        self._not_empty = threading.Condition(self._lock)
        self._num_workers = max(1, num_workers)
        self._workers: list[threading.Thread] = []
        self._shutdown = False

    def start(self) -> None:
        """启动 worker 线程。"""
        if self._workers:
            return
        for i in range(self._num_workers):
            t = threading.Thread(target=self._worker_loop, daemon=True, name=f"task-worker-{i}")
            t.start()
            self._workers.append(t)

    def stop(self, wait: bool = True, timeout: float = 5.0) -> None:
        """停止 worker 线程。"""
        self._shutdown = True
        with self._not_empty:
            self._not_empty.notify_all()
        if wait:
            for t in self._workers:
                t.join(timeout=timeout)
        self._workers.clear()

    def submit(
        self,
        func: Callable[..., Any],
        *args: Any,
        priority: int = 10,
        task_id: str | None = None,
        **kwargs: Any,
    ) -> str:
        """提交任务到队列。

        Args:
            func: 要执行的函数
            priority: 优先级（越小越优先，0=最高）
            task_id: 自定义 task_id（默认自动生成 UUID）

        Returns:
            task_id
        """
        if task_id is None:
            task_id = str(uuid.uuid4())

        with self._lock:
            if task_id in self._tasks:
                raise ValueError(f"Task ID already exists: {task_id}")

            item = TaskItem(
                priority=priority,
                sequence=self._sequence,
                task_id=task_id,
                func=func,
                args=args,
                kwargs=kwargs,
            )
            self._sequence += 1
            self._tasks[task_id] = item
            heapq.heappush(self._heap, item)
            self._not_empty.notify()

        return task_id

    def cancel(self, task_id: str) -> bool:
        """取消 pending 状态的任务。

        Returns:
            True 如果成功取消，False 如果任务不存在或已非 pending
        """
        with self._lock:
            item = self._tasks.get(task_id)
            if item is None:
                return False
            if item.status != TaskStatus.PENDING:
                return False
            item.status = TaskStatus.CANCELLED
            return True

    def get_status(self, task_id: str) -> dict | None:
        """查询任务状态。"""
        with self._lock:
            item = self._tasks.get(task_id)
            if item is None:
                return None
            return {
                "task_id": item.task_id,
                "priority": item.priority,
                "status": item.status.value,
                "created_at": item.created_at,
                "started_at": item.started_at,
                "completed_at": item.completed_at,
                "result": item.result if item.status == TaskStatus.COMPLETED else None,
                "error": str(item.error) if item.error else None,
            }

    def get_result(self, task_id: str, timeout: float | None = None) -> Any:
        """等待并获取任务结果。

        Args:
            task_id: 任务 ID
            timeout: 超时时间（秒），None 表示无限等待

        Returns:
            任务返回值

        Raises:
            KeyError: 任务不存在
            Exception: 任务执行失败时抛出原异常
        """
        start = time.time()
        while True:
            with self._lock:
                item = self._tasks.get(task_id)
                if item is None:
                    raise KeyError(f"Task not found: {task_id}")
                if item.status == TaskStatus.COMPLETED:
                    return item.result
                if item.status == TaskStatus.FAILED:
                    raise item.error  # type: ignore[misc]
                if item.status == TaskStatus.CANCELLED:
                    raise RuntimeError(f"Task was cancelled: {task_id}")

                if timeout is not None:
                    elapsed = time.time() - start
                    if elapsed >= timeout:
                        raise TimeoutError(f"Timeout waiting for task: {task_id}")

            time.sleep(0.05)

    def _worker_loop(self) -> None:
        """Worker 线程主循环。"""
        while not self._shutdown:
            item = self._get_next_task()
            if item is None:
                continue
            self._execute_task(item)

    def _get_next_task(self) -> TaskItem | None:
        """从队列取下一个任务。"""
        with self._not_empty:
            while not self._heap and not self._shutdown:
                self._not_empty.wait()

            if self._shutdown:
                return None

            # 跳过已取消的任务
            while self._heap:
                item = heapq.heappop(self._heap)
                if item.status == TaskStatus.PENDING:
                    item.status = TaskStatus.RUNNING
                    item.started_at = time.time()
                    return item
            return None

    def _execute_task(self, item: TaskItem) -> None:
        """执行任务。"""
        try:
            result = item.func(*item.args, **item.kwargs)
            with self._lock:
                item.result = result
                item.status = TaskStatus.COMPLETED
                item.completed_at = time.time()
        except Exception as e:
            with self._lock:
                item.error = e
                item.status = TaskStatus.FAILED
                item.completed_at = time.time()

    @property
    def pending_count(self) -> int:
        """当前 pending 任务数。"""
        with self._lock:
            return sum(1 for item in self._tasks.values() if item.status == TaskStatus.PENDING)

    @property
    def total_count(self) -> int:
        """总任务数（含已完成）。"""
        with self._lock:
            return len(self._tasks)
