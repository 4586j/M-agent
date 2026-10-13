"""任务持久化。每个任务一个 JSON 文件，支持状态流转与按 worker/lost 查询。

线程安全：approve/reject 等 sync def 端点在线程池并发运行，本模块以实例级
可重入锁（RLock）保护全部写路径；transition 提供 compare-and-set 原子状态
迁移，供审批流占位防并发双重派发（TOCTOU）。
"""
import json
import logging
import os
import tempfile
import threading
import time
import uuid
from copy import deepcopy
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from safe_id import validate_id

# 任务状态集合。awaiting_approval（阶段四审批挂起态）与 dispatching（approve
# 派发占位态）为非终态：security-guard 拦截后等待人工裁决；approve 先原子
# 占位 dispatching 再派发（完成后落终态、异常回滚回 awaiting_approval）。
# 二者均不参与自动重试（list_retryable 只取 lost/failed）、不被 sweeper 标
# lost（只扫 running）、事件总线 TERMINAL_STATUSES 亦不含它们。
VALID_STATUSES = ("pending", "running", "awaiting_approval", "dispatching",
                  "done", "failed", "lost", "cancelled")


def new_trace_id() -> str:
    """生成短格式 trace_id（uuid4 hex 截断，trc- 前缀便于日志 grep 关联）。"""
    return f"trc-{uuid.uuid4().hex[:12]}"


@dataclass
class TaskRecord:
    task_id: str
    role: str
    prompt: str
    worker_id: str | None
    status: str
    created_at: str
    updated_at: str
    output: Any = None
    artifacts: list = field(default_factory=list)
    error: Any = None
    verdict: dict | None = None  # security-guard 产物裁决（done 时若命中规则则存在）
    retry_count: int = 0
    max_retries: int = 0
    last_retry_at: str = ""
    # 链路追踪 ID：任务创建时生成，贯穿 brain/worker 日志、worker payload 与回调。
    # dataclass 默认值兜底旧任务文件缺字段（兼容读取为 None/空 dict）
    trace_id: str | None = None
    # worker 回传的执行元数据（usage/cost_usd/num_turns/model 等），终态化时由
    # dispatcher 写入；无数据时保持空 dict，查询端点原样透出供 eval/前端消费
    meta: dict = field(default_factory=dict)
    # Durable accounting outbox, independent of task status and worker metadata.
    cost_report: dict | None = None


class TaskStore:
    def __init__(self, store_dir: str):
        self.store_dir = Path(store_dir)
        self.store_dir.mkdir(parents=True, exist_ok=True)
        # 按 task_id 索引的内存缓存（dict）：装填（_all 全量一次）后 get 为 O(1)，
        # 写路径只更新受影响条目，不再整表失效——异步轮询每次 tick 对在飞任务
        # 逐个 get 的热路径不再反复读盘。None = 尚未全量装填（get 直接读单文件）。
        self._cache: dict[str, TaskRecord] | None = None
        # 可重入写锁：get 返回缓存对象引用、update 原为无锁 setattr，而
        # approve/reject 在线程池并发运行——统一串行化写路径；transition 的
        # compare-and-set 依赖它保证原子性（可重入以支持 update/transition
        # 内部复用 write）。
        self._lock = threading.RLock()

    def _path(self, task_id: str) -> Path:
        validate_id(task_id, "task_id")
        return self.store_dir / f"{task_id}.json"

    def write(self, record: TaskRecord) -> None:
        if record.status not in VALID_STATUSES:
            raise ValueError(f"invalid status: {record.status}")
        with self._lock:
            destination = self._path(record.task_id)
            fd, temporary = tempfile.mkstemp(dir=self.store_dir, suffix=".tmp")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    json.dump(asdict(record), f, ensure_ascii=False, indent=2)
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(temporary, destination)
            finally:
                if os.path.exists(temporary):
                    os.unlink(temporary)
            # 只更新受影响条目的缓存（若已装填），不整表失效
            if self._cache is not None:
                self._cache[record.task_id] = deepcopy(record)

    def get(self, task_id: str) -> TaskRecord | None:
        with self._lock:
            p = self._path(task_id)
            if self._cache is not None:
                return deepcopy(self._cache.get(task_id))
            if not p.exists():
                return None
            with open(p, encoding="utf-8") as f:
                return TaskRecord(**json.load(f))

    def update(self, task_id: str, **fields) -> TaskRecord:
        """读改写全流程持锁：并发 update 不同字段不互相覆盖。"""
        with self._lock:
            rec = self.get(task_id)
            if rec is None:
                raise KeyError(f"task not found: {task_id}")
            if "status" in fields and fields["status"] not in VALID_STATUSES:
                raise ValueError(f"invalid status: {fields['status']}")
            for k, v in fields.items():
                setattr(rec, k, v)
            if rec.cost_report and "meta" in fields:
                rec.meta = {**(rec.meta or {}), "cost_usd": rec.cost_report["cost_usd"]}
            self.write(rec)
            return rec

    def transition(self, task_id: str, from_status: str, to_status: str) -> bool:
        """原子 compare-and-set 状态迁移：仅当当前状态等于 from_status 时迁移。

        锁内完成「读-比较-写」：并发调用（如两个 approve 同时裁决同一任务）
        恰好一个成功，其余返回 False——调用方据此返回 409 Conflict，
        从根上消除「读状态→校验→派发→更新」窗口期的 TOCTOU 双重派发。
        """
        with self._lock:
            rec = self.get(task_id)
            if rec is None or rec.status != from_status:
                return False
            if to_status not in VALID_STATUSES:
                raise ValueError(f"invalid status: {to_status}")
            rec.status = to_status
            rec.updated_at = str(int(time.time()))
            self.write(rec)
            return True

    def finalize_if(self, task_id: str, expected: tuple[str, ...],
                    **fields) -> TaskRecord | None:
        """原子终态化：仅当当前状态在 expected 集合内才写入 fields（含 status）。

        用于本地执行收尾（P0：取消不可覆盖）：若任务已被 cancel 端点置为
        cancelled（终态、不可变），派发线程的迟到写入不得覆盖它——返回 None
        表示未写入（已被取消/已终态化）。锁内完成「读-比较-写」，消除取消与
        收尾的竞态；成功写入时刷新 updated_at（供时长/审计口径准确）。
        """
        with self._lock:
            rec = self.get(task_id)
            if rec is None:
                raise KeyError(f"task not found: {task_id}")
            if rec.status not in expected:
                return None
            if "status" in fields and fields["status"] not in VALID_STATUSES:
                raise ValueError(f"invalid status: {fields['status']}")
            for k, v in fields.items():
                setattr(rec, k, v)
            if rec.cost_report and "meta" in fields:
                rec.meta = {**(rec.meta or {}), "cost_usd": rec.cost_report["cost_usd"]}
            rec.updated_at = str(int(time.time()))
            self.write(rec)
            return rec

    def record_cost(self, task_id: str, cost_usd, day: str) -> dict:
        from budget_store import cost_units
        units = cost_units(cost_usd)
        with self._lock:
            rec = self.get(task_id)
            if rec is None:
                raise KeyError(task_id)
            if rec.cost_report and cost_units(rec.cost_report["cost_usd"]) >= units:
                return rec.cost_report
            report = {"cost_usd": units / 1e6,
                      "day": rec.cost_report["day"] if rec.cost_report else day}
            self.update(task_id, cost_report=report,
                        meta={**(rec.meta or {}), "cost_usd": report["cost_usd"]})
            return report

    def list_lost(self) -> list[TaskRecord]:
        return [r for r in self._all() if r.status == "lost"]

    def list_by_worker(self, worker_id: str) -> list[TaskRecord]:
        return [r for r in self._all() if r.worker_id == worker_id]

    def list_all(self) -> list[TaskRecord]:
        return self._all()

    def list_by_status(self, status: str) -> list[TaskRecord]:
        return [r for r in self._all() if r.status == status]

    def list_retryable(self) -> list[TaskRecord]:
        """返回 lost 或 failed 且 retry_count < max_retries 的任务。"""
        return [r for r in self._all()
                if r.status in ("lost", "failed")
                and r.max_retries > 0
                and r.retry_count < r.max_retries
                and not any((r.meta or {}).get(k) for k in (
                    "retry_parent_id", "retry_in_flight", "retry_succeeded",
                    "reconciliation_required"))]

    def claim_retry(self, task_id: str, timestamp: str) -> TaskRecord | None:
        """Reserve a root task's retry before executing. Interrupted claims require review."""
        with self._lock:
            rec = self.get(task_id)
            if rec is None or task_id not in {r.task_id for r in self.list_retryable()}:
                return None
            return self.update(task_id, retry_count=rec.retry_count + 1,
                last_retry_at=timestamp, updated_at=timestamp,
                meta={**(rec.meta or {}), "retry_in_flight": True})

    def release_retry(self, task_id: str) -> None:
        """Restore a reserved attempt only when admission definitively refused it."""
        with self._lock:
            rec = self.get(task_id)
            if rec is not None and (rec.meta or {}).get("retry_in_flight"):
                self.update(task_id, retry_count=max(0, rec.retry_count - 1),
                    meta={**rec.meta, "retry_in_flight": False})

    def finish_retry(self, task_id: str, child_id: str | None, status: str) -> None:
        with self._lock:
            rec = self.get(task_id)
            meta = dict(rec.meta or {})
            meta.update(retry_in_flight=False, retry_succeeded=status == "done")
            if child_id:
                meta["last_retry_task_id"] = child_id
                child = self.get(child_id)
                if child and (child.meta or {}).get("reconciliation_required"):
                    meta["reconciliation_required"] = True
            if status not in ("done", "failed", "cancelled"):
                meta["reconciliation_required"] = True
            self.update(task_id, meta=meta)

    def _all(self) -> list[TaskRecord]:
        with self._lock:
            if self._cache is None:
                loaded: dict[str, TaskRecord] = {}
                for p in sorted(self.store_dir.glob("*.json")):
                    try:
                        with open(p, encoding="utf-8") as f:
                            rec = TaskRecord(**json.load(f))
                            loaded[rec.task_id] = rec
                    except (ValueError, TypeError, OSError):
                        logging.getLogger(__name__).error("Unreadable task record: %s", p.name)
                self._cache = loaded
            # 按 task_id 排序返回，保持与 disk 文件名序一致的确定性输出（调用方
            # 多会再按 updated_at 倒序，此处保证列表视图稳定不变）
            return deepcopy(sorted(self._cache.values(), key=lambda r: r.task_id))
