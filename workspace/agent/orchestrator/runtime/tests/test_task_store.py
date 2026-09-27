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


# ── awaiting_approval（阶段四审批挂起态）──

def test_awaiting_approval_roundtrip(tmp_path):
    store = TaskStore(str(tmp_path))
    store.write(make_record("task-9", status="awaiting_approval", worker_id=None))
    got = store.get("task-9")
    assert got is not None
    assert got.status == "awaiting_approval"


def test_awaiting_approval_not_retryable_and_not_terminal(tmp_path):
    from event_bus import TERMINAL_STATUSES
    from task_store import VALID_STATUSES
    assert "awaiting_approval" in VALID_STATUSES
    # 非终态：不进终态集合（SSE 流保持打开、时间线持续可见）
    assert "awaiting_approval" not in TERMINAL_STATUSES
    store = TaskStore(str(tmp_path))
    store.write(make_record("task-a", status="awaiting_approval", worker_id=None))
    # 排队等人工裁决：不参与自动重试
    assert [r.task_id for r in store.list_retryable()] == []


# ── transition：原子 compare-and-set（approve/reject 并发裁决互斥）──

def test_dispatching_is_valid_but_not_terminal(tmp_path):
    """dispatching（approve 派发占位态）合法但非终态。"""
    from event_bus import TERMINAL_STATUSES
    from task_store import VALID_STATUSES
    assert "dispatching" in VALID_STATUSES
    assert "dispatching" not in TERMINAL_STATUSES


def test_transition_success_and_persisted(tmp_path):
    store = TaskStore(str(tmp_path))
    store.write(make_record("task-1", status="awaiting_approval", worker_id=None))
    assert store.transition("task-1", "awaiting_approval", "dispatching") is True
    assert store.get("task-1").status == "dispatching"


def test_transition_rejects_mismatched_from(tmp_path):
    """当前状态不等于 from_status：返回 False 且状态不变。"""
    store = TaskStore(str(tmp_path))
    store.write(make_record("task-1", status="awaiting_approval", worker_id=None))
    assert store.transition("task-1", "pending", "dispatching") is False
    assert store.get("task-1").status == "awaiting_approval"


def test_transition_missing_task_returns_false(tmp_path):
    store = TaskStore(str(tmp_path))
    assert store.transition("nope", "pending", "running") is False


def test_transition_invalid_to_status_raises(tmp_path):
    import pytest
    store = TaskStore(str(tmp_path))
    store.write(make_record())
    with pytest.raises(ValueError):
        store.transition("task-1", "pending", "bogus")


def test_transition_concurrent_only_one_wins(tmp_path):
    """并发 compare-and-set：N 线程抢迁移恰好一个成功（TOCTOU 防护）。"""
    import threading
    store = TaskStore(str(tmp_path))
    store.write(make_record("task-1", status="awaiting_approval", worker_id=None))
    results = []
    barrier = threading.Barrier(8)

    def worker():
        barrier.wait()
        results.append(store.transition("task-1", "awaiting_approval", "dispatching"))

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert results.count(True) == 1
    assert store.get("task-1").status == "dispatching"


def test_finalize_if_writes_when_expected(tmp_path):
    """当前状态在 expected 集合内：写入终态字段并刷新 updated_at。"""
    store = TaskStore(str(tmp_path))
    store.write(make_record("task-1", status="running"))
    rec = store.finalize_if("task-1", expected=("running", "pending"),
                            status="done", output="ok")
    assert rec is not None and rec.status == "done" and rec.output == "ok"
    assert rec.updated_at != "t0"  # 收尾刷新时间戳
    assert store.get("task-1").status == "done"


def test_finalize_if_skips_when_cancelled(tmp_path):
    """取消不可覆盖（P0）：已置 cancelled 时 finalize_if 返回 None 且不覆写。"""
    store = TaskStore(str(tmp_path))
    store.write(make_record("task-1", status="running"))
    store.update("task-1", status="cancelled")
    rec = store.finalize_if("task-1", expected=("running", "pending"),
                            status="done", output="迟到的成功")
    assert rec is None
    got = store.get("task-1")
    assert got.status == "cancelled"
    assert got.output != "迟到的成功"  # 终态不可变


def test_finalize_if_missing_task_raises(tmp_path):
    import pytest
    store = TaskStore(str(tmp_path))
    with pytest.raises(KeyError):
        store.finalize_if("nope", expected=("running",), status="done")


def test_concurrent_updates_no_corruption(tmp_path):
    """并发 update 写路径持锁：不抛异常、落盘文件始终是合法 JSON。"""
    import json as _json
    import threading
    store = TaskStore(str(tmp_path))
    store.write(make_record())

    def f1():
        for i in range(20):
            store.update("task-1", output=f"out-{i}")

    def f2():
        for i in range(20):
            store.update("task-1", error=f"err-{i}")

    t1, t2 = threading.Thread(target=f1), threading.Thread(target=f2)
    t1.start(); t2.start(); t1.join(); t2.join()
    rec = store.get("task-1")
    assert rec.output.startswith("out-")
    assert rec.error.startswith("err-")
    # 落盘文件可直接反序列化（无交错写坏）
    data = _json.loads((store.store_dir / "task-1.json").read_text(encoding="utf-8"))
    assert data["task_id"] == "task-1"
