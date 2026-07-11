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


def test_proxy_artifact(tmp_path, monkeypatch):
    import brain_api
    client, _, _ = make_app(tmp_path)
    client.post("/register", json={
        "worker_id": "A", "role": "executor", "host": "127.0.0.1", "port": 8001, "capabilities": {}})

    def fake_http(method, url, **kwargs):
        assert "/artifact/t1" in url
        assert kwargs.get("params", {}).get("path") == "report.md"
        return {"status_code": 200, "json": {"content": "报告内容"}}

    monkeypatch.setattr(brain_api, "_http_call", fake_http)
    r = client.get("/proxy/artifact?worker_id=A&task_id=t1&path=report.md")
    assert r.status_code == 200
    assert r.json()["content"] == "报告内容"

