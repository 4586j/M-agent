"""Admission, restart and cancellation contracts for the legacy HTTP worker."""
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent_runner import TaskResult
from fastapi.testclient import TestClient
from task_receipts import TaskReceipts
from worker_api import create_worker_app, start_auto_heartbeat
from worker_config import WorkerConfig


def client_for(tmp_path, runner):
    cfg = WorkerConfig(work_dir=str(tmp_path), worker_panel=False, auto_register=False)
    return TestClient(create_worker_app(runner, cfg))


def test_concurrent_duplicate_requests_execute_only_once(tmp_path):
    started, release = threading.Event(), threading.Event()
    calls = []
    class Runner:
        def run(self, req):
            calls.append(req.task_id)
            started.set()
            release.wait(5)
            return TaskResult(req.task_id, "done")
    client = client_for(tmp_path, Runner())
    body = {"task_id": "task-once", "prompt": "fixture"}
    try:
        with ThreadPoolExecutor(max_workers=4) as pool:
            replies = list(pool.map(lambda _: client.post("/task", json=body), range(4)))
        assert started.wait(2)
        assert all(r.status_code == 200 for r in replies)
        assert calls == ["task-once"]
        conflict = client.post("/task", json={**body, "prompt": "different"})
        assert conflict.status_code == 409
    finally:
        release.set()


def test_interrupted_receipt_requires_reconciliation_and_cannot_reexecute(tmp_path):
    receipts = TaskReceipts(str(tmp_path))
    # Seed an interrupted attempt as if the previous process exited after admission.
    assert receipts.claim("task-interrupted", "digest", 1) == "accepted"
    calls = []
    class Runner:
        def run(self, req):
            calls.append(req.task_id)
            return TaskResult(req.task_id, "done")
    client = client_for(tmp_path, Runner())
    recovered = client.get("/task/task-interrupted").json()
    assert recovered["status"] == "failed"
    assert recovered["meta"]["reconciliation_required"]
    assert client.post("/task", json={"task_id": "task-interrupted", "prompt": "fixture"}).status_code == 409
    assert calls == []


def test_finished_receipt_survives_worker_restart(tmp_path):
    finished = threading.Event()
    calls = []
    class Runner:
        def run(self, req):
            calls.append(req.task_id)
            return TaskResult(req.task_id, "done", output="durable")
    client = client_for(tmp_path, Runner())
    # Hook persistence so the simulated restart is after the terminal commit.
    original = TaskReceipts.finish
    from unittest.mock import patch
    def finish(self, result):
        original(self, result)
        finished.set()
    body = {"task_id": "task-durable", "prompt": "fixture"}
    with patch.object(TaskReceipts, "finish", finish):
        assert client.post("/task", json=body).status_code == 200
        assert finished.wait(2)
    restarted = client_for(tmp_path, Runner())
    assert restarted.post("/task", json=body).json()["duplicate"] is True
    assert restarted.get("/task/task-durable").json()["output"] == "durable"
    assert calls == ["task-durable"]


def test_cancel_does_not_release_slot_until_runner_stops(tmp_path, monkeypatch):
    monkeypatch.setenv("ORCH_WORKER_MAX_CONCURRENT", "1")
    started, release = threading.Event(), threading.Event()
    class Runner:
        def run(self, req):
            started.set()
            release.wait(5)  # Deliberately ignores cooperative cancellation.
            return TaskResult(req.task_id, "done")
    client = client_for(tmp_path, Runner())
    try:
        client.post("/task", json={"task_id": "task-one", "prompt": "fixture"})
        assert started.wait(2)
        assert client.post("/task/task-one/cancel").status_code == 200
        assert client.post("/task", json={"task_id": "task-two", "prompt": "fixture"}).status_code == 429
        assert client.get("/task/task-one").json()["status"] == "cancelled"
    finally:
        release.set()


def test_heartbeat_registers_again_after_brain_restart(tmp_path):
    stop = threading.Event()
    registrations = []
    class Response:
        status_code = 200
        def __init__(self, status):
            self.status = status
        def json(self):
            return {"status": self.status}
    def post(url, **kwargs):
        if url.endswith("/register"):
            registrations.append(url)
            if len(registrations) == 2:
                stop.set()
            return Response("registered")
        return Response("registration_required")
    thread = start_auto_heartbeat(WorkerConfig(work_dir=str(tmp_path)), interval=0.001,
                                  stop_event=stop, http_post=post)
    thread.join(2)
    stop.set()
    assert not thread.is_alive()
    assert len(registrations) == 2
