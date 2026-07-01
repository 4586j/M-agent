import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from worker_registry import WorkerRegistry, WorkerInfo


def make_info(wid="worker-A", role="executor"):
    return WorkerInfo(
        worker_id=wid, role=role, host="127.0.0.1", port=8000,
        capabilities={"model": "sonnet"}, last_heartbeat=0.0,
    )


def test_register_and_select(tmp_path):
    reg = WorkerRegistry(heartbeat_timeout=45)
    reg.register(make_info())
    w = reg.select_worker("executor")
    assert w is not None
    assert w.worker_id == "worker-A"


def test_select_no_active_returns_none():
    reg = WorkerRegistry(heartbeat_timeout=45)
    assert reg.select_worker("executor") is None


def test_select_round_robin():
    reg = WorkerRegistry(heartbeat_timeout=45)
    reg.register(make_info("A"))
    reg.register(make_info("B"))
    picked = [reg.select_worker("executor").worker_id for _ in range(4)]
    assert picked == ["A", "B", "A", "B"]


def test_heartbeat_timeout_marks_inactive():
    times = [100.0]
    reg = WorkerRegistry(heartbeat_timeout=45, now_fn=lambda: times[0])
    reg.register(make_info())
    reg.heartbeat("worker-A")  # last_heartbeat=100
    times[0] = 200.0  # 100s later, > 45s timeout
    assert reg.select_worker("executor") is None
    assert reg.active_workers() == []


def test_role_filter():
    reg = WorkerRegistry(heartbeat_timeout=45)
    reg.register(make_info("A", role="executor"))
    reg.register(make_info("B", role="security"))
    assert reg.select_worker("executor").worker_id == "A"
    assert reg.select_worker("security").worker_id == "B"
