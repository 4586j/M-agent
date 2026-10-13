"""Deterministic lifecycle regressions from the 2026-10-13 architecture review."""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from async_dispatcher import AsyncRemoteDispatcher
from budget_store import BudgetStore
from dispatch_context import AdmittedDispatcher, approved_dispatch
from dispatcher import RemoteDispatcher
from retry_manager import RetryManager
from worker_registry import WorkerInfo, WorkerRegistry

from task_store import TaskRecord, TaskStore


def registry():
    reg = WorkerRegistry()
    reg.register(WorkerInfo("worker-test", "executor", "127.0.0.1", 1, {}, 0))
    return reg


def record(task_id="task-test"):
    return TaskRecord(task_id, "executor", "fixture", "worker-test", "running", "1", "1")


def test_cancel_wins_while_callback_waits_for_artifact(tmp_path):
    async def scenario():
        store = TaskStore(str(tmp_path))
        store.write(record())
        entered, release = asyncio.Event(), asyncio.Event()
        async def http(*args, **kwargs):
            entered.set()
            await release.wait()
            return {"status_code": 200, "json": {"content": "fixture"}}
        spent = []
        disp = AsyncRemoteDispatcher(store, registry(), http,
            product_check=lambda _: {"verdict": "pass"}, on_spend=spent.append)
        callback = asyncio.create_task(disp.handle_callback("task-test", {
            "status": "done", "artifacts": ["out.txt"], "meta": {"cost_usd": 1}}))
        await entered.wait()
        store.finalize_if("task-test", ("running",), status="cancelled")
        release.set()
        assert await callback is False
        assert store.get("task-test").status == "cancelled"
        assert spent == []
    asyncio.run(scenario())


def test_concurrent_callbacks_settle_once(tmp_path):
    async def scenario():
        store = TaskStore(str(tmp_path))
        store.write(record())
        release = asyncio.Event()
        entered = 0
        async def http(*args, **kwargs):
            nonlocal entered
            entered += 1
            if entered == 2:
                release.set()
            await release.wait()
            return {"status_code": 200, "json": {"content": "fixture"}}
        spent = []
        disp = AsyncRemoteDispatcher(store, registry(), http,
            product_check=lambda _: {"verdict": "pass"}, on_spend=spent.append)
        data = {"status": "done", "artifacts": ["out.txt"], "meta": {"cost_usd": 1}}
        handled = await asyncio.gather(*(disp.handle_callback("task-test", data) for _ in range(2)))
        assert sorted(handled) == [False, True]
        assert spent == [1]
    asyncio.run(scenario())


def test_early_callback_cannot_be_reset_to_running(tmp_path):
    async def scenario():
        store = TaskStore(str(tmp_path))
        async def http(method, url, **kwargs):
            await disp.handle_callback(kwargs["json"]["task_id"], {"status": "done"})
            return {"status_code": 200}
        disp = AsyncRemoteDispatcher(store, registry(), http)
        task_id = await disp.dispatch_async("executor", "fixture")
        assert store.get(task_id).status == "done"
        assert disp.get_background_task(task_id) is None
    asyncio.run(scenario())


def test_recovery_queries_existing_task_without_reexecuting(tmp_path):
    async def scenario():
        store = TaskStore(str(tmp_path))
        store.write(record())
        calls = []
        async def http(method, url, **kwargs):
            calls.append(method)
            return {"status_code": 200, "json": {"status": "done", "output": "restored"}}
        disp = AsyncRemoteDispatcher(store, registry(), http, poll_interval=0)
        assert disp.resume_polls() == 1
        assert disp.resume_polls() == 0
        await disp.get_background_task("task-test")
        assert store.get("task-test").output == "restored"
        assert calls == ["GET"]
    asyncio.run(scenario())


def test_retry_budget_does_not_reset_on_child_tasks(tmp_path):
    store = TaskStore(str(tmp_path))
    reg = registry()
    def http(*args, **kwargs):
        return {"status_code": 200, "json": {"status": "failed"}}
    disp = RemoteDispatcher(store, reg, http, max_retries=1)
    disp.dispatch("executor", "fixture")
    retry = RetryManager(store, reg, disp, retry_base_delay=0)
    for _ in range(5):
        retry.sweep_once()
    assert len(store.list_all()) == 2
    assert store.list_retryable() == []


def test_successful_retry_prevents_further_retries(tmp_path):
    store = TaskStore(str(tmp_path))
    reg = registry()
    outcome = ["failed"]
    def http(*args, **kwargs):
        return {"status_code": 200, "json": {"status": outcome[0]}}
    disp = RemoteDispatcher(store, reg, http, max_retries=3)
    original = disp.dispatch("executor", "fixture")
    outcome[0] = "done"
    retry = RetryManager(store, reg, disp, retry_base_delay=0)
    for _ in range(5):
        retry.sweep_once()
    assert len(store.list_all()) == 2
    assert store.get(original.task_id).meta["retry_succeeded"]


def test_admission_applies_to_schedules_and_approved_dispatch(tmp_path):
    import pytest
    from dispatcher import DispatchResult
    from schedule_store import ScheduleRecord, ScheduleStore
    from task_scheduler import TaskScheduler
    budget = BudgetStore(str(tmp_path / "budget.json"))
    calls = []
    class Delegate:
        def dispatch(self, **kwargs):
            calls.append(kwargs)
            return DispatchResult("child", "done")
    guarded = AdmittedDispatcher(Delegate(), budget,
        lambda _: {"verdict": "block"}, TaskStore(str(tmp_path / "tasks")))
    schedules = ScheduleStore(str(tmp_path / "schedules"))
    schedules.write(ScheduleRecord("schedule-1", "test", "* * * * *", "executor", "fixture"))
    scheduler = TaskScheduler(schedules, guarded)
    assert not scheduler.trigger_manual("schedule-1")
    assert calls == []
    with approved_dispatch("fixture"):
        assert scheduler.trigger_manual("schedule-1")
    budget.set_limits(global_limit_usd=1)
    budget.record_spend(1)
    with approved_dispatch("fixture"), pytest.raises(RuntimeError, match="预算"):
        guarded.dispatch("executor", "fixture")
    assert len(calls) == 1


def test_task_store_keeps_original_file_when_atomic_replace_fails(tmp_path, monkeypatch):
    import pytest

    import task_store
    store = TaskStore(str(tmp_path))
    store.write(record())
    store.list_all()
    def fail(*args):
        raise OSError("fixture replace failure")
    monkeypatch.setattr(task_store.os, "replace", fail)
    with pytest.raises(OSError):
        store.update("task-test", status="done")
    assert store.get("task-test").status == "running"
    assert TaskStore(str(tmp_path)).get("task-test").status == "running"


def isolated_config(tmp_path):
    from config import Config
    return Config(mode="remote", security_llm=False,
        providers_file=str(tmp_path / "providers.json"),
        budgets_file=str(tmp_path / "budget.json"),
        sessions_dir=str(tmp_path / "sessions"),
        knowledge_dir=str(tmp_path / "knowledge"))


def test_brain_wires_admission_into_scheduler_and_retry(tmp_path, monkeypatch):
    import brain_routers
    from brain_api import create_brain_app
    from dispatcher import DispatchResult
    from schedule_store import ScheduleRecord, ScheduleStore
    from task_scheduler import TaskScheduler

    calls = []
    class Delegate:
        def dispatch(self, **kwargs):
            calls.append(kwargs)
            return DispatchResult("child", "done")
    store = TaskStore(str(tmp_path / "tasks"))
    failed = record()
    failed.status, failed.max_retries = "failed", 1
    store.write(failed)
    reg, delegate = registry(), Delegate()
    budget = BudgetStore(str(tmp_path / "budget.json"))
    budget.set_limits(global_limit_usd=1)
    budget.record_spend(1)
    schedules = ScheduleStore(str(tmp_path / "schedules"))
    schedules.write(ScheduleRecord("schedule-1", "test", "* * * * *", "executor", "fixture"))
    scheduler = TaskScheduler(schedules, delegate)
    retry = RetryManager(store, reg, delegate, retry_base_delay=0)
    monkeypatch.setattr(brain_routers, "security_check", lambda *a, **kw: {"verdict": "pass"})
    create_brain_app(reg, store, isolated_config(tmp_path), dispatcher=delegate,
        budget_store=budget, task_scheduler=scheduler, retry_manager=retry)
    assert not scheduler.trigger_manual("schedule-1")
    assert retry.sweep_once()["retried"] == 0
    assert store.get(failed.task_id).retry_count == 0
    assert not store.get(failed.task_id).meta.get("reconciliation_required")
    budget.set_limits(global_limit_usd=0)
    assert retry.sweep_once()["retried"] == 1
    assert len(calls) == 1


def test_brain_lifespan_reconciles_and_unknown_heartbeat_requests_registration(tmp_path):
    import threading

    from brain_api import create_brain_app
    from fastapi.testclient import TestClient

    store = TaskStore(str(tmp_path / "tasks"))
    store.write(record())
    reg = registry()
    checked = threading.Event()
    calls = []
    async def http(method, url, **kwargs):
        calls.append(method)
        checked.set()
        return {"status_code": 200, "json": {"status": "done", "output": "recovered"}}
    disp = AsyncRemoteDispatcher(store, reg, http, poll_interval=0)
    app = create_brain_app(reg, store, isolated_config(tmp_path), async_dispatcher=disp)
    with TestClient(app) as client:
        assert checked.wait(2)
        reply = client.post("/heartbeat", json={"worker_id": "unknown-worker", "load": 0})
        assert reply.json()["status"] == "registration_required"
    assert store.get("task-test").status == "done"
    assert calls == ["GET"]
    assert disp._tasks == {}
