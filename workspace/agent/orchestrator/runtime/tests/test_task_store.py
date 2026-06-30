import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from task_store import TaskStore, TaskRecord


def make_record(task_id="task-1", status="pending", worker_id="worker-A"):
    return TaskRecord(
        task_id=task_id, role="executor", prompt="do x",
        worker_id=worker_id, status=status,
        created_at="t0", updated_at="t0",
        output=None, artifacts=[], error=None,
    )


def test_write_and_get(tmp_path):
    store = TaskStore(str(tmp_path))
    rec = make_record()
    store.write(rec)
    got = store.get("task-1")
    assert got is not None
    assert got.status == "pending"
    assert got.prompt == "do x"


def test_update_changes_status_and_fields(tmp_path):
    store = TaskStore(str(tmp_path))
    store.write(make_record())
    updated = store.update("task-1", status="done", output="result")
    assert updated.status == "done"
    assert updated.output == "result"
    assert store.get("task-1").status == "done"


def test_list_lost_returns_only_lost(tmp_path):
    store = TaskStore(str(tmp_path))
    store.write(make_record("task-1", status="lost"))
    store.write(make_record("task-2", status="running", worker_id="worker-B"))
    store.write(make_record("task-3", status="done"))
    lost = store.list_lost()
    assert [r.task_id for r in lost] == ["task-1"]


def test_list_by_worker(tmp_path):
    store = TaskStore(str(tmp_path))
    store.write(make_record("task-1", status="running", worker_id="worker-A"))
    store.write(make_record("task-2", status="running", worker_id="worker-B"))
    store.write(make_record("task-3", status="done", worker_id="worker-A"))
    by_a = store.list_by_worker("worker-A")
    assert {r.task_id for r in by_a} == {"task-1", "task-3"}


def test_get_missing_returns_none(tmp_path):
    store = TaskStore(str(tmp_path))
    assert store.get("nope") is None
