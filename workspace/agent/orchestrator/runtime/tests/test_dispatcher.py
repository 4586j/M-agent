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


def test_local_dispatcher_redispatch_uses_original_role_prompt(tmp_path):
    store = TaskStore(str(tmp_path))
    d = LocalDispatcher(task_store=store, now_fn=lambda: "t0")
    res1 = d.dispatch(role="executor", prompt="do x")
    res2 = d.redispatch(res1.task_id)
    assert res2.status == "done"
    assert res2.task_id != res1.task_id
    rec2 = store.get(res2.task_id)
    assert rec2.prompt == "do x"
    assert rec2.role == "executor"


def test_local_dispatcher_redispatch_missing_raises(tmp_path):
    store = TaskStore(str(tmp_path))
    d = LocalDispatcher(task_store=store, now_fn=lambda: "t0")
    try:
        d.redispatch("nope")
        assert False, "应抛 KeyError"
    except KeyError:
        pass


def test_remote_dispatcher_redispatch(tmp_path):
    store = TaskStore(str(tmp_path))
    reg = WorkerRegistry(heartbeat_timeout=45, now_fn=lambda: 0.0)
    reg.register(make_info())

    def fake_http(method, url, **kwargs):
        if method == "POST":
            return {"status_code": 200, "json": {"task_id": "task-r", "status": "accepted"}}
        return {"status_code": 200, "json": {
            "task_id": "task-r", "status": "done",
            "output": "ok", "artifacts": [], "error": None}}

    d = RemoteDispatcher(task_store=store, registry=reg, http_call=fake_http, now_fn=lambda: "t0")
    first = d.dispatch(role="executor", prompt="original")
    second = d.redispatch(first.task_id)
    assert second.status == "done"
    assert second.task_id != first.task_id
    assert store.get(second.task_id).prompt == "original"


def test_remote_dispatcher_product_check_fills_verdict(tmp_path):
    """done 后拉 artifacts + output 过产物检验，verdict 填充并持久化。"""
    store = TaskStore(str(tmp_path))
    reg = WorkerRegistry(heartbeat_timeout=45, now_fn=lambda: 0.0)
    reg.register(make_info())

    seen = {"content": ""}

    def product_check(content):
        seen["content"] = content
        return {"verdict": "block", "reason": "产物含 secret",
                "matched_rules": ["R-SECRET-01"], "suggestion": "删除"}

    def fake_http(method, url, **kwargs):
        if method == "POST":
            return {"status_code": 200, "json": {"task_id": "task-p", "status": "accepted"}}
        if "/artifact/" in url:
            return {"status_code": 200, "json": {"content": "api_key='abcdef0123456789'"}}
        return {"status_code": 200, "json": {
            "task_id": "task-p", "status": "done",
            "output": "结果", "artifacts": ["report.md"], "error": None}}

    d = RemoteDispatcher(task_store=store, registry=reg, http_call=fake_http,
                         now_fn=lambda: "t0", product_check=product_check)
    res = d.dispatch(role="executor", prompt="do x")
    assert res.status == "done"
    assert res.verdict is not None
    assert res.verdict["verdict"] == "block"
    assert "report.md" in seen["content"]  # 拉了 artifact 内容
    assert "结果" in seen["content"]  # 含 output
    rec = store.get(res.task_id)
    assert rec.verdict == res.verdict  # 持久化


def test_remote_dispatcher_no_product_check_leaves_verdict_none(tmp_path):
    store = TaskStore(str(tmp_path))
    reg = WorkerRegistry(heartbeat_timeout=45, now_fn=lambda: 0.0)
    reg.register(make_info())

    def fake_http(method, url, **kwargs):
        if method == "POST":
            return {"status_code": 200, "json": {"task_id": "task-n", "status": "accepted"}}
        return {"status_code": 200, "json": {
            "task_id": "task-n", "status": "done",
            "output": "ok", "artifacts": [], "error": None}}

    d = RemoteDispatcher(task_store=store, registry=reg, http_call=fake_http, now_fn=lambda: "t0")
    res = d.dispatch(role="executor", prompt="do x")
    assert res.verdict is None


def test_local_dispatcher_product_check(tmp_path):
    store = TaskStore(str(tmp_path))
    d = LocalDispatcher(task_store=store, now_fn=lambda: "t0",
                        product_check=lambda c: {"verdict": "pass", "reason": "ok",
                                                  "matched_rules": [], "suggestion": ""})
    res = d.dispatch(role="executor", prompt="do x")
    assert res.status == "done"
    assert res.verdict is not None
    assert res.verdict["verdict"] == "pass"

