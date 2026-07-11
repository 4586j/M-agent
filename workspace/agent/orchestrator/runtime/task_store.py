"""任务持久化。每个任务一个 JSON 文件，支持状态流转与按 worker/lost 查询。"""
import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

VALID_STATUSES = ("pending", "running", "done", "failed", "lost", "cancelled")


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


class TaskStore:
    def __init__(self, store_dir: str):
        self.store_dir = Path(store_dir)
        self.store_dir.mkdir(parents=True, exist_ok=True)

    def _path(self, task_id: str) -> Path:
        return self.store_dir / f"{task_id}.json"

    def write(self, record: TaskRecord) -> None:
        if record.status not in VALID_STATUSES:
            raise ValueError(f"invalid status: {record.status}")
        with open(self._path(record.task_id), "w", encoding="utf-8") as f:
            json.dump(asdict(record), f, ensure_ascii=False, indent=2)

    def get(self, task_id: str) -> TaskRecord | None:
        p = self._path(task_id)
        if not p.exists():
            return None
        with open(p, encoding="utf-8") as f:
            return TaskRecord(**json.load(f))

    def update(self, task_id: str, **fields) -> TaskRecord:
        rec = self.get(task_id)
        if rec is None:
            raise KeyError(f"task not found: {task_id}")
        if "status" in fields and fields["status"] not in VALID_STATUSES:
            raise ValueError(f"invalid status: {fields['status']}")
        for k, v in fields.items():
            setattr(rec, k, v)
        self.write(rec)
        return rec

    def list_lost(self) -> list[TaskRecord]:
        return [r for r in self._all() if r.status == "lost"]

    def list_by_worker(self, worker_id: str) -> list[TaskRecord]:
        return [r for r in self._all() if r.worker_id == worker_id]

    def list_all(self) -> list[TaskRecord]:
        return self._all()

    def list_by_status(self, status: str) -> list[TaskRecord]:
        return [r for r in self._all() if r.status == status]

    def _all(self) -> list[TaskRecord]:
        out = []
        for p in sorted(self.store_dir.glob("*.json")):
            with open(p, encoding="utf-8") as f:
                out.append(TaskRecord(**json.load(f)))
        return out
