"""全局/每日预算闸门测试：BudgetStore 账本、/budgets API、/dispatch 429、记账钩子。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import pytest
from fastapi.testclient import TestClient

from budget_store import BudgetStore
from config import Config


# ---- BudgetStore 单元 ----

def test_unlimited_by_default(tmp_path):
    bs = BudgetStore(str(tmp_path / "budgets.json"))
    allowed, reason = bs.check()
    assert allowed and reason == ""
    snap = bs.snapshot()
    assert snap["global_limit_usd"] is None and snap["daily_limit_usd"] is None


def test_global_limit_blocks(tmp_path):
    bs = BudgetStore(str(tmp_path / "budgets.json"))
    bs.set_limits(global_limit_usd=1.0)
    allowed, _ = bs.check()
    assert allowed
    bs.record_spend(0.6)
    allowed, _ = bs.check()
    assert allowed
    bs.record_spend(0.4)
    allowed, reason = bs.check()
    assert not allowed
    assert "全局预算" in reason


def test_daily_limit_blocks_only_today_gate(tmp_path):
    bs = BudgetStore(str(tmp_path / "budgets.json"))
    bs.set_limits(daily_limit_usd=0.5)
    bs.record_spend(0.5)
    allowed, reason = bs.check()
    assert not allowed
    assert "当日预算" in reason


def test_invalid_spend_ignored(tmp_path):
    bs = BudgetStore(str(tmp_path / "budgets.json"))
    for bad in (None, 0, -1, "x", True):
        bs.record_spend(bad)
    assert bs.snapshot()["spent_total_usd"] == 0.0


def test_ledger_persists_across_instances(tmp_path):
    f = str(tmp_path / "budgets.json")
    bs = BudgetStore(f)
    bs.set_limits(global_limit_usd=2.0, daily_limit_usd=1.0)
    bs.record_spend(0.75)
    bs2 = BudgetStore(f)
    snap = bs2.snapshot()
    assert snap["spent_total_usd"] == 0.75
    assert snap["global_limit_usd"] == 2.0
    assert snap["daily_remaining_usd"] == 0.25
    allowed, _ = bs2.check()
    assert allowed  # 0.75 < 2.0（全局）且 < 1.0（当日）


def test_zero_limit_means_unlimited(tmp_path):
    bs = BudgetStore(str(tmp_path / "budgets.json"))
    bs.set_limits(global_limit_usd=0, daily_limit_usd=-3)
    snap = bs.snapshot()
    assert snap["global_limit_usd"] is None
    assert snap["daily_limit_usd"] is None


# ---- /budgets API 与 /dispatch 闸门 ----

def _make_app(tmp_path, budget_store=None, **cfg_kw):
    """带 RemoteDispatcher（fake http，秒回 done）的 brain app：
    /dispatch 走完整 _preflight 管线，可测预算 429 与记账。"""
    from brain_api import create_brain_app
    from dispatcher import RemoteDispatcher
    from async_dispatcher import AsyncRemoteDispatcher
    from worker_registry import WorkerRegistry, WorkerInfo
    from task_store import TaskStore
    from event_bus import TaskEventBus

    def fake_http(method, url, **kwargs):
        if method == "POST":
            return {"status_code": 200, "json": {"task_id": "x", "status": "accepted"}}
        return {"status_code": 200, "json": {
            "task_id": "x", "status": "done", "output": "out", "artifacts": [],
            "error": None, "meta": {"cost_usd": 0.01}}}

    reg = WorkerRegistry(heartbeat_timeout=45, now_fn=lambda: 0.0)
    reg.register(WorkerInfo(worker_id="worker-A", role="executor", host="127.0.0.1",
                            port=8000, capabilities={}, last_heartbeat=0.0))
    store = TaskStore(str(tmp_path / ".tasks"))
    cfg = Config(mode="remote", task_store_dir=str(tmp_path / ".tasks"),
                 security_llm=False,
                 budgets_file=str(tmp_path / "budgets.json"),
                 **cfg_kw)
    if budget_store is None:
        budget_store = BudgetStore(cfg.budgets_file)

    async def fake_async_http(method, url, **kwargs):
        return fake_http(method, url, **kwargs)

    disp = RemoteDispatcher(task_store=store, registry=reg, http_call=fake_http,
                            now_fn=lambda: "t0", event_bus=TaskEventBus(),
                            on_spend=budget_store.record_spend)
    async_disp = AsyncRemoteDispatcher(
        task_store=store, registry=reg, async_http=fake_async_http,
        now_fn=lambda: "t0", event_bus=TaskEventBus(), poll_interval=0.01,
        poll_max=5, on_spend=budget_store.record_spend)
    app = create_brain_app(reg, store, cfg, dispatcher=disp,
                           async_dispatcher=async_disp,
                           budget_store=budget_store)
    return TestClient(app), reg, store


def test_budgets_endpoints(tmp_path):
    client, _, _ = _make_app(tmp_path)
    r = client.get("/budgets")
    assert r.status_code == 200
    assert r.json()["global_limit_usd"] is None
    r = client.put("/budgets", json={"global_limit_usd": 5.0, "daily_limit_usd": 2.0})
    assert r.status_code == 200
    snap = r.json()
    assert snap["global_limit_usd"] == 5.0
    assert snap["daily_limit_usd"] == 2.0
    # PUT 0 → 视为不限制
    r = client.put("/budgets", json={"global_limit_usd": 0, "daily_limit_usd": 0})
    assert r.json()["global_limit_usd"] is None


def test_cfg_initial_limits_applied(tmp_path):
    """cfg 环境变量初始上限（>0）在装配时写入账本（未显式传入 store 时）。"""
    from brain_api import create_brain_app
    from worker_registry import WorkerRegistry
    from task_store import TaskStore
    reg = WorkerRegistry(heartbeat_timeout=45, now_fn=lambda: 0.0)
    store = TaskStore(str(tmp_path / ".tasks"))
    cfg = Config(mode="remote", task_store_dir=str(tmp_path / ".tasks"),
                 security_llm=False, budgets_file=str(tmp_path / "budgets.json"),
                 budget_global_usd=3.0, budget_daily_usd=1.5)
    client = TestClient(create_brain_app(reg, store, cfg))
    snap = client.get("/budgets").json()
    assert snap["global_limit_usd"] == 3.0
    assert snap["daily_limit_usd"] == 1.5


def test_dispatch_blocked_429_when_budget_exhausted(tmp_path):
    """超限后 /dispatch 返回 429，并落审计 decision=budget_exceeded。"""
    bs = BudgetStore(str(tmp_path / "budgets.json"))
    bs.set_limits(global_limit_usd=1.0)
    bs.record_spend(1.0)
    client, _, _ = _make_app(tmp_path, budget_store=bs)
    r = client.post("/dispatch", json={"prompt": "做个网站", "role": "executor"})
    assert r.status_code == 429
    assert "全局预算" in r.json()["detail"]
    # 审计流水可见（audit_api 读取）
    r = client.get("/audit", params={"decision": "budget_exceeded"})
    assert r.status_code == 200
    assert any(e["decision"] == "budget_exceeded" for e in r.json())


def test_async_dispatch_blocked_429(tmp_path):
    bs = BudgetStore(str(tmp_path / "budgets.json"))
    bs.set_limits(daily_limit_usd=0.5)
    bs.record_spend(0.5)
    client, _, _ = _make_app(tmp_path, budget_store=bs)
    r = client.post("/dispatch/async", json={"prompt": "x", "role": "executor"})
    assert r.status_code == 429


def test_force_dispatch_still_blocked(tmp_path):
    """force 跳过的是安全关卡，不能跳过预算总闸。"""
    bs = BudgetStore(str(tmp_path / "budgets.json"))
    bs.set_limits(global_limit_usd=0.01)
    bs.record_spend(0.01)
    client, _, _ = _make_app(tmp_path, budget_store=bs)
    r = client.post("/dispatch", json={"prompt": "x", "role": "executor", "force": True})
    assert r.status_code == 429


# ---- 记账钩子（dispatchers on_spend） ----

def test_remote_dispatcher_reports_spend(tmp_path):
    """sync 派发回传 meta.cost_usd → on_spend 上报账本。"""
    from dispatcher import RemoteDispatcher
    from worker_registry import WorkerRegistry, WorkerInfo
    from task_store import TaskStore
    from event_bus import TaskEventBus

    spent = []
    reg = WorkerRegistry(heartbeat_timeout=45, now_fn=lambda: 0.0)
    reg.register(WorkerInfo(worker_id="worker-A", role="executor", host="127.0.0.1",
                            port=8000, capabilities={}, last_heartbeat=0.0))
    store = TaskStore(str(tmp_path / ".tasks"))

    def fake_http(method, url, **kwargs):
        if method == "POST":
            return {"status_code": 200, "json": {"task_id": "x", "status": "accepted"}}
        return {"status_code": 200, "json": {
            "task_id": "x", "status": "done", "output": "out", "artifacts": [],
            "error": None, "meta": {"cost_usd": 0.123, "num_turns": 2}}}

    disp = RemoteDispatcher(task_store=store, registry=reg, http_call=fake_http,
                            now_fn=lambda: "t0", event_bus=TaskEventBus(),
                            on_spend=spent.append)
    res = disp.dispatch(role="executor", prompt="p")
    assert res.status == "done"
    assert spent == [0.123]


def test_report_spend_ignores_bad_values(tmp_path):
    from dispatcher import RemoteDispatcher
    from worker_registry import WorkerRegistry
    from task_store import TaskStore
    from event_bus import TaskEventBus

    spent = []
    reg = WorkerRegistry(heartbeat_timeout=45, now_fn=lambda: 0.0)
    store = TaskStore(str(tmp_path / ".tasks"))
    disp = RemoteDispatcher(task_store=store, registry=reg, http_call=lambda *a, **k: {},
                            event_bus=TaskEventBus(), on_spend=spent.append)
    for bad in ({"cost_usd": None}, {"cost_usd": 0}, {"cost_usd": -2},
                {"cost_usd": "1"}, {}, "not-a-dict"):
        disp._report_spend(bad)  # type: ignore[arg-type]
    assert spent == []
