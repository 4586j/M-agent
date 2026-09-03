import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from fastapi.testclient import TestClient
from brain_api import create_brain_app
from worker_registry import WorkerRegistry
from task_store import TaskStore, TaskRecord
from config import Config


def make_app(tmp_path, now_fn=None):
    reg = WorkerRegistry(heartbeat_timeout=45, now_fn=now_fn or (lambda: 0.0))
    store = TaskStore(str(tmp_path))
    cfg = Config(mode="remote", task_store_dir=str(tmp_path))
    app = create_brain_app(reg, store, cfg)
    return TestClient(app), reg, store


def test_register_worker(tmp_path):
    client, reg, _ = make_app(tmp_path)
    r = client.post("/register", json={
        "worker_id": "worker-A", "role": "executor",
        "host": "127.0.0.1", "port": 8000, "capabilities": {"model": "sonnet"},
    })
    assert r.status_code == 200
    assert r.json()["status"] == "registered"
    assert reg.select_worker("executor") is not None


def test_heartbeat(tmp_path):
    client, reg, _ = make_app(tmp_path, now_fn=lambda: 100.0)
    client.post("/register", json={
        "worker_id": "worker-A", "role": "executor",
        "host": "127.0.0.1", "port": 8000, "capabilities": {},
    })
    r = client.post("/heartbeat", json={"worker_id": "worker-A", "load": 1})
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_list_tasks_by_worker(tmp_path):
    client, _, store = make_app(tmp_path)
    store.write(TaskRecord(
        task_id="task-1", role="executor", prompt="x", worker_id="worker-A",
        status="running", created_at="t", updated_at="t"))
    r = client.get("/tasks?worker_id=worker-A")
    assert r.status_code == 200
    ids = [t["task_id"] for t in r.json()]
    assert "task-1" in ids


def test_list_workers(tmp_path):
    client, _, _ = make_app(tmp_path)
    client.post("/register", json={
        "worker_id": "worker-A", "role": "executor",
        "host": "127.0.0.1", "port": 8000, "capabilities": {},
    })
    r = client.get("/workers")
    assert r.status_code == 200
    assert any(w["worker_id"] == "worker-A" for w in r.json())


def make_app_remote(tmp_path, now_fn=None):
    """带 RemoteDispatcher 的 brain app（fake http_call），用于测 dispatch/redispatch/proxy。"""
    from dispatcher import RemoteDispatcher
    reg = WorkerRegistry(heartbeat_timeout=45, now_fn=now_fn or (lambda: 0.0))
    store = TaskStore(str(tmp_path))
    cfg = Config(mode="remote", task_store_dir=str(tmp_path))

    def fake_http(method, url, **kwargs):
        if method == "POST":
            return {"status_code": 200, "json": {"task_id": "x", "status": "accepted"}}
        return {"status_code": 200, "json": {
            "task_id": "x", "status": "done", "output": "out",
            "artifacts": [], "error": None}}

    disp = RemoteDispatcher(task_store=store, registry=reg, http_call=fake_http, now_fn=lambda: "t0")
    return TestClient(create_brain_app(reg, store, cfg, dispatcher=disp)), reg, store


def test_delete_worker(tmp_path):
    client, _, _ = make_app(tmp_path)
    client.post("/register", json={
        "worker_id": "A", "role": "executor", "host": "127.0.0.1", "port": 8000, "capabilities": {}})
    r = client.delete("/workers/A")
    assert r.status_code == 200
    assert r.json()["status"] == "unregistered"
    assert client.get("/workers").json() == []
    assert client.delete("/workers/A").status_code == 404


def test_get_task_and_404(tmp_path):
    client, _, store = make_app(tmp_path)
    store.write(TaskRecord(
        task_id="t1", role="executor", prompt="x", worker_id="A",
        status="done", created_at="t", updated_at="t"))
    r = client.get("/tasks/t1")
    assert r.status_code == 200
    assert r.json()["task_id"] == "t1"
    assert client.get("/tasks/missing").status_code == 404


def test_cancel_task(tmp_path):
    client, _, store = make_app(tmp_path)
    store.write(TaskRecord(
        task_id="t1", role="executor", prompt="x", worker_id="A",
        status="running", created_at="t", updated_at="t"))
    r = client.post("/tasks/t1/cancel")
    assert r.status_code == 200
    assert r.json()["status"] == "cancelled"


def test_cancel_rejects_terminal_status(tmp_path):
    client, _, store = make_app(tmp_path)
    store.write(TaskRecord(
        task_id="t1", role="executor", prompt="x", worker_id="A",
        status="done", created_at="t", updated_at="t"))
    assert client.post("/tasks/t1/cancel").status_code == 400


def test_redispatch(tmp_path):
    from worker_registry import WorkerInfo
    client, reg, store = make_app_remote(tmp_path)
    reg.register(WorkerInfo(worker_id="A", role="executor", host="127.0.0.1",
                            port=8001, capabilities={}, last_heartbeat=0.0))
    store.write(TaskRecord(
        task_id="t1", role="executor", prompt="hello", worker_id="A",
        status="failed", created_at="t", updated_at="t"))
    r = client.post("/tasks/t1/redispatch", json={})
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "done"
    assert body["task_id"] != "t1"


def test_stats(tmp_path):
    client, _, store = make_app(tmp_path)
    client.post("/register", json={
        "worker_id": "A", "role": "executor", "host": "127.0.0.1", "port": 8000, "capabilities": {}})
    store.write(TaskRecord(
        task_id="t1", role="executor", prompt="x", worker_id="A",
        status="done", created_at="t", updated_at="t"))
    store.write(TaskRecord(
        task_id="t2", role="executor", prompt="y", worker_id="A",
        status="running", created_at="t", updated_at="t"))
    s = client.get("/stats").json()
    assert s["worker_active"] == 1
    assert s["task_total"] == 2
    assert s["task_status"]["done"] == 1
    assert s["task_status"]["running"] == 1


def test_proxy_artifact(tmp_path):
    def fake_http(method, url, **kwargs):
        assert "/artifact/t1" in url
        assert kwargs.get("params", {}).get("path") == "report.md"
        return {"status_code": 200, "json": {"content": "报告内容"}}

    reg = WorkerRegistry(heartbeat_timeout=45, now_fn=lambda: 0.0)
    store = TaskStore(str(tmp_path))
    cfg = Config(mode="remote", task_store_dir=str(tmp_path))
    app = create_brain_app(reg, store, cfg, http_call=fake_http)
    client = TestClient(app)
    client.post("/register", json={
        "worker_id": "A", "role": "executor", "host": "127.0.0.1", "port": 8001, "capabilities": {}})
    r = client.get("/proxy/artifact?worker_id=A&task_id=t1&path=report.md")
    assert r.status_code == 200
    assert r.json()["content"] == "报告内容"


# ── /dispatch ──

def test_dispatch_success(tmp_path):
    client, reg, _ = make_app_remote(tmp_path)
    from worker_registry import WorkerInfo
    reg.register(WorkerInfo(worker_id="A", role="executor", host="127.0.0.1",
                            port=8001, capabilities={}, last_heartbeat=0.0))
    r = client.post("/dispatch", json={"prompt": "hello", "role": "executor"})
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "done"
    assert body["task_id"] is not None


def test_dispatch_no_dispatcher(tmp_path):
    client, _, _ = make_app(tmp_path)
    r = client.post("/dispatch", json={"prompt": "hello"})
    assert r.status_code == 200
    assert r.json()["status"] == "failed"


def test_dispatch_no_worker(tmp_path):
    client, _, _ = make_app_remote(tmp_path)
    r = client.post("/dispatch", json={"prompt": "hello", "role": "executor"})
    assert r.status_code == 200
    assert r.json()["status"] == "failed"


def test_dispatch_security_block(tmp_path):
    from dispatcher import RemoteDispatcher
    from unittest.mock import patch
    reg = WorkerRegistry(heartbeat_timeout=45, now_fn=lambda: 0.0)
    store = TaskStore(str(tmp_path))
    cfg = Config(mode="remote", task_store_dir=str(tmp_path))
    disp = RemoteDispatcher(task_store=store, registry=reg,
                            http_call=lambda *a, **k: {"status_code": 200, "json": {}},
                            now_fn=lambda: "t0")
    client = TestClient(create_brain_app(reg, store, cfg, dispatcher=disp))
    with patch("brain_api.security_check", return_value={"verdict": "block", "reason": "dangerous"}):
        r = client.post("/dispatch", json={"prompt": "rm -rf /"})
    assert r.json()["status"] == "blocked"


def test_dispatch_force_skips_security(tmp_path):
    from dispatcher import RemoteDispatcher
    from unittest.mock import patch
    from worker_registry import WorkerInfo
    reg = WorkerRegistry(heartbeat_timeout=45, now_fn=lambda: 0.0)
    store = TaskStore(str(tmp_path))
    cfg = Config(mode="remote", task_store_dir=str(tmp_path))

    def fake_http(method, url, **kwargs):
        if method == "POST":
            return {"status_code": 200, "json": {"status": "accepted"}}
        return {"status_code": 200, "json": {"status": "done", "output": "ok", "artifacts": []}}

    disp = RemoteDispatcher(task_store=store, registry=reg, http_call=fake_http, now_fn=lambda: "t0")
    reg.register(WorkerInfo(worker_id="A", role="executor", host="127.0.0.1",
                            port=8001, capabilities={}, last_heartbeat=0.0))
    client = TestClient(create_brain_app(reg, store, cfg, dispatcher=disp))
    called = []
    original = __import__("brain_api").security_check
    with patch("brain_api.security_check", side_effect=lambda *a, **k: called.append(1) or original(*a, **k)):
        r = client.post("/dispatch", json={"prompt": "rm -rf /", "force": True})
    assert r.json()["status"] == "done"
    assert called == []


# ── /dispatch/async ──

def test_dispatch_async_no_async_dispatcher(tmp_path):
    client, _, _ = make_app(tmp_path)
    r = client.post("/dispatch/async", json={"prompt": "hello"})
    assert r.json()["status"] == "failed"


def test_dispatch_async_success(tmp_path):
    import asyncio
    from async_dispatcher import AsyncRemoteDispatcher
    reg = WorkerRegistry(heartbeat_timeout=45, now_fn=lambda: 0.0)
    store = TaskStore(str(tmp_path))
    cfg = Config(mode="remote", task_store_dir=str(tmp_path))

    async def fake_async_http(method, url, **kwargs):
        return {"status_code": 200, "json": {"status": "accepted"}}

    from worker_registry import WorkerInfo
    reg.register(WorkerInfo(worker_id="A", role="executor", host="127.0.0.1",
                            port=8001, capabilities={}, last_heartbeat=0.0))
    async_disp = AsyncRemoteDispatcher(
        task_store=store, registry=reg, async_http=fake_async_http,
        now_fn=lambda: "t0",
    )
    client = TestClient(create_brain_app(reg, store, cfg, async_dispatcher=async_disp))
    r = client.post("/dispatch/async", json={"prompt": "hello", "role": "executor", "force": True})
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "accepted"
    assert body["task_id"].startswith("task-")


# ── /tasks/{id}/retry ──

def test_retry_task_success(tmp_path):
    from worker_registry import WorkerInfo
    client, reg, store = make_app_remote(tmp_path)
    reg.register(WorkerInfo(worker_id="A", role="executor", host="127.0.0.1",
                            port=8001, capabilities={}, last_heartbeat=0.0))
    store.write(TaskRecord(
        task_id="t1", role="executor", prompt="hello", worker_id="A",
        status="failed", created_at="t", updated_at="t"))
    r = client.post("/tasks/t1/retry")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "done"


def test_retry_no_dispatcher(tmp_path):
    client, _, store = make_app(tmp_path)
    store.write(TaskRecord(
        task_id="t1", role="executor", prompt="x", worker_id="A",
        status="failed", created_at="t", updated_at="t"))
    r = client.post("/tasks/t1/retry")
    assert r.status_code == 400


def test_retry_rejects_non_retryable_status(tmp_path):
    client, _, store = make_app(tmp_path)
    store.write(TaskRecord(
        task_id="t1", role="executor", prompt="x", worker_id="A",
        status="done", created_at="t", updated_at="t"))
    assert client.post("/tasks/t1/retry").status_code == 400


def test_retry_404(tmp_path):
    client, reg, _ = make_app_remote(tmp_path)
    assert client.post("/tasks/missing/retry").status_code == 404


# ── /tasks/{id}/status ──

def test_task_status(tmp_path):
    client, _, store = make_app(tmp_path)
    store.write(TaskRecord(
        task_id="t1", role="executor", prompt="x", worker_id="A",
        status="running", created_at="t", updated_at="t"))
    r = client.get("/tasks/t1/status")
    assert r.status_code == 200
    body = r.json()
    assert body["task_id"] == "t1"
    assert body["status"] == "running"
    assert body["worker_id"] == "A"


def test_task_status_404(tmp_path):
    client, _, _ = make_app(tmp_path)
    assert client.get("/tasks/missing/status").status_code == 404


# ── /tasks/{id}/callback ──

def test_callback_no_async_dispatcher(tmp_path):
    client, _, _ = make_app(tmp_path)
    r = client.post("/tasks/t1/callback", json={"status": "done", "output": "ok"})
    assert r.status_code == 400


def test_callback_success(tmp_path):
    import asyncio
    from async_dispatcher import AsyncRemoteDispatcher
    reg = WorkerRegistry(heartbeat_timeout=45, now_fn=lambda: 0.0)
    store = TaskStore(str(tmp_path))
    cfg = Config(mode="remote", task_store_dir=str(tmp_path))
    store.write(TaskRecord(
        task_id="t1", role="executor", prompt="x", worker_id="A",
        status="running", created_at="t", updated_at="t"))

    async def fake_async_http(method, url, **kwargs):
        return {"status_code": 200, "json": {}}

    async_disp = AsyncRemoteDispatcher(
        task_store=store, registry=reg, async_http=fake_async_http,
        now_fn=lambda: "t0",
    )
    client = TestClient(create_brain_app(reg, store, cfg, async_dispatcher=async_disp))
    r = client.post("/tasks/t1/callback", json={"status": "done", "output": "result"})
    assert r.status_code == 200
    assert r.json()["handled"] is True
    assert store.get("t1").status == "done"


# ── /experiences ──

def _make_exp_store(tmp_path):
    from experience_store import ExperienceStore, ExperienceRecord
    exp = ExperienceStore(str(tmp_path / "exp"))
    exp.write(ExperienceRecord(
        exp_id="exp-1", role="executor", prompt="build feature",
        output_summary="done", success=True, created_at="t0"))
    exp.write(ExperienceRecord(
        exp_id="exp-2", role="executor", prompt="fix bug",
        output_summary="failed", success=False, created_at="t1"))
    return exp


def test_list_experiences(tmp_path):
    exp = _make_exp_store(tmp_path)
    client, reg, store = make_app(tmp_path)
    app = create_brain_app(reg, store, Config(mode="remote", task_store_dir=str(tmp_path)),
                           experience_store=exp)
    client2 = TestClient(app)
    r = client2.get("/experiences")
    assert r.status_code == 200
    assert len(r.json()) == 1
    assert r.json()[0]["exp_id"] == "exp-1"


def test_list_experiences_all(tmp_path):
    exp = _make_exp_store(tmp_path)
    client, reg, store = make_app(tmp_path)
    app = create_brain_app(reg, store, Config(mode="remote", task_store_dir=str(tmp_path)),
                           experience_store=exp)
    client2 = TestClient(app)
    r = client2.get("/experiences?successful_only=false")
    assert len(r.json()) == 2


def test_list_experiences_no_store(tmp_path):
    client, _, _ = make_app(tmp_path)
    assert client.get("/experiences").status_code == 400


def test_search_experiences(tmp_path):
    exp = _make_exp_store(tmp_path)
    client, reg, store = make_app(tmp_path)
    app = create_brain_app(reg, store, Config(mode="remote", task_store_dir=str(tmp_path)),
                           experience_store=exp)
    client2 = TestClient(app)
    r = client2.post("/experiences/search", json={"query": "build feature"})
    assert r.status_code == 200
    assert len(r.json()) == 1
    assert r.json()[0]["exp_id"] == "exp-1"


def test_get_experience(tmp_path):
    exp = _make_exp_store(tmp_path)
    client, reg, store = make_app(tmp_path)
    app = create_brain_app(reg, store, Config(mode="remote", task_store_dir=str(tmp_path)),
                           experience_store=exp)
    client2 = TestClient(app)
    r = client2.get("/experiences/exp-1")
    assert r.status_code == 200
    assert r.json()["exp_id"] == "exp-1"
    assert client2.get("/experiences/missing").status_code == 404


def test_delete_experience(tmp_path):
    exp = _make_exp_store(tmp_path)
    client, reg, store = make_app(tmp_path)
    app = create_brain_app(reg, store, Config(mode="remote", task_store_dir=str(tmp_path)),
                           experience_store=exp)
    client2 = TestClient(app)
    r = client2.delete("/experiences/exp-1")
    assert r.status_code == 200
    assert r.json()["status"] == "deleted"
    assert client2.delete("/experiences/exp-1").status_code == 404


def test_reindex_experiences(tmp_path):
    exp = _make_exp_store(tmp_path)
    client, reg, store = make_app(tmp_path)
    app = create_brain_app(reg, store, Config(mode="remote", task_store_dir=str(tmp_path)),
                           experience_store=exp)
    client2 = TestClient(app)
    r = client2.post("/experiences/reindex")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


# ── /schedules ──

def _make_schedule_store(tmp_path):
    from schedule_store import ScheduleStore
    return ScheduleStore(str(tmp_path / "sched"))


def test_create_schedule(tmp_path):
    ss = _make_schedule_store(tmp_path)
    client, reg, store = make_app(tmp_path)
    from task_scheduler import TaskScheduler
    from dispatcher import RemoteDispatcher
    disp = RemoteDispatcher(task_store=store, registry=reg,
                            http_call=lambda *a, **k: {"status_code": 200, "json": {}},
                            now_fn=lambda: "t0")
    sched = TaskScheduler(schedule_store=ss, dispatcher=disp)
    app = create_brain_app(reg, store, Config(mode="remote", task_store_dir=str(tmp_path)),
                           schedule_store=ss, task_scheduler=sched)
    client2 = TestClient(app)
    r = client2.post("/schedules", json={
        "name": "daily", "cron_expr": "@interval_60s",
        "role": "executor", "prompt": "check status",
    })
    assert r.status_code == 200
    body = r.json()
    assert body["name"] == "daily"
    assert body["schedule_id"].startswith("sched-")


def test_create_schedule_bad_cron(tmp_path):
    ss = _make_schedule_store(tmp_path)
    client, reg, store = make_app(tmp_path)
    app = create_brain_app(reg, store, Config(mode="remote", task_store_dir=str(tmp_path)),
                           schedule_store=ss)
    client2 = TestClient(app)
    r = client2.post("/schedules", json={
        "name": "bad", "cron_expr": "not a cron",
        "role": "executor", "prompt": "x",
    })
    assert r.status_code == 400


def test_list_schedules(tmp_path):
    ss = _make_schedule_store(tmp_path)
    client, reg, store = make_app(tmp_path)
    app = create_brain_app(reg, store, Config(mode="remote", task_store_dir=str(tmp_path)),
                           schedule_store=ss)
    client2 = TestClient(app)
    client2.post("/schedules", json={
        "name": "s1", "cron_expr": "@interval_60s",
        "role": "executor", "prompt": "x",
    })
    r = client2.get("/schedules")
    assert r.status_code == 200
    assert len(r.json()) == 1


def test_delete_schedule(tmp_path):
    ss = _make_schedule_store(tmp_path)
    client, reg, store = make_app(tmp_path)
    app = create_brain_app(reg, store, Config(mode="remote", task_store_dir=str(tmp_path)),
                           schedule_store=ss)
    client2 = TestClient(app)
    body = client2.post("/schedules", json={
        "name": "s1", "cron_expr": "@interval_60s",
        "role": "executor", "prompt": "x",
    }).json()
    sid = body["schedule_id"]
    r = client2.delete(f"/schedules/{sid}")
    assert r.status_code == 200
    assert r.json()["status"] == "deleted"
    assert client2.delete(f"/schedules/{sid}").status_code == 404


def test_toggle_schedule(tmp_path):
    ss = _make_schedule_store(tmp_path)
    client, reg, store = make_app(tmp_path)
    app = create_brain_app(reg, store, Config(mode="remote", task_store_dir=str(tmp_path)),
                           schedule_store=ss)
    client2 = TestClient(app)
    sid = client2.post("/schedules", json={
        "name": "s1", "cron_expr": "@interval_60s",
        "role": "executor", "prompt": "x",
    }).json()["schedule_id"]
    r = client2.post(f"/schedules/{sid}/toggle")
    assert r.status_code == 200
    assert r.json()["enabled"] is False
    r2 = client2.post(f"/schedules/{sid}/toggle")
    assert r2.json()["enabled"] is True


def test_toggle_schedule_404(tmp_path):
    ss = _make_schedule_store(tmp_path)
    client, reg, store = make_app(tmp_path)
    app = create_brain_app(reg, store, Config(mode="remote", task_store_dir=str(tmp_path)),
                           schedule_store=ss)
    client2 = TestClient(app)
    assert client2.post("/schedules/missing/toggle").status_code == 404


def test_trigger_schedule(tmp_path):
    from worker_registry import WorkerInfo
    ss = _make_schedule_store(tmp_path)
    reg = WorkerRegistry(heartbeat_timeout=45, now_fn=lambda: 0.0)
    store = TaskStore(str(tmp_path))
    reg.register(WorkerInfo(worker_id="A", role="executor", host="127.0.0.1",
                            port=8001, capabilities={}, last_heartbeat=0.0))

    def fake_http(method, url, **kwargs):
        if method == "POST":
            return {"status_code": 200, "json": {"status": "accepted"}}
        return {"status_code": 200, "json": {"status": "done", "output": "ok", "artifacts": []}}

    from dispatcher import RemoteDispatcher
    from task_scheduler import TaskScheduler
    disp = RemoteDispatcher(task_store=store, registry=reg, http_call=fake_http, now_fn=lambda: "t0")
    sched = TaskScheduler(schedule_store=ss, dispatcher=disp)
    cfg = Config(mode="remote", task_store_dir=str(tmp_path))
    app = create_brain_app(reg, store, cfg, dispatcher=disp,
                           schedule_store=ss, task_scheduler=sched)
    client = TestClient(app)
    sid = client.post("/schedules", json={
        "name": "s1", "cron_expr": "@interval_60s",
        "role": "executor", "prompt": "x",
    }).json()["schedule_id"]
    r = client.post(f"/schedules/{sid}/trigger")
    assert r.status_code == 200
    assert r.json()["status"] == "triggered"


def test_trigger_no_scheduler(tmp_path):
    ss = _make_schedule_store(tmp_path)
    client, reg, store = make_app(tmp_path)
    app = create_brain_app(reg, store, Config(mode="remote", task_store_dir=str(tmp_path)),
                           schedule_store=ss)
    client2 = TestClient(app)
    sid = client2.post("/schedules", json={
        "name": "s1", "cron_expr": "@interval_60s",
        "role": "executor", "prompt": "x",
    }).json()["schedule_id"]
    assert client2.post(f"/schedules/{sid}/trigger").status_code == 400


# ── cancel propagation ──

def test_cancel_propagates_to_worker(tmp_path):
    calls = []

    def fake_http(method, url, **kwargs):
        calls.append((method, url))
        return {"status_code": 200, "json": {"status": "cancelled"}}

    reg = WorkerRegistry(heartbeat_timeout=45, now_fn=lambda: 0.0)
    store = TaskStore(str(tmp_path))
    cfg = Config(mode="remote", task_store_dir=str(tmp_path))
    from worker_registry import WorkerInfo
    reg.register(WorkerInfo(worker_id="A", role="executor", host="127.0.0.1",
                            port=8001, capabilities={}, last_heartbeat=0.0))
    app = create_brain_app(reg, store, cfg, http_call=fake_http)
    client = TestClient(app)
    store.write(TaskRecord(
        task_id="t1", role="executor", prompt="x", worker_id="A",
        status="running", created_at="t", updated_at="t"))
    r = client.post("/tasks/t1/cancel")
    assert r.status_code == 200
    assert any("cancel" in url for _, url in calls)

