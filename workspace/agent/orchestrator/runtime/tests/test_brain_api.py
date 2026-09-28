import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from fastapi.testclient import TestClient
from brain_api import create_brain_app
from worker_registry import WorkerRegistry, WorkerInfo
from task_store import TaskStore, TaskRecord
from config import Config


def make_app(tmp_path, now_fn=None):
    reg = WorkerRegistry(heartbeat_timeout=45, now_fn=now_fn or (lambda: 0.0))
    store = TaskStore(str(tmp_path))
    cfg = Config(mode="remote", task_store_dir=str(tmp_path), security_llm=False)
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


def test_register_hostname_worker(tmp_path):
    client, _, _ = make_app(tmp_path)
    r = client.post("/register", json={
        "worker_id": "worker-B", "role": "executor",
        "host": "worker-1.internal.corp", "port": 8000, "capabilities": {},
    })
    assert r.status_code == 200


def test_register_rejects_bad_host(tmp_path):
    client, reg, _ = make_app(tmp_path)
    for host in ["http://evil.com", "evil.com:7000", "user@evil.com", "../etc", "0.0.0.0"]:
        r = client.post("/register", json={
            "worker_id": "worker-A", "role": "executor",
            "host": host, "port": 8000, "capabilities": {},
        })
        assert r.status_code == 400, host
    assert reg.all_workers() == []


def test_register_rejects_bad_worker_id(tmp_path):
    client, _, _ = make_app(tmp_path)
    r = client.post("/register", json={
        "worker_id": "../evil", "role": "executor",
        "host": "127.0.0.1", "port": 8000, "capabilities": {},
    })
    assert r.status_code == 400


def test_register_rejects_bad_port(tmp_path):
    client, _, _ = make_app(tmp_path)
    for port in [0, -1, 70000]:
        r = client.post("/register", json={
            "worker_id": "worker-A", "role": "executor",
            "host": "127.0.0.1", "port": port, "capabilities": {},
        })
        assert r.status_code == 400, port


def test_register_blocks_private_host_when_strict(tmp_path, monkeypatch):
    monkeypatch.setenv("ORCH_BLOCK_PRIVATE_HOSTS", "1")
    client, _, _ = make_app(tmp_path)
    r = client.post("/register", json={
        "worker_id": "worker-A", "role": "executor",
        "host": "192.168.1.5", "port": 8000, "capabilities": {},
    })
    assert r.status_code == 400


def test_heartbeat(tmp_path):
    client, reg, _ = make_app(tmp_path, now_fn=lambda: 100.0)
    client.post("/register", json={
        "worker_id": "worker-A", "role": "executor",
        "host": "127.0.0.1", "port": 8000, "capabilities": {},
    })
    r = client.post("/heartbeat", json={"worker_id": "worker-A", "load": 1})
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_proxy_artifact_rejects_traversal(tmp_path):
    client, reg, _ = make_app(tmp_path)
    reg.register(WorkerInfo(worker_id="A", role="executor", host="127.0.0.1",
                            port=8000, capabilities={}, last_heartbeat=0.0))
    r = client.get("/proxy/artifact?worker_id=A&task_id=t1&path=../other/x")
    assert r.status_code == 400
    r = client.get("/proxy/artifact?worker_id=A&task_id=..%2Fevil&path=x")
    assert r.status_code == 400
    r = client.get("/proxy/artifact?worker_id=A&task_id=t1&path=%2Fetc%2Fpasswd")
    assert r.status_code == 400


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
    cfg = Config(mode="remote", task_store_dir=str(tmp_path), security_llm=False)

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
    cfg = Config(mode="remote", task_store_dir=str(tmp_path), security_llm=False)
    app = create_brain_app(reg, store, cfg, http_call=fake_http)
    client = TestClient(app)
    client.post("/register", json={
        "worker_id": "A", "role": "executor", "host": "127.0.0.1", "port": 8001, "capabilities": {}})
    r = client.get("/proxy/artifact?worker_id=A&task_id=t1&path=report.md")
    assert r.status_code == 200
    assert r.json()["content"] == "报告内容"


# ── /proxy/node/*（节点详情 / 工作区规定）──

def _reg_worker_a(client):
    client.post("/register", json={
        "worker_id": "A", "role": "executor", "host": "127.0.0.1", "port": 8001, "capabilities": {}})


def test_proxy_node_status(tmp_path):
    calls = []

    def fake_http(method, url, **kwargs):
        calls.append((method, url))
        return {"status_code": 200, "json": {
            "worker_id": "A", "role": "executor", "panel_enabled": True,
            "workspace": {"path": "/ws", "name": "ws", "exists": True},
            "skills": [{"name": "web-expert", "description": "d", "triggers": []}],
            "mcp_servers": [], "runs": {"running": 0, "total": 0}}}

    reg = WorkerRegistry(heartbeat_timeout=45, now_fn=lambda: 0.0)
    store = TaskStore(str(tmp_path))
    cfg = Config(mode="remote", task_store_dir=str(tmp_path), security_llm=False)
    client = TestClient(create_brain_app(reg, store, cfg, http_call=fake_http))
    # 未注册节点 → 404
    assert client.get("/proxy/node/status?worker_id=NOPE").status_code == 404
    _reg_worker_a(client)
    r = client.get("/proxy/node/status?worker_id=A")
    assert r.status_code == 200
    assert r.json()["skills"][0]["name"] == "web-expert"
    assert calls[-1] == ("GET", "http://127.0.0.1:8001/agent/status")


def test_proxy_node_status_worker_failure(tmp_path):
    """worker 未开面板（/agent/status 404）或失联 → brain 502。"""

    def fake_http(method, url, **kwargs):
        return {"status_code": 404, "json": {"detail": "panel 未安装"}}

    reg = WorkerRegistry(heartbeat_timeout=45, now_fn=lambda: 0.0)
    store = TaskStore(str(tmp_path))
    cfg = Config(mode="remote", task_store_dir=str(tmp_path), security_llm=False)
    client = TestClient(create_brain_app(reg, store, cfg, http_call=fake_http))
    _reg_worker_a(client)
    assert client.get("/proxy/node/status?worker_id=A").status_code == 502


def test_proxy_node_workspace(tmp_path):
    calls = []

    def fake_http(method, url, **kwargs):
        calls.append((method, url, kwargs.get("json")))
        return {"status_code": 200, "json": {"path": "/new-ws", "name": "new-ws"}}

    reg = WorkerRegistry(heartbeat_timeout=45, now_fn=lambda: 0.0)
    store = TaskStore(str(tmp_path))
    cfg = Config(mode="remote", task_store_dir=str(tmp_path), security_llm=False)
    client = TestClient(create_brain_app(reg, store, cfg, http_call=fake_http))
    assert client.put("/proxy/node/workspace", json={
        "worker_id": "A", "path": "/new-ws"}).status_code == 404  # 未注册
    _reg_worker_a(client)
    r = client.put("/proxy/node/workspace", json={"worker_id": "A", "path": "/new-ws"})
    assert r.status_code == 200
    assert r.json()["path"] == "/new-ws"
    assert calls[-1] == ("PUT", "http://127.0.0.1:8001/agent/workspace", {"path": "/new-ws"})


def test_proxy_node_workspace_bad_dir_passthrough(tmp_path):
    """worker 端 400（目录不存在）原样透传 400。"""

    def fake_http(method, url, **kwargs):
        return {"status_code": 400, "json": {"detail": "目录不存在: /nope"}}

    reg = WorkerRegistry(heartbeat_timeout=45, now_fn=lambda: 0.0)
    store = TaskStore(str(tmp_path))
    cfg = Config(mode="remote", task_store_dir=str(tmp_path), security_llm=False)
    client = TestClient(create_brain_app(reg, store, cfg, http_call=fake_http))
    _reg_worker_a(client)
    r = client.put("/proxy/node/workspace", json={"worker_id": "A", "path": "/nope"})
    assert r.status_code == 400
    assert "目录不存在" in r.json()["detail"]


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


def test_dispatch_security_block_awaits_approval(tmp_path):
    """阶段四审批挂起态：block 指令不再直接终结，任务置 awaiting_approval 等人工裁决。"""
    from dispatcher import RemoteDispatcher
    from unittest.mock import patch
    reg = WorkerRegistry(heartbeat_timeout=45, now_fn=lambda: 0.0)
    store = TaskStore(str(tmp_path))
    cfg = Config(mode="remote", task_store_dir=str(tmp_path), security_llm=False)
    disp = RemoteDispatcher(task_store=store, registry=reg,
                            http_call=lambda *a, **k: {"status_code": 200, "json": {}},
                            now_fn=lambda: "t0")
    client = TestClient(create_brain_app(reg, store, cfg, dispatcher=disp))
    with patch("brain_routers.security_check", return_value={"verdict": "block", "reason": "dangerous"}):
        r = client.post("/dispatch", json={"prompt": "rm -rf /"})
    body = r.json()
    assert body["status"] == "awaiting_approval"
    task_id = body["task_id"]
    assert task_id
    rec = store.get(task_id)
    assert rec is not None and rec.status == "awaiting_approval"
    approval = rec.meta["approval"]
    assert approval["status"] == "pending"
    assert approval["verdict"]["reason"] == "dangerous"
    assert approval["original_prompt"] == "rm -rf /"
    assert rec.worker_id is None  # 挂起任务不占用 worker，也未产生真实派发


def test_dispatch_force_skips_security(tmp_path):
    from dispatcher import RemoteDispatcher
    from unittest.mock import patch
    from worker_registry import WorkerInfo
    reg = WorkerRegistry(heartbeat_timeout=45, now_fn=lambda: 0.0)
    store = TaskStore(str(tmp_path))
    cfg = Config(mode="remote", task_store_dir=str(tmp_path), security_llm=False)

    def fake_http(method, url, **kwargs):
        if method == "POST":
            return {"status_code": 200, "json": {"status": "accepted"}}
        return {"status_code": 200, "json": {"status": "done", "output": "ok", "artifacts": []}}

    disp = RemoteDispatcher(task_store=store, registry=reg, http_call=fake_http, now_fn=lambda: "t0")
    reg.register(WorkerInfo(worker_id="A", role="executor", host="127.0.0.1",
                            port=8001, capabilities={}, last_heartbeat=0.0))
    client = TestClient(create_brain_app(reg, store, cfg, dispatcher=disp))
    called = []
    original = __import__("brain_routers").security_check
    with patch("brain_routers.security_check", side_effect=lambda *a, **k: called.append(1) or original(*a, **k)):
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
    cfg = Config(mode="remote", task_store_dir=str(tmp_path), security_llm=False)

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
    cfg = Config(mode="remote", task_store_dir=str(tmp_path), security_llm=False)
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
    app = create_brain_app(reg, store, Config(mode="remote", task_store_dir=str(tmp_path), security_llm=False),
                           experience_store=exp)
    client2 = TestClient(app)
    r = client2.get("/experiences")
    assert r.status_code == 200
    assert len(r.json()) == 1
    assert r.json()[0]["exp_id"] == "exp-1"


def test_list_experiences_all(tmp_path):
    exp = _make_exp_store(tmp_path)
    client, reg, store = make_app(tmp_path)
    app = create_brain_app(reg, store, Config(mode="remote", task_store_dir=str(tmp_path), security_llm=False),
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
    app = create_brain_app(reg, store, Config(mode="remote", task_store_dir=str(tmp_path), security_llm=False),
                           experience_store=exp)
    client2 = TestClient(app)
    r = client2.post("/experiences/search", json={"query": "build feature"})
    assert r.status_code == 200
    assert len(r.json()) == 1
    assert r.json()[0]["exp_id"] == "exp-1"


def test_get_experience(tmp_path):
    exp = _make_exp_store(tmp_path)
    client, reg, store = make_app(tmp_path)
    app = create_brain_app(reg, store, Config(mode="remote", task_store_dir=str(tmp_path), security_llm=False),
                           experience_store=exp)
    client2 = TestClient(app)
    r = client2.get("/experiences/exp-1")
    assert r.status_code == 200
    assert r.json()["exp_id"] == "exp-1"
    assert client2.get("/experiences/missing").status_code == 404


def test_delete_experience(tmp_path):
    exp = _make_exp_store(tmp_path)
    client, reg, store = make_app(tmp_path)
    app = create_brain_app(reg, store, Config(mode="remote", task_store_dir=str(tmp_path), security_llm=False),
                           experience_store=exp)
    client2 = TestClient(app)
    r = client2.delete("/experiences/exp-1")
    assert r.status_code == 200
    assert r.json()["status"] == "deleted"
    assert client2.delete("/experiences/exp-1").status_code == 404


def test_reindex_experiences(tmp_path):
    exp = _make_exp_store(tmp_path)
    client, reg, store = make_app(tmp_path)
    app = create_brain_app(reg, store, Config(mode="remote", task_store_dir=str(tmp_path), security_llm=False),
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
    app = create_brain_app(reg, store, Config(mode="remote", task_store_dir=str(tmp_path), security_llm=False),
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
    app = create_brain_app(reg, store, Config(mode="remote", task_store_dir=str(tmp_path), security_llm=False),
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
    app = create_brain_app(reg, store, Config(mode="remote", task_store_dir=str(tmp_path), security_llm=False),
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
    app = create_brain_app(reg, store, Config(mode="remote", task_store_dir=str(tmp_path), security_llm=False),
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
    app = create_brain_app(reg, store, Config(mode="remote", task_store_dir=str(tmp_path), security_llm=False),
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
    app = create_brain_app(reg, store, Config(mode="remote", task_store_dir=str(tmp_path), security_llm=False),
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
    cfg = Config(mode="remote", task_store_dir=str(tmp_path), security_llm=False)
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
    app = create_brain_app(reg, store, Config(mode="remote", task_store_dir=str(tmp_path), security_llm=False),
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
    cfg = Config(mode="remote", task_store_dir=str(tmp_path), security_llm=False)
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


def test_cancel_signals_local_dispatcher(tmp_path):
    """P0：本地执行（worker_id=local、无远程 worker）时，cancel 端点须
    直接信号派发器 cancel(task_id)，否则本地执行收不到取消信号。"""
    reg = WorkerRegistry(heartbeat_timeout=45, now_fn=lambda: 0.0)
    store = TaskStore(str(tmp_path))
    cfg = Config(mode="local", task_store_dir=str(tmp_path), security_llm=False)
    signaled = []

    class FakeLocalDispatcher:
        def cancel(self, task_id):
            signaled.append(task_id)
            return True

    app = create_brain_app(reg, store, cfg, dispatcher=FakeLocalDispatcher())
    client = TestClient(app)
    store.write(TaskRecord(
        task_id="tl1", role="executor", prompt="x", worker_id="local",
        status="running", created_at="t", updated_at="t"))
    r = client.post("/tasks/tl1/cancel")
    assert r.status_code == 200
    assert r.json()["status"] == "cancelled"
    assert signaled == ["tl1"]


# ── 审批流：awaiting_approval / approve / reject（阶段四）──

BLOCK_VERDICT = {"verdict": "block", "reason": "dangerous", "matched_rules": ["rule-1"],
                 "suggestion": "", "llm_enhanced": False}


def _audit_events(tmp_path):
    import json
    p = tmp_path / "audit.jsonl"
    if not p.exists():
        return []
    return [json.loads(line) for line in p.read_text(encoding="utf-8").splitlines()
            if line.strip()]


def _approval_client(tmp_path, monkeypatch):
    """带 dispatcher 的 app + 预置一个已挂起的待审批任务；审计落盘到 tmp。"""
    from unittest.mock import patch
    from dispatcher import RemoteDispatcher
    monkeypatch.setenv("ORCH_AUDIT_LOG", str(tmp_path / "audit.jsonl"))
    reg = WorkerRegistry(heartbeat_timeout=45, now_fn=lambda: 0.0)
    reg.register(WorkerInfo(worker_id="A", role="executor", host="127.0.0.1",
                            port=8001, capabilities={}, last_heartbeat=0.0))
    store = TaskStore(str(tmp_path))
    cfg = Config(mode="remote", task_store_dir=str(tmp_path), security_llm=False)
    bodies = []

    def fake_http(method, url, **kwargs):
        if method == "POST" and url.endswith("/task"):
            bodies.append(kwargs.get("json"))
            return {"status_code": 200, "json": {"status": "accepted"}}
        return {"status_code": 200,
                "json": {"status": "done", "output": "ok", "artifacts": []}}

    disp = RemoteDispatcher(task_store=store, registry=reg, http_call=fake_http,
                            now_fn=lambda: "t0")
    client = TestClient(create_brain_app(reg, store, cfg, dispatcher=disp))
    with patch("brain_routers.security_check", return_value=BLOCK_VERDICT):
        r = client.post("/dispatch", json={"prompt": "rm -rf /",
                                           "context_memory": "前置上下文",
                                           "model": "sonnet"})
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "awaiting_approval"
    return client, store, bodies, body["task_id"]


def test_approve_original_prompt_dispatches(tmp_path, monkeypatch):
    """原指令人工放行：不重检，直接按原流程派发并缓存 context/model。"""
    client, store, bodies, task_id = _approval_client(tmp_path, monkeypatch)
    r = client.post(f"/tasks/{task_id}/approve", json={}, headers={"X-Operator": "op-1"})
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "approved" and body["approved"] is True
    assert body["dispatched_task_id"].startswith("task-")
    # 原任务终态 done + 审批详情回写
    rec = store.get(task_id)
    assert rec.status == "done"
    approval = rec.meta["approval"]
    assert approval["status"] == "approved"
    assert approval["approved_by"] == "op-1"
    assert approval["approved_prompt"] == "rm -rf /"
    # 派发沿用挂起时缓存的 context/model
    dispatched = store.get(body["dispatched_task_id"])
    assert dispatched.prompt == "rm -rf /"
    assert dispatched.meta["model"] == "sonnet"
    task_body = [b for b in bodies if b and b.get("task_id") == body["dispatched_task_id"]][0]
    assert task_body["context_memory"] == "前置上下文"
    # 审计留痕：decision=approve，operator 取 X-Operator
    evts = [e for e in _audit_events(tmp_path) if e["stage"] == "approve"]
    assert evts and evts[-1]["decision"] == "approve"
    assert evts[-1]["operator"] == "op-1" and evts[-1]["task_id"] == task_id


def test_approve_revised_prompt_recheck_pass(tmp_path, monkeypatch):
    """修订后放行：重检通过则按修订后 prompt 派发。"""
    from unittest.mock import patch
    client, store, bodies, task_id = _approval_client(tmp_path, monkeypatch)
    pass_verdict = {"verdict": "pass", "reason": "无规则命中", "matched_rules": [],
                    "suggestion": "", "llm_enhanced": False}
    with patch("brain_routers.security_check", return_value=pass_verdict):
        r = client.post(f"/tasks/{task_id}/approve", json={"prompt": "ls -l 安全指令"})
    assert r.status_code == 200
    body = r.json()
    assert body["approved"] is True
    rec = store.get(task_id)
    assert rec.status == "done"
    assert rec.meta["approval"]["approved_prompt"] == "ls -l 安全指令"
    dispatched = store.get(body["dispatched_task_id"])
    assert dispatched.prompt == "ls -l 安全指令"  # 派发的是修订后指令


def test_approve_revised_prompt_recheck_block_keeps_pending(tmp_path, monkeypatch):
    """修订后仍未通过安全检验：维持挂起、更新裁决详情、不产生真实派发。"""
    from unittest.mock import patch
    client, store, bodies, task_id = _approval_client(tmp_path, monkeypatch)
    new_block = dict(BLOCK_VERDICT, reason="仍然危险", matched_rules=["rule-2"])
    with patch("brain_routers.security_check", return_value=new_block):
        r = client.post(f"/tasks/{task_id}/approve",
                        json={"prompt": "rm -rf / --no-preserve-root"})
    assert r.status_code == 200
    body = r.json()
    assert body["approved"] is False
    assert body["status"] == "awaiting_approval"
    assert "仍未通过" in body["error"]
    rec = store.get(task_id)
    assert rec.status == "awaiting_approval"           # 维持挂起
    approval = rec.meta["approval"]
    assert approval["status"] == "pending"
    assert approval["verdict"]["reason"] == "仍然危险"  # 裁决详情已更新
    assert approval["revised_prompt"] == "rm -rf / --no-preserve-root"
    assert bodies == []                                 # 未产生真实派发
    evts = [e for e in _audit_events(tmp_path) if e["stage"] == "approve"]
    assert evts and evts[-1]["decision"] == "block"    # 重检不通过也留痕


def test_reject_marks_failed_and_audits(tmp_path, monkeypatch):
    """拒绝：终态 failed + 审计留痕；拒绝后不可再 approve。"""
    client, store, bodies, task_id = _approval_client(tmp_path, monkeypatch)
    r = client.post(f"/tasks/{task_id}/reject", json={"reason": "危险操作，不予执行"},
                    headers={"X-Operator": "op-2"})
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "failed"
    assert body["error"] == "危险操作，不予执行"
    rec = store.get(task_id)
    approval = rec.meta["approval"]
    assert approval["status"] == "rejected"
    assert approval["rejected_by"] == "op-2"
    assert approval["reject_reason"] == "危险操作，不予执行"
    assert bodies == []                                # 拒绝不产生派发
    evts = [e for e in _audit_events(tmp_path) if e["stage"] == "approve"]
    assert evts and evts[-1]["decision"] == "reject"
    assert evts[-1]["operator"] == "op-2"
    # 已终态：再 approve/reject 都拒绝
    assert client.post(f"/tasks/{task_id}/approve", json={}).status_code == 400
    assert client.post(f"/tasks/{task_id}/reject", json={"reason": "x"}).status_code == 400


def test_redispatch_and_retry_blocked_for_awaiting_approval(tmp_path, monkeypatch):
    """待审批任务不参与重试/重派：必须先走人工裁决。"""
    client, store, _, task_id = _approval_client(tmp_path, monkeypatch)
    r = client.post(f"/tasks/{task_id}/redispatch", json={"model": None})
    assert r.status_code == 400
    assert "待审批" in r.json()["detail"]
    # retry 端点同理：awaiting_approval 非 failed/lost，不满足重试条件
    r2 = client.post(f"/tasks/{task_id}/retry")
    assert r2.status_code == 400


# ── approve/reject TOCTOU 并发防护与响应契约 ──

def test_approve_concurrent_only_dispatches_once(tmp_path, monkeypatch):
    """并发 approve 同一待审批任务：CAS 占位保证恰好派发一次，落败方 409。"""
    import threading
    from task_store import TaskStore as _TS
    client, store, bodies, task_id = _approval_client(tmp_path, monkeypatch)
    client2 = TestClient(client.app)
    results = {}

    real_transition = _TS.transition
    entered = threading.Barrier(2)  # 强制两个请求都进入 CAS 段，消除调度不确定性

    def racing_transition(self, tid, from_status, to_status):
        entered.wait()
        return real_transition(self, tid, from_status, to_status)

    monkeypatch.setattr(_TS, "transition", racing_transition)

    def call(i, c):
        r = c.post(f"/tasks/{task_id}/approve", json={},
                   headers={"X-Operator": f"op-{i}"})
        results[i] = (r.status_code, r.json())

    t1 = threading.Thread(target=call, args=(1, client))
    t2 = threading.Thread(target=call, args=(2, client2))
    t1.start(); t2.start(); t1.join(); t2.join()

    codes = [results[1][0], results[2][0]]
    assert codes.count(200) == 1
    assert codes.count(409) == 1
    assert len(bodies) == 1  # dispatcher 只被派发一次（修复前会派发两次）
    winner = results[1] if results[1][0] == 200 else results[2]
    assert winner[1]["approved"] is True


def test_approve_cas_conflict_returns_409(tmp_path, monkeypatch):
    """占位失败（已被其他请求抢先）：返回 409 且不产生派发。"""
    client, store, bodies, task_id = _approval_client(tmp_path, monkeypatch)
    monkeypatch.setattr(store, "transition", lambda *a, **k: False)
    r = client.post(f"/tasks/{task_id}/approve", json={})
    assert r.status_code == 409
    assert bodies == []
    assert store.get(task_id).status == "awaiting_approval"


def test_reject_cas_conflict_returns_409(tmp_path, monkeypatch):
    """reject 与 approve 同样持锁：CAS 失败返回 409，状态不变。"""
    client, store, _, task_id = _approval_client(tmp_path, monkeypatch)
    monkeypatch.setattr(store, "transition", lambda *a, **k: False)
    r = client.post(f"/tasks/{task_id}/reject", json={"reason": "x"})
    assert r.status_code == 409
    rec = store.get(task_id)
    assert rec.status == "awaiting_approval"
    assert rec.meta["approval"]["status"] == "pending"


def test_approve_dispatch_failure_contract_and_rollback(tmp_path, monkeypatch):
    """dispatch 抛异常：HTTP 200 + {approved:false, status:failed, error, task_id}
    契约体，任务回滚回 awaiting_approval，之后可重新裁决成功。"""
    from dispatcher import RemoteDispatcher
    real_dispatch = RemoteDispatcher.dispatch
    calls = {"n": 0}

    def flaky(self, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("无可用 executor worker")
        return real_dispatch(self, **kwargs)

    monkeypatch.setattr(RemoteDispatcher, "dispatch", flaky)
    client, store, bodies, task_id = _approval_client(tmp_path, monkeypatch)

    r = client.post(f"/tasks/{task_id}/approve", json={})
    assert r.status_code == 200          # HTTP 200 保持不变
    body = r.json()
    assert body["approved"] is False
    assert body["status"] == "failed"
    assert body["error"] == "无可用 executor worker"
    assert body["task_id"] == task_id
    # 回滚：任务回到待审批，approval 仍 pending
    rec = store.get(task_id)
    assert rec.status == "awaiting_approval"
    assert rec.meta["approval"]["status"] == "pending"

    # 回滚后可重新裁决并成功放行
    r2 = client.post(f"/tasks/{task_id}/approve", json={})
    assert r2.status_code == 200
    assert r2.json()["approved"] is True
    assert store.get(task_id).status == "done"


def test_redispatch_rejects_rejected_approval(tmp_path, monkeypatch):
    """被人工拒绝的指令不可经 redispatch 绕过安全关卡（与 retry 同检查）。"""
    client, store, bodies, task_id = _approval_client(tmp_path, monkeypatch)
    r = client.post(f"/tasks/{task_id}/reject", json={"reason": "危险操作"})
    assert r.status_code == 200
    assert store.get(task_id).meta["approval"]["status"] == "rejected"

    r2 = client.post(f"/tasks/{task_id}/redispatch", json={})
    assert r2.status_code == 400
    assert "拒绝" in r2.json()["detail"]
    assert bodies == []  # 未产生任何真实派发


def test_redispatch_blocked_for_dispatching(tmp_path, monkeypatch):
    """dispatching（approve 派发占位中）任务不可重派。"""
    client, store, _, task_id = _approval_client(tmp_path, monkeypatch)
    assert store.transition(task_id, "awaiting_approval", "dispatching") is True
    r = client.post(f"/tasks/{task_id}/redispatch", json={})
    assert r.status_code == 400
    assert "重派" in r.json()["detail"]


def test_callback_rejected_for_dispatching_and_awaiting(tmp_path):
    """迟到 callback 不得改写 dispatching/awaiting_approval/终态任务（修复前
    仅拦截 done/failed，cancelled 等可被迟到回调复活）。"""
    import asyncio
    from async_dispatcher import AsyncRemoteDispatcher
    reg = WorkerRegistry(heartbeat_timeout=45, now_fn=lambda: 0.0)
    store = TaskStore(str(tmp_path))
    cfg = Config(mode="remote", task_store_dir=str(tmp_path), security_llm=False)

    async def fake_async_http(method, url, **kwargs):
        return {"status_code": 200, "json": {}}

    async_disp = AsyncRemoteDispatcher(
        task_store=store, registry=reg, async_http=fake_async_http,
        now_fn=lambda: "t0",
    )
    client = TestClient(create_brain_app(reg, store, cfg, async_dispatcher=async_disp))

    for st in ("dispatching", "awaiting_approval", "cancelled", "lost", "failed"):
        store.write(TaskRecord(
            task_id=f"t-{st}", role="executor", prompt="x", worker_id="A",
            status=st, created_at="t", updated_at="t"))
        r = client.post(f"/tasks/t-{st}/callback",
                        json={"status": "done", "output": "late result"})
        assert r.status_code == 200, st
        assert r.json()["handled"] is False, st
        assert store.get(f"t-{st}").status == st, st  # 状态未被改写

    # 对照组：running 任务正常接收回调
    store.write(TaskRecord(
        task_id="t-running", role="executor", prompt="x", worker_id="A",
        status="running", created_at="t", updated_at="t"))
    r = client.post("/tasks/t-running/callback",
                    json={"status": "done", "output": "ok"})
    assert r.json()["handled"] is True
    assert store.get("t-running").status == "done"


def test_dispatch_async_security_check_does_not_block_loop(tmp_path, monkeypatch):
    """async 端点的安全检验挪到线程池：慢检验（阻塞型 LLM）期间事件循环
    仍可调度其他协程；未修复时同步调用会阻塞整个 loop，心跳协程无法推进。"""
    import asyncio
    import time as _time
    import httpx
    from async_dispatcher import AsyncRemoteDispatcher
    from unittest.mock import patch

    reg = WorkerRegistry(heartbeat_timeout=45, now_fn=lambda: 0.0)
    reg.register(WorkerInfo(worker_id="A", role="executor", host="127.0.0.1",
                            port=8001, capabilities={}, last_heartbeat=0.0))
    store = TaskStore(str(tmp_path))
    cfg = Config(mode="remote", task_store_dir=str(tmp_path), security_llm=False)

    async def fake_async_http(method, url, **kwargs):
        return {"status_code": 200, "json": {"status": "accepted"}}

    async_disp = AsyncRemoteDispatcher(
        task_store=store, registry=reg, async_http=fake_async_http,
        now_fn=lambda: "t0", poll_interval=0.01, poll_max=2,
    )
    app = create_brain_app(reg, store, cfg, async_dispatcher=async_disp)

    def slow_check(*a, **k):
        _time.sleep(0.6)  # 模拟阻塞型 security_llm 调用
        return {"verdict": "pass", "reason": "ok", "matched_rules": [],
                "suggestion": "", "llm_enhanced": False}

    async def scenario():
        ticks = {"n": 0}

        async def heartbeat():
            while True:
                await asyncio.sleep(0.05)
                ticks["n"] += 1

        hb = asyncio.create_task(heartbeat())
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as ac:
            with patch("brain_routers.security_check", slow_check):
                r = await ac.post("/dispatch/async",
                                  json={"prompt": "hello", "role": "executor"})
        hb.cancel()
        try:
            await hb
        except asyncio.CancelledError:
            pass
        return r, ticks["n"]

    r, ticks = asyncio.run(scenario())
    assert r.status_code == 200
    assert r.json()["status"] == "accepted"
    # 慢检验期间心跳协程持续被调度（0.6s / 0.05s ≈ 12 次）；
    # 阻塞在事件循环里时 ticks 接近 0
    assert ticks >= 5, f"事件循环疑似被阻塞：0.6s 内仅调度 {ticks} 次"


# ---------------------------------------------------------------- P0 本地执行配置对等

def test_build_local_core_config_parity(tmp_path, monkeypatch):
    """P0 配置对等：本地执行核心配置走 load_agent_config，转发 brain
    已解析的目录，并携带 CLI 同款 model / 守卫 / 纠错开关（不再手搭遗漏）。"""
    import brain_api
    # 显式设定环境变量，避免仓库 .env / 外部环境干扰断言（monkeypatch 先于
    # load_dotenv 写入 os.environ，load_dotenv(override=False) 不会覆盖）
    monkeypatch.setenv("AGENT_MODEL", "sonnet")
    monkeypatch.setenv("ORCH_PERMISSION_MODE", "default")
    monkeypatch.setenv("ORCH_MAX_TURNS", "42")
    monkeypatch.setenv("AGENT_CORRECTION_ENABLED", "1")
    monkeypatch.setenv("AGENT_OUTPUT_GUARD_ENABLED", "1")
    cfg = Config(mode="local",
                 memory_core_dir=str(tmp_path / "mc"),
                 memory_context_dir=str(tmp_path / "mx"),
                 experience_store_dir=str(tmp_path / "exp"))
    core = brain_api._build_local_core_config(cfg)
    # brain 解析的目录被原样转发（与调度器其他组件共用存储位置）
    assert core.memory_core_dir == str(tmp_path / "mc")
    assert core.memory_context_dir == str(tmp_path / "mx")
    assert core.experience_store_dir == str(tmp_path / "exp")
    # ORCH_* 兜底仍生效
    assert core.permission_mode == "default"
    assert core.max_turns == 42
    # CLI 同款字段不再被手搭配置遗漏：model / 守卫 / 纠错 / 规划 / rag
    assert core.model == "sonnet"
    assert core.correction_enabled is True
    assert core.output_guard_enabled is True
    assert hasattr(core, "planning_enabled")
    assert hasattr(core, "rag_enabled")
    assert hasattr(core, "task_budget_usd")



# ── 定点派发（worker_id）──

def test_dispatch_pinned_targets_worker(tmp_path):
    """worker_id 定点：POST 打到目标节点，TaskRecord 落 worker_id + meta.pinned。"""
    from dispatcher import RemoteDispatcher
    from worker_registry import WorkerInfo
    reg = WorkerRegistry(heartbeat_timeout=45, now_fn=lambda: 0.0)
    reg.register(WorkerInfo(worker_id="A", role="executor", host="127.0.0.1",
                            port=8001, capabilities={}, last_heartbeat=0.0))
    reg.register(WorkerInfo(worker_id="B", role="executor", host="10.0.0.5",
                            port=8002, capabilities={}, last_heartbeat=0.0))
    store = TaskStore(str(tmp_path))
    cfg = Config(mode="remote", task_store_dir=str(tmp_path), security_llm=False)
    seen = {"post": []}

    def fake_http(method, url, **kwargs):
        if method == "POST":
            seen["post"].append(url)
            return {"status_code": 200, "json": {"status": "accepted"}}
        return {"status_code": 200, "json": {"status": "done", "output": "ok",
                                             "artifacts": [], "error": None}}

    disp = RemoteDispatcher(task_store=store, registry=reg, http_call=fake_http,
                            now_fn=lambda: "t0")
    client = TestClient(create_brain_app(reg, store, cfg, dispatcher=disp))
    r = client.post("/dispatch", json={"prompt": "hello", "role": "executor",
                                       "worker_id": "B", "force": True})
    assert r.status_code == 200
    assert r.json()["status"] == "done"
    assert seen["post"][0] == "http://10.0.0.5:8002/task"
    rec = store.get(r.json()["task_id"])
    assert rec.worker_id == "B"
    assert rec.meta.get("pinned") is True


def test_dispatch_pinned_dead_fails_no_task(tmp_path):
    """定点到心跳超时节点：status failed 报错且不产生新任务。"""
    from dispatcher import RemoteDispatcher
    times = [100.0]

    def now_fn():
        return times[0]

    reg = WorkerRegistry(heartbeat_timeout=45, now_fn=now_fn)
    reg.register(WorkerInfo(worker_id="A", role="executor", host="127.0.0.1",
                            port=8001, capabilities={}, last_heartbeat=0.0))
    times[0] = 200.0  # 超时
    store = TaskStore(str(tmp_path))
    cfg = Config(mode="remote", task_store_dir=str(tmp_path), security_llm=False)
    disp = RemoteDispatcher(task_store=store, registry=reg,
                            http_call=lambda *a, **k: {"status_code": 200, "json": {}},
                            now_fn=lambda: "t0")
    client = TestClient(create_brain_app(reg, store, cfg, dispatcher=disp))
    r = client.post("/dispatch", json={"prompt": "hello", "role": "executor",
                                       "worker_id": "A", "force": True})
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "failed"
    assert "心跳超时" in body["error"]
    assert store.list_all() == []  # 零任务落库


def test_dispatch_pinned_unknown_fails(tmp_path):
    """定点到从未注册的节点：status failed 且错误信息说明从未注册。"""
    from dispatcher import RemoteDispatcher
    reg = WorkerRegistry(heartbeat_timeout=45, now_fn=lambda: 0.0)
    reg.register(WorkerInfo(worker_id="A", role="executor", host="127.0.0.1",
                            port=8001, capabilities={}, last_heartbeat=0.0))
    store = TaskStore(str(tmp_path))
    cfg = Config(mode="remote", task_store_dir=str(tmp_path), security_llm=False)
    disp = RemoteDispatcher(task_store=store, registry=reg,
                            http_call=lambda *a, **k: {"status_code": 200, "json": {}},
                            now_fn=lambda: "t0")
    client = TestClient(create_brain_app(reg, store, cfg, dispatcher=disp))
    r = client.post("/dispatch", json={"prompt": "hello", "role": "executor",
                                       "worker_id": "NOPE", "force": True})
    assert r.json()["status"] == "failed"
    assert "从未注册" in r.json()["error"]


def test_dispatch_pinned_malformed_id_400(tmp_path):
    """非法 worker_id 格式：400（validate_id 拒绝，不走 dispatcher）。"""
    client, _, _ = make_app_remote(tmp_path)
    r = client.post("/dispatch", json={"prompt": "hello", "role": "executor",
                                       "worker_id": "A/B/../etc", "force": True})
    assert r.status_code == 400


def test_dispatch_async_pinned_targets_worker(tmp_path):
    """异步定点派发：POST 打到目标节点并落 pinned。"""
    import asyncio
    from async_dispatcher import AsyncRemoteDispatcher
    reg = WorkerRegistry(heartbeat_timeout=45, now_fn=lambda: 0.0)
    reg.register(WorkerInfo(worker_id="A", role="executor", host="127.0.0.1",
                            port=8001, capabilities={}, last_heartbeat=0.0))
    reg.register(WorkerInfo(worker_id="B", role="executor", host="10.0.0.9",
                            port=8003, capabilities={}, last_heartbeat=0.0))
    store = TaskStore(str(tmp_path))
    cfg = Config(mode="remote", task_store_dir=str(tmp_path), security_llm=False)
    seen = {"post": []}

    async def fake_async_http(method, url, **kwargs):
        if method == "POST":
            seen["post"].append(url)
        return {"status_code": 200, "json": {"status": "accepted"}}

    async_disp = AsyncRemoteDispatcher(
        task_store=store, registry=reg, async_http=fake_async_http,
        now_fn=lambda: "t0")
    client = TestClient(create_brain_app(reg, store, cfg, async_dispatcher=async_disp))
    r = client.post("/dispatch/async", json={"prompt": "hello", "role": "executor",
                                             "worker_id": "B", "force": True})
    assert r.status_code == 200
    assert r.json()["status"] == "accepted"
    assert seen["post"][0] == "http://10.0.0.9:8003/task"
    rec = store.get(r.json()["task_id"])
    assert rec.worker_id == "B"
    assert rec.meta.get("pinned") is True


def test_approval_restores_pinned_worker(tmp_path, monkeypatch):
    """审批连续性：被拦截的定点派发，批准后仍打到原定点节点。"""
    from dispatcher import RemoteDispatcher
    from unittest.mock import patch
    reg = WorkerRegistry(heartbeat_timeout=45, now_fn=lambda: 0.0)
    reg.register(WorkerInfo(worker_id="A", role="executor", host="127.0.0.1",
                            port=8001, capabilities={}, last_heartbeat=0.0))
    reg.register(WorkerInfo(worker_id="B", role="executor", host="10.0.0.5",
                            port=8002, capabilities={}, last_heartbeat=0.0))
    store = TaskStore(str(tmp_path))
    cfg = Config(mode="remote", task_store_dir=str(tmp_path), security_llm=False)
    seen = {"post": []}

    def fake_http(method, url, **kwargs):
        if method == "POST":
            seen["post"].append(url)
            return {"status_code": 200, "json": {"status": "accepted"}}
        return {"status_code": 200, "json": {"status": "done", "output": "ok",
                                             "artifacts": [], "error": None}}

    disp = RemoteDispatcher(task_store=store, registry=reg, http_call=fake_http,
                            now_fn=lambda: "t0")
    client = TestClient(create_brain_app(reg, store, cfg, dispatcher=disp))
    with patch("brain_routers.security_check",
               return_value={"verdict": "block", "reason": "dangerous"}):
        r = client.post("/dispatch", json={"prompt": "rm -rf /", "role": "executor",
                                           "worker_id": "B"})
    assert r.json()["status"] == "awaiting_approval"
    task_id = r.json()["task_id"]
    assert store.get(task_id).meta["approval"]["worker_id"] == "B"
    assert seen["post"] == []  # 拦截未产生真实派发
    r2 = client.post(f"/tasks/{task_id}/approve", json={})
    assert r2.status_code == 200
    assert r2.json()["approved"] is True
    assert seen["post"][0] == "http://10.0.0.5:8002/task"  # approve 恢复原定点


# ---- 运行时 Provider：池管理 + 派发路由 + 密钥不落盘 ----

def _provider_client(tmp_path, *, default_provider=""):
    """带 ProviderStore 与捕获派发器的 brain app。返回 (client, captured, store)。"""
    from providers import ProviderStore
    from dispatcher import Dispatcher, DispatchResult
    captured = {"provider": None, "model": None}

    class Cap(Dispatcher):
        def dispatch(self, role, prompt, context_memory=None, model=None,
                     use_experience=True, enable_subagents=None,
                     worker_id=None, provider=None):
            captured["provider"] = provider
            captured["model"] = model
            return DispatchResult("task-pr", "done", output="ok")

        def redispatch(self, task_id, model=None, worker_id=None, provider=None):
            raise KeyError(task_id)

    reg = WorkerRegistry(heartbeat_timeout=45, now_fn=lambda: 0.0)
    store = TaskStore(str(tmp_path / "tasks"))
    cfg = Config(mode="remote", task_store_dir=str(tmp_path / "tasks"),
                 security_llm=False, default_provider=default_provider)
    pstore = ProviderStore(tmp_path / "providers.json", env={"K": "real-tok"})
    app = create_brain_app(reg, store, cfg, dispatcher=Cap(),
                           provider_store=pstore)
    return TestClient(app), captured, pstore, store


def test_providers_crud_and_masked(tmp_path):
    client, _, pstore, _ = _provider_client(tmp_path)
    # upsert（明文密钥 + ${ENV} 引用）
    r = client.put("/providers", json={
        "name": "gw", "model": "sonnet",
        "base_url": "https://gw.example.com/v1", "auth_token": "${K}"})
    assert r.status_code == 200
    # GET 回显池：auth_token 已遮蔽、不含明文和引用原文
    view = client.get("/providers").json()
    assert set(view["providers"].keys()) == {"gw"}
    assert view["providers"]["gw"]["auth_token"].startswith("***")
    assert "real-tok" not in view["providers"]["gw"]["auth_token"]
    # 设默认 + 查询
    assert client.put("/providers/default", json={"name": "gw"}).json()["default"] == "gw"
    assert client.get("/providers/default").json()["default"] == "gw"
    # 未知默认 → 404
    assert client.put("/providers/default", json={"name": "ghost"}).status_code == 404
    # 删除 → 404
    assert client.delete("/providers/gw").json()["status"] == "deleted"
    assert client.delete("/providers/gw").status_code == 404


def test_dispatch_routes_provider_and_resolves_env(tmp_path):
    client, captured, pstore, _ = _provider_client(tmp_path)
    pstore.upsert("gw", model="sonnet", base_url="https://gw.example.com",
                  auth_token="sk-${K}")
    r = client.post("/dispatch", json={
        "prompt": "分析一下", "role": "executor", "model": None,
        "provider": "gw"})
    assert r.status_code == 200
    assert r.json()["provider"] == "gw"
    p = captured["provider"]
    # brain 解析后的三元组（${ENV} 已替换）进入派发器
    assert p["name"] == "gw"
    assert p["model"] == "sonnet"
    assert p["base_url"] == "https://gw.example.com"
    assert p["auth_token"] == "sk-real-tok"


def test_dispatch_provider_unknown_400(tmp_path):
    client, _, _, _ = _provider_client(tmp_path)
    r = client.post("/dispatch", json={
        "prompt": "hi", "role": "executor", "model": None, "provider": "ghost"})
    assert r.status_code == 400
    assert "ghost" in r.json()["detail"]


def test_dispatch_default_provider_applied(tmp_path):
    client, captured, pstore, _ = _provider_client(
        tmp_path, default_provider="gw")
    pstore.upsert("gw", model="haiku", base_url="")
    r = client.post("/dispatch", json={"prompt": "hi", "role": "executor"})
    assert r.status_code == 200 and r.json()["provider"] == "gw"
    # 默认 provider 的 model 覆盖（任务未显式指定）
    assert captured["model"] == "haiku"


def test_remote_dispatch_meta_never_contains_token(tmp_path):
    """真实 RemoteDispatcher：TaskRecord.meta 只含 provider 名，三元组/密钥只走瞬时 body。

    这是「密钥不落盘」的关键保证——TaskRecord 明文写 .tasks/*.json。
    """
    import json
    from dispatcher import RemoteDispatcher
    sent: dict = {}

    def fake_http(method, url, **kw):
        if method == "POST":
            sent["body"] = kw.get("json") or {}
            return {"status_code": 200}
        return {"status_code": 200,
                "json": {"status": "done", "output": "ok", "meta": {}}}

    reg = WorkerRegistry(heartbeat_timeout=45, now_fn=lambda: 0.0)
    reg.register(WorkerInfo(worker_id="w1", role="executor", host="127.0.0.1",
                            port=8001, capabilities={}, last_heartbeat=0.0, load=0))
    store = TaskStore(str(tmp_path / "tasks"))
    disp = RemoteDispatcher(task_store=store, registry=reg, http_call=fake_http,
                            max_retries=0)
    disp.dispatch(role="executor", prompt="hi", model="sonnet",
                  provider={"name": "gw", "model": "sonnet",
                            "base_url": "https://gw.example.com",
                            "auth_token": "top-secret-token"})
    # 瞬时 body 确实带三元组（worker 本次任务执行用；不落任何持久化）
    assert sent["body"]["provider"] == "gw"
    assert sent["body"]["provider_base_url"] == "https://gw.example.com"
    assert sent["body"]["provider_auth_token"] == "top-secret-token"
    # 落盘的 TaskRecord.meta：只有 provider 名 + model，绝无 token/端点
    rec = store.get(sent["body"]["task_id"])
    meta = rec.meta if isinstance(rec.meta, dict) else {}
    assert meta.get("provider") == "gw" and meta.get("model") == "sonnet"
    dumped = json.dumps(meta, ensure_ascii=False)
    assert "top-secret-token" not in dumped
    assert "gw.example.com" not in dumped
