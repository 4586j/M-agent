import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from dispatcher import DispatchResult, LocalDispatcher, RemoteDispatcher
from task_store import TaskStore, TaskRecord
from worker_registry import WorkerRegistry, WorkerInfo


def make_info(wid="worker-A"):
    return WorkerInfo(
        worker_id=wid, role="executor", host="127.0.0.1", port=8000,
        capabilities={}, last_heartbeat=0.0,
    )


def test_local_dispatcher_writes_task_and_returns_done(tmp_path):
    store = TaskStore(str(tmp_path))
    d = LocalDispatcher(task_store=store, now_fn=lambda: "t0")
    res = d.dispatch(role="executor", prompt="do x")
    assert res.status == "done"
    assert res.task_id.startswith("task-")
    rec = store.get(res.task_id)
    assert rec is not None
    assert rec.status == "done"


def test_remote_dispatcher_no_worker_raises(tmp_path):
    store = TaskStore(str(tmp_path))
    reg = WorkerRegistry(heartbeat_timeout=45)
    d = RemoteDispatcher(task_store=store, registry=reg, http_call=lambda *a, **k: None, now_fn=lambda: "t0")
    try:
        d.dispatch(role="executor", prompt="do x")
        assert False, "应抛错"
    except RuntimeError as e:
        assert "无可用" in str(e) and "worker" in str(e)


def test_remote_dispatcher_dispatches_and_polls(tmp_path):
    store = TaskStore(str(tmp_path))
    reg = WorkerRegistry(heartbeat_timeout=45, now_fn=lambda: 0.0)
    reg.register(make_info())

    calls = {"post": 0, "get": 0}

    def fake_http(method, url, **kwargs):
        if method == "POST":
            calls["post"] += 1
            return {"status_code": 200, "json": {"task_id": "task-x", "status": "accepted"}}
        if method == "GET":
            calls["get"] += 1
            if calls["get"] == 1:
                return {"status_code": 200, "json": {"task_id": "task-x", "status": "running"}}
            return {"status_code": 200, "json": {
                "task_id": "task-x", "status": "done",
                "output": "结果", "artifacts": [], "error": None}}
        return {"status_code": 404}

    d = RemoteDispatcher(task_store=store, registry=reg, http_call=fake_http, now_fn=lambda: "t0")
    res = d.dispatch(role="executor", prompt="do x", context_memory="ctx")
    assert res.status == "done"
    assert res.output == "结果"
    assert calls["post"] == 1
    assert calls["get"] == 2
    rec = store.get(res.task_id)
    assert rec.status == "done"


def test_remote_dispatcher_failed_propagates(tmp_path):
    store = TaskStore(str(tmp_path))
    reg = WorkerRegistry(heartbeat_timeout=45, now_fn=lambda: 0.0)
    reg.register(make_info())

    def fake_http(method, url, **kwargs):
        if method == "POST":
            return {"status_code": 200, "json": {"task_id": "task-y", "status": "accepted"}}
        return {"status_code": 200, "json": {
            "task_id": "task-y", "status": "failed", "output": None,
            "artifacts": [], "error": "boom"}}

    d = RemoteDispatcher(task_store=store, registry=reg, http_call=fake_http, now_fn=lambda: "t0")
    res = d.dispatch(role="executor", prompt="do x")
    assert res.status == "failed"
    assert res.error == "boom"
