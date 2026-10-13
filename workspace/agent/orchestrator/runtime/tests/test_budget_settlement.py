"""Atomic admission and replayable accounting under concurrency and interruption."""
import asyncio
import json
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from async_dispatcher import AsyncRemoteDispatcher
from budget_store import BudgetExceeded, BudgetStore
from cost_accounting import capture_cost, replay_costs
from dispatch_context import AdmissionRejected, AdmittedDispatcher
from dispatcher import RemoteDispatcher
from worker_registry import WorkerInfo, WorkerRegistry

from task_store import TaskRecord, TaskStore


def registry():
    reg = WorkerRegistry(now_fn=lambda: 0)
    reg.register(WorkerInfo("worker-test", "executor", "127.0.0.1", 1, {}, 0))
    return reg


def ledger(tmp_path):
    return BudgetStore(str(tmp_path / "budget.json"))


def task(store, task_id="task-one", status="running"):
    store.write(TaskRecord(task_id, "executor", "fixture", "worker-test", status, "1", "1"))
    return task_id


def test_two_ledger_instances_reserve_atomically(tmp_path):
    a, b = ledger(tmp_path), ledger(tmp_path)
    a.set_limits(global_limit_usd=0.05)
    barrier = threading.Barrier(2)
    def reserve(pair):
        book, name = pair
        barrier.wait(timeout=3)
        try:
            book.reserve(name, 0.05)
            return True
        except BudgetExceeded:
            return False
    with ThreadPoolExecutor(2) as pool:
        assert sorted(pool.map(reserve, [(a, "task-a"), (b, "task-b")])) == [False, True]
    assert a.snapshot()["reserved_usd"] == 0.05
    assert b.snapshot()["global_remaining_usd"] == 0


def test_duplicate_settlement_and_upward_correction_across_restart(tmp_path):
    book = ledger(tmp_path)
    book.reserve("task-one", 0.2)
    books = [ledger(tmp_path), ledger(tmp_path)]
    with ThreadPoolExecutor(2) as pool:
        assert sorted(pool.map(lambda b: b.settle("task-one", 0.12), books)) == [False, True]
    assert ledger(tmp_path).snapshot()["spent_total_usd"] == 0.12
    assert book.settle("task-one", 0.15)
    assert not book.settle("task-one", 0.1)
    assert book.snapshot()["spent_total_usd"] == 0.15
    assert book.snapshot()["reserved_usd"] == 0


def test_sql_transaction_failure_rolls_back_charge_and_reservation(tmp_path, monkeypatch):
    book = ledger(tmp_path)
    book.reserve("task-one", 0.2)
    original = book._charge
    def fail(conn, units, day):
        original(conn, units, day)
        raise OSError("fixture power loss before commit")
    monkeypatch.setattr(book, "_charge", fail)
    with pytest.raises(OSError):
        book.settle("task-one", 0.12)
    recovered = ledger(tmp_path)
    assert recovered.snapshot()["spent_total_usd"] == 0
    assert recovered.snapshot()["reserved_usd"] == 0.2
    assert recovered.settle("task-one", 0.12)


def test_legacy_json_imported_once_without_modifying_source(tmp_path):
    path = tmp_path / "budget.json"
    original = json.dumps({"spent_total_usd": 2.5, "spent_by_day": {"2026-10-12": 1},
                           "global_limit_usd": 5})
    path.write_text(original, encoding="utf-8")
    book = ledger(tmp_path)
    book.reserve("task-one", 0.2)
    book.settle("task-one", 0.1)
    assert path.read_text(encoding="utf-8") == original
    assert ledger(tmp_path).snapshot()["spent_total_usd"] == 2.6


def test_corrupt_legacy_ledger_blocks_and_can_be_repaired(tmp_path):
    path = tmp_path / "budget.json"
    path.write_text("{bad", encoding="utf-8")
    with pytest.raises(ValueError):
        ledger(tmp_path)
    path.write_text('{"spent_total_usd": 7}', encoding="utf-8")
    assert ledger(tmp_path).snapshot()["spent_total_usd"] == 7


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -1, True, "0.1"])
def test_invalid_cost_cannot_close_reservation(tmp_path, value):
    book = ledger(tmp_path)
    book.reserve("task-one", 0.2)
    with pytest.raises(ValueError):
        book.settle("task-one", value)
    assert book.snapshot()["reserved_usd"] == 0.2


def test_outstanding_work_still_counts_after_midnight(tmp_path, monkeypatch):
    import budget_store
    monkeypatch.setattr(budget_store, "_today", lambda: "2026-10-12")
    book = ledger(tmp_path)
    book.set_limits(daily_limit_usd=0.1)
    book.reserve("task-one", 0.1)
    monkeypatch.setattr(budget_store, "_today", lambda: "2026-10-13")
    with pytest.raises(BudgetExceeded):
        ledger(tmp_path).reserve("task-two", 0.1)
    book.settle("task-one", 0.03, "2026-10-12")
    assert book.snapshot()["spent_today_usd"] == 0
    assert book.snapshot()["spent_total_usd"] == 0.03


def test_replay_after_ledger_failure_preserves_cancel_and_charges_once(tmp_path, monkeypatch):
    store, book = TaskStore(str(tmp_path / "tasks")), ledger(tmp_path)
    tid = task(store, status="cancelled")
    book.reserve(tid, 0.05)
    disp = RemoteDispatcher(store, registry(), lambda *a, **k: {})
    disp.budget_store = book
    with monkeypatch.context() as patch:
        def fail(*args):
            raise OSError("fixture full disk")
        patch.setattr(book, "settle", fail)
        with pytest.raises(OSError):
            capture_cost(disp, tid, {"cost_usd": 0.03})
    recovered_store, recovered_book = TaskStore(str(tmp_path / "tasks")), ledger(tmp_path)
    assert recovered_store.get(tid).cost_report["cost_usd"] == 0.03
    assert replay_costs(recovered_store, recovered_book) == 1
    assert replay_costs(recovered_store, recovered_book) == 0
    assert recovered_store.get(tid).status == "cancelled"
    assert recovered_book.snapshot()["spent_total_usd"] == 0.03


def test_late_cancel_callback_settles_without_reopening_task(tmp_path):
    async def scenario():
        store, book = TaskStore(str(tmp_path / "tasks")), ledger(tmp_path)
        tid = task(store, status="cancelled")
        book.reserve(tid, 0.05)
        async def http(*a, **k):
            raise AssertionError("callback must not dispatch")
        disp = AsyncRemoteDispatcher(store, registry(), http)
        disp.budget_store = book
        data = {"status": "cancelled", "meta": {"cost_usd": 0.03, "execution_finished": True}}
        assert await disp.handle_callback(tid, data) is False
        assert await disp.handle_callback(tid, data) is False
        assert store.get(tid).status == "cancelled"
        assert book.snapshot()["spent_total_usd"] == 0.03
    asyncio.run(scenario())


def test_restart_queries_cancelled_task_until_execution_receipt(tmp_path):
    async def scenario():
        store, book = TaskStore(str(tmp_path / "tasks")), ledger(tmp_path)
        tid = task(store, status="cancelled")
        book.reserve(tid, 0.05)
        calls = []
        async def http(method, url, **kwargs):
            calls.append(method)
            meta = {"execution_finished": False} if len(calls) == 1 else {
                "execution_finished": True, "cost_usd": 0.02}
            return {"status_code": 200, "json": {"status": "cancelled", "meta": meta}}
        disp = AsyncRemoteDispatcher(store, registry(), http, poll_interval=0)
        disp.budget_store = book
        assert disp.resume_polls() == 1
        await disp.get_background_task(tid)
        assert calls == ["GET", "GET"]
        assert book.snapshot()["reserved_usd"] == 0
        assert book.snapshot()["spent_total_usd"] == 0.02
        assert store.get(tid).status == "cancelled"
    asyncio.run(scenario())


def test_parallel_dispatch_cannot_spend_same_available_budget(tmp_path):
    store, book = TaskStore(str(tmp_path / "tasks")), ledger(tmp_path)
    book.set_limits(global_limit_usd=0.05)
    entered, release = threading.Event(), threading.Event()
    calls = []
    def http(method, url, **kwargs):
        calls.append(method)
        if method == "POST":
            entered.set()
            assert release.wait(3)
        return {"status_code": 200, "json": {"status": "done", "meta": {"cost_usd": 0.01}}}
    guard = AdmittedDispatcher(RemoteDispatcher(store, registry(), http), book,
                               lambda _: {"verdict": "pass"}, store)
    with ThreadPoolExecutor(1) as pool:
        pending = pool.submit(guard.dispatch, "executor", "fixture")
        try:
            assert entered.wait(2)
            with pytest.raises(AdmissionRejected):
                guard.dispatch("executor", "second")
        finally:
            release.set()
        assert pending.result(timeout=3).status == "done"
    assert calls == ["POST", "GET"]
    assert book.snapshot()["spent_total_usd"] == 0.01


@pytest.mark.parametrize("outcome,expected", [(429, 0), (500, 0.05), ("network", 0.05)])
def test_only_definitive_refusal_releases_hold(tmp_path, outcome, expected):
    store, book = TaskStore(str(tmp_path / "tasks")), ledger(tmp_path)
    def http(*args, **kwargs):
        if outcome == "network":
            raise ConnectionError("fixture response lost")
        return {"status_code": outcome}
    guarded = AdmittedDispatcher(RemoteDispatcher(store, registry(), http), book,
                                lambda _: {"verdict": "pass"}, store)
    try:
        guarded.dispatch("executor", "fixture")
    except ConnectionError:
        assert outcome == "network"
    assert book.snapshot()["reserved_usd"] == expected


def test_no_worker_releases_reservation_before_execution(tmp_path):
    store, book = TaskStore(str(tmp_path / "tasks")), ledger(tmp_path)
    guarded = AdmittedDispatcher(RemoteDispatcher(store, WorkerRegistry(), lambda *a, **k: {}),
                                book, lambda _: {"verdict": "pass"}, store)
    with pytest.raises(RuntimeError):
        guarded.dispatch("executor", "fixture")
    assert book.snapshot()["reserved_usd"] == 0


def test_local_cancelled_execution_has_durable_cost_without_legacy_double_charge(tmp_path):
    from local_executor_dispatcher import LocalExecutorDispatcher

    from agent_core.config import AgentConfig
    from agent_core.kernel import KernelResult

    store, book = TaskStore(str(tmp_path / "tasks")), ledger(tmp_path)
    cfg = AgentConfig(project_root=str(tmp_path), work_dir=str(tmp_path / "work"),
                      persona_dir=str(tmp_path / "personas"), durable_journal_enabled=False)
    old_hook = []
    disp = LocalExecutorDispatcher(store, config=cfg, on_spend=old_hook.append)
    def run(*args, **kwargs):
        tid = next(iter(disp._cancel_events))
        store.finalize_if(tid, ("pending", "running"), status="cancelled")
        return KernelResult(output="partial", cost_usd=0.02, cancelled=True)
    disp._kernel.run_oneshot = run
    guarded = AdmittedDispatcher(disp, book, lambda _: {"verdict": "pass"}, store)
    result = guarded.dispatch("executor", "fixture", use_experience=False)
    assert result.status == "cancelled"
    assert store.get(result.task_id).cost_report["cost_usd"] == 0.02
    assert store.get(result.task_id).meta["cost_usd"] == 0.02
    assert book.snapshot()["spent_total_usd"] == 0.02
    assert old_hook == []


def test_missing_cost_does_not_claim_free_execution(tmp_path):
    store, book = TaskStore(str(tmp_path / "tasks")), ledger(tmp_path)
    tid = task(store, status="done")
    book.reserve(tid, 0.05)
    disp = RemoteDispatcher(store, registry(), lambda *a, **k: {})
    disp.budget_store = book
    assert capture_cost(disp, tid, {"execution_finished": True})
    assert book.snapshot()["reserved_usd"] == 0.05
    assert store.get(tid).cost_report is None


def test_preflight_rejects_insufficient_reservation_and_exposes_holds(tmp_path):
    from brain_api import create_brain_app
    from config import Config
    from fastapi.testclient import TestClient

    store, book, reg = TaskStore(str(tmp_path / "tasks")), ledger(tmp_path), registry()
    book.set_limits(global_limit_usd=0.02)
    calls = []
    def http(*args, **kwargs):
        calls.append(args)
        raise AssertionError("must not execute")
    cfg = Config(security_llm=False, budgets_file=str(tmp_path / "budget.json"),
        providers_file=str(tmp_path / "providers.json"), sessions_dir=str(tmp_path / "sessions"),
        knowledge_dir=str(tmp_path / "knowledge"))
    client = TestClient(create_brain_app(reg, store, cfg, budget_store=book,
                                       dispatcher=RemoteDispatcher(store, reg, http)))
    assert client.post("/dispatch", json={"prompt": "fixture", "force": True}).status_code == 429
    assert calls == []
    book.reserve("task-unconfirmed", 0.01)
    pending = client.get("/budgets/reservations").json()
    assert pending[0]["task_id"] == "task-unconfirmed"
    assert pending[0]["task_status"] == "missing"
    assert pending[0]["reconciliation_required"] is True


def test_process_exit_before_commit_does_not_partially_charge(tmp_path):
    import subprocess

    book = ledger(tmp_path)
    book.reserve("task-one", 0.05)
    script = """
import os, sys
sys.path.insert(0, sys.argv[1])
from budget_store import BudgetStore
book = BudgetStore(sys.argv[2])
with book._transaction() as conn:
    book._charge(conn, 20000, '2026-10-13')
    conn.execute("UPDATE attempts SET state='settled', cost=20000 WHERE task_id='task-one'")
    os._exit(7)
"""
    result = subprocess.run([sys.executable, "-c", script,
        str(Path(__file__).resolve().parents[1]), str(tmp_path / "budget.json")],
        capture_output=True, timeout=15)
    assert result.returncode == 7, result.stderr.decode(errors="replace")
    restored = ledger(tmp_path)
    assert restored.snapshot()["spent_total_usd"] == 0
    assert restored.snapshot()["reserved_usd"] == 0.05
    assert restored.settle("task-one", 0.02)


def test_brain_startup_replays_local_outbox_before_serving(tmp_path):
    from brain_api import create_brain_app
    from config import Config
    from fastapi.testclient import TestClient

    store, book = TaskStore(str(tmp_path / "tasks")), ledger(tmp_path)
    tid = task(store, status="cancelled")
    book.reserve(tid, 0.05)
    store.record_cost(tid, 0.02, "2026-10-13")
    cfg = Config(security_llm=False, budgets_file=str(tmp_path / "budget.json"),
        providers_file=str(tmp_path / "providers.json"), sessions_dir=str(tmp_path / "sessions"),
        knowledge_dir=str(tmp_path / "knowledge"))
    # No async dispatcher: local execution also needs startup accounting replay.
    app = create_brain_app(registry(), store, cfg, budget_store=book)
    with TestClient(app) as client:
        assert client.get("/budgets").json()["spent_total_usd"] == 0.02
    assert ledger(tmp_path).snapshot()["spent_total_usd"] == 0.02
    assert store.get(tid).status == "cancelled"


def test_admission_replays_unsettled_cost_before_checking_available_budget(tmp_path):
    store, book = TaskStore(str(tmp_path / "tasks")), ledger(tmp_path)
    tid = task(store, status="done")
    book.set_limits(global_limit_usd=0.06)
    book.reserve(tid, 0.05)
    store.record_cost(tid, 0.07, "2026-10-13")
    calls = []
    def http(*args, **kwargs):
        calls.append(args)
        raise AssertionError("must not execute")
    guarded = AdmittedDispatcher(RemoteDispatcher(store, registry(), http), book,
                                lambda _: {"verdict": "pass"}, store)
    with pytest.raises(AdmissionRejected):
        guarded.dispatch("executor", "next")
    assert calls == []
    assert book.snapshot()["spent_total_usd"] == 0.07


def test_crash_after_settlement_before_terminal_write_does_not_charge_twice(tmp_path):
    async def scenario():
        store, book = TaskStore(str(tmp_path / "tasks")), ledger(tmp_path)
        tid = task(store)
        book.reserve(tid, 0.05)
        store.record_cost(tid, 0.02, "2026-10-13")
        book.settle(tid, 0.02, "2026-10-13")
        # Recreate stores at the window between accounting commit and task commit.
        restored = TaskStore(str(tmp_path / "tasks"))
        async def http(*args, **kwargs):
            return {"status_code": 200, "json": {"status": "done", "meta": {"cost_usd": 0.02}}}
        disp = AsyncRemoteDispatcher(restored, registry(), http, poll_interval=0)
        disp.budget_store = ledger(tmp_path)
        assert disp.resume_polls() == 1
        await disp.get_background_task(tid)
        assert restored.get(tid).status == "done"
        assert ledger(tmp_path).snapshot()["spent_total_usd"] == 0.02
    asyncio.run(scenario())
