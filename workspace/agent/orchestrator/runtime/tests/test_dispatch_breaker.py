"""worker 熔断接线测试：BreakerBoard 状态机 + 派发层跳过熔断节点 + 定点报错。

熔断开沿经 on_open 回调外发 webhook（breaker_open 事件，接线在 brain_api）。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import pytest
from fastapi.testclient import TestClient

from config import Config
from dispatch_breaker import BreakerBoard


# ---- BreakerBoard 单元 ----

def test_unknown_worker_allowed():
    board = BreakerBoard(failure_threshold=2, recovery_timeout=60.0)
    allowed, retry_after = board.allow("worker-A")
    assert allowed and retry_after == 0.0


def test_disabled_board_always_allows():
    board = BreakerBoard(enabled=False)
    for _ in range(10):
        board.record_failure("worker-A")
    allowed, _ = board.allow("worker-A")
    assert allowed


def test_opens_after_threshold_and_reports_edge_once():
    opens = []
    board = BreakerBoard(failure_threshold=2, recovery_timeout=60.0,
                         on_open=lambda wid, th: opens.append((wid, th)))
    board.record_failure("w1")
    allowed, _ = board.allow("w1")
    assert allowed  # 1 次失败未达阈值
    board.record_failure("w1")
    assert opens == [("w1", 2)]  # 开沿回调只触发一次
    allowed, retry_after = board.allow("w1")
    assert not allowed
    assert 0 < retry_after <= 60.0
    board.record_failure("w1")
    assert opens == [("w1", 2)]  # OPEN 期间连击不再触发开沿


def test_success_resets_failure_count():
    board = BreakerBoard(failure_threshold=3, recovery_timeout=60.0)
    board.record_failure("w1")
    board.record_failure("w1")
    board.record_success("w1")
    board.record_failure("w1")
    board.record_failure("w1")
    allowed, _ = board.allow("w1")
    assert allowed  # 成功重置连击，未达 3 次


def test_half_open_probe_success_closes():
    board = BreakerBoard(failure_threshold=1, recovery_timeout=0.1)
    board.record_failure("w1")
    allowed, _ = board.allow("w1")
    assert not allowed  # 立即 OPEN
    import time
    time.sleep(0.15)  # 超过恢复等待（CircuitBreaker 下限 0.1s）
    allowed, _ = board.allow("w1")
    assert allowed  # HALF_OPEN 试探放行
    board.record_success("w1")
    allowed, _ = board.allow("w1")
    assert allowed  # 恢复 CLOSED


# ---- 派发层集成 ----

def _registry_with_two_workers():
    """两个节点，端口区分（fake http 按端口决定 A 失败、B 成功）。"""
    from worker_registry import WorkerRegistry, WorkerInfo
    reg = WorkerRegistry(heartbeat_timeout=45, now_fn=lambda: 0.0)
    for wid, port in (("worker-A", 8000), ("worker-B", 8001)):
        reg.register(WorkerInfo(worker_id=wid, role="executor", host="127.0.0.1",
                                port=port, capabilities={}, last_heartbeat=0.0))
    return reg


def test_dispatcher_skips_open_worker(tmp_path):
    """worker-A 连续失败熔断后，自动派发跳过它落到 worker-B。

    round_robin 在 A/B 间轮换：r1(A,失败) r2(B,成功) r3(A,失败→达阈值熔断)
    r4(自动解析跳过 A → 落到 B)。
    """
    from dispatcher import RemoteDispatcher
    from task_store import TaskStore
    from event_bus import TaskEventBus

    reg = _registry_with_two_workers()
    store = TaskStore(str(tmp_path / ".tasks"))
    board = BreakerBoard(failure_threshold=2, recovery_timeout=60.0)

    def fake_http(method, url, **kwargs):
        if method == "POST":
            if ":8000/" in url:  # worker-A：总是失败
                return {"status_code": 500, "json": {"error": "boom"}}
            return {"status_code": 200, "json": {"task_id": "x", "status": "accepted"}}
        return {"status_code": 200, "json": {
            "task_id": "x", "status": "done", "output": "ok", "artifacts": [],
            "error": None, "meta": {}}}

    disp = RemoteDispatcher(task_store=store, registry=reg, http_call=fake_http,
                            now_fn=lambda: "t0", event_bus=TaskEventBus(),
                            breaker_board=board)
    r1 = disp.dispatch(role="executor", prompt="p1")
    assert r1.status == "failed"   # A 首败
    r2 = disp.dispatch(role="executor", prompt="p2")
    assert r2.status == "done"     # B 成功
    r3 = disp.dispatch(role="executor", prompt="p3")
    assert r3.status == "failed"   # A 次败 → 熔断开启
    r4 = disp.dispatch(role="executor", prompt="p4")
    assert r4.status == "done"
    rec4 = store.get(r4.task_id)
    assert rec4.worker_id == "worker-B"  # 自动跳过熔断中的 A


def test_dispatcher_pinned_open_worker_raises(tmp_path):
    """定点派发到熔断中的节点：明确报错（不自动换节点）。"""
    from dispatcher import RemoteDispatcher
    from task_store import TaskStore
    from event_bus import TaskEventBus

    reg = _registry_with_two_workers()
    store = TaskStore(str(tmp_path / ".tasks"))
    board = BreakerBoard(failure_threshold=1, recovery_timeout=60.0)
    board.record_failure("worker-A")  # 直接置 OPEN

    def fake_http(method, url, **kwargs):
        return {"status_code": 200, "json": {"task_id": "x", "status": "accepted"}}

    disp = RemoteDispatcher(task_store=store, registry=reg, http_call=fake_http,
                            now_fn=lambda: "t0", event_bus=TaskEventBus(),
                            breaker_board=board)
    with pytest.raises(RuntimeError, match="熔断中"):
        disp.dispatch(role="executor", prompt="p", worker_id="worker-A")


def test_dispatch_endpoint_reports_breaker_message(tmp_path):
    """全部候选节点熔断：/dispatch 返回 failed + 可读原因（不 500）。"""
    from brain_api import create_brain_app
    from dispatcher import RemoteDispatcher
    from worker_registry import WorkerRegistry, WorkerInfo
    from task_store import TaskStore
    from event_bus import TaskEventBus
    from budget_store import BudgetStore

    reg = WorkerRegistry(heartbeat_timeout=45, now_fn=lambda: 0.0)
    reg.register(WorkerInfo(worker_id="worker-A", role="executor", host="127.0.0.1",
                            port=8000, capabilities={}, last_heartbeat=0.0))
    store = TaskStore(str(tmp_path / ".tasks"))
    board = BreakerBoard(failure_threshold=1, recovery_timeout=60.0)
    board.record_failure("worker-A")

    def fake_http(method, url, **kwargs):
        return {"status_code": 200, "json": {"task_id": "x", "status": "accepted"}}

    disp = RemoteDispatcher(task_store=store, registry=reg, http_call=fake_http,
                            now_fn=lambda: "t0", event_bus=TaskEventBus(),
                            breaker_board=board)
    cfg = Config(mode="remote", task_store_dir=str(tmp_path / ".tasks"),
                 security_llm=False)
    client = TestClient(create_brain_app(
        reg, store, cfg, dispatcher=disp,
        budget_store=BudgetStore(str(tmp_path / "budgets.json"))))
    r = client.post("/dispatch", json={"prompt": "p", "role": "executor"})
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "failed"
    assert "熔断" in body["error"]
