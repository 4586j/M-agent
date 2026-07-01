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
