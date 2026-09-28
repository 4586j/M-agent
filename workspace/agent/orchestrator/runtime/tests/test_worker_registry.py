import sys
import threading
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


def test_unregister_removes_worker():
    reg = WorkerRegistry(heartbeat_timeout=45)
    reg.register(make_info("A"))
    assert reg.unregister("A") is True
    assert reg.select_worker("executor") is None
    assert reg.unregister("A") is False  # 已不在


def test_heartbeat_stores_load():
    reg = WorkerRegistry(heartbeat_timeout=45)
    reg.register(make_info("A"))
    reg.heartbeat("A", load=5)
    w = reg.active_workers()[0]
    assert w.load == 5


def test_select_least_load_picks_lower_load():
    reg = WorkerRegistry(heartbeat_timeout=45)
    reg.register(make_info("A"))
    reg.register(make_info("B"))
    reg.heartbeat("A", load=9)
    reg.heartbeat("B", load=1)
    assert reg.select_worker("executor", strategy="least_load").worker_id == "B"


def test_all_workers_includes_timed_out():
    times = [0.0]
    reg = WorkerRegistry(heartbeat_timeout=45, now_fn=lambda: times[0])
    reg.register(make_info("A"))
    times[0] = 1000.0  # 超时
    assert reg.active_workers() == []
    assert len(reg.all_workers()) == 1


def test_resolve_pinned_alive():
    reg = WorkerRegistry(heartbeat_timeout=45)
    reg.register(make_info("A"))
    reg.register(make_info("B"))
    w = reg.resolve_worker("executor", worker_id="A")
    assert w.worker_id == "A"


def test_resolve_pinned_unknown_raises():
    reg = WorkerRegistry(heartbeat_timeout=45)
    reg.register(make_info("A"))
    try:
        reg.resolve_worker("executor", worker_id="NOPE")
    except RuntimeError as e:
        assert "从未注册" in str(e)
    else:
        raise AssertionError("未知节点定点应抛 RuntimeError")


def test_resolve_pinned_dead_raises():
    times = [100.0]
    reg = WorkerRegistry(heartbeat_timeout=45, now_fn=lambda: times[0])
    reg.register(make_info("A"))
    reg.register(make_info("B"))
    reg.heartbeat("A")
    reg.heartbeat("B")
    times[0] = 200.0  # 全部超时
    try:
        reg.resolve_worker("executor", worker_id="A")
    except RuntimeError as e:
        msg = str(e)
        assert "心跳超时" in msg
        assert "从未注册" not in msg
    else:
        raise AssertionError("超时节点定点应抛 RuntimeError")


def test_resolve_pinned_role_ignored():
    """定点时以 worker_id 为准，不看请求的 role（role 替换由 dispatch 层负责）。"""
    reg = WorkerRegistry(heartbeat_timeout=45)
    reg.register(make_info("A", role="executor"))
    reg.register(make_info("G", role="security-guard"))
    w = reg.resolve_worker("executor", worker_id="G")
    assert w.worker_id == "G"


def test_resolve_no_pin_round_robin():
    reg = WorkerRegistry(heartbeat_timeout=45)
    reg.register(make_info("A"))
    reg.register(make_info("B"))
    picked = [reg.resolve_worker("executor").worker_id for _ in range(4)]
    assert picked == ["A", "B", "A", "B"]


def test_resolve_no_pin_no_worker_raises():
    reg = WorkerRegistry(heartbeat_timeout=45)
    try:
        reg.resolve_worker("executor")
    except RuntimeError as e:
        assert "无可用" in str(e)
    else:
        raise AssertionError("无可用节点时应抛 RuntimeError")


def test_concurrent_register_heartbeat_select():
    """并发注册/心跳/选择不再触发 dict changed size / cycle 竞态。"""
    reg = WorkerRegistry(heartbeat_timeout=45)
    errors: list[Exception] = []

    def spam_register():
        try:
            for i in range(200):
                reg.register(make_info(f"w-{i}"))
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    def spam_unregister():
        try:
            for i in range(200):
                reg.unregister(f"w-{i}")
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    def spam_heartbeat():
        try:
            for _ in range(200):
                reg.heartbeat("w-0", load=1)
                reg.get_worker("w-1")
                reg.all_workers()
                reg.dead_workers()
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    def spam_select():
        try:
            for _ in range(200):
                reg.select_worker("executor")
                reg.select_worker("executor", strategy="least_load")
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    threads = [threading.Thread(target=fn)
               for fn in (spam_register, spam_unregister, spam_heartbeat, spam_select)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []


