import base64
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from fastapi.testclient import TestClient
from worker_api import create_worker_app
from agent_runner import FakeAgentRunner
from worker_config import WorkerConfig


def make_client(tmp_path):
    runner = FakeAgentRunner(work_dir=str(tmp_path))
    cfg = WorkerConfig(work_dir=str(tmp_path))
    return TestClient(create_worker_app(runner, cfg))


def test_post_task_accepted_then_done(tmp_path):
    client = make_client(tmp_path)
    r = client.post("/task", json={
        "task_id": "task-1", "prompt": "分析数据", "context_memory": "", "model": None})
    assert r.status_code == 200
    assert r.json()["status"] == "accepted"
    # Fake 同步执行，立即查应为 done
    r2 = client.get("/task/task-1")
    assert r2.status_code == 200
    assert r2.json()["status"] == "done"


def test_post_task_failed(tmp_path):
    client = make_client(tmp_path)
    client.post("/task", json={
        "task_id": "task-2", "prompt": "FORCE_ERROR boom", "context_memory": "", "model": None})
    r = client.get("/task/task-2")
    assert r.json()["status"] == "failed"
    assert r.json()["error"] is not None


def test_get_missing_task(tmp_path):
    client = make_client(tmp_path)
    r = client.get("/task/nope")
    assert r.status_code == 404


def test_artifact_endpoint_returns_file(tmp_path):
    client = make_client(tmp_path)
    # 产物落在 work_dir/{task_id}/ 下，path 相对该子目录
    p = Path(tmp_path) / "task-1" / "report.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("报告内容", encoding="utf-8")
    r = client.get("/artifact/task-1?path=report.md")
    assert r.status_code == 200
    body = r.json()
    assert body["encoding"] == "utf-8"
    assert "报告内容" in body["content"]


def test_artifact_rejects_cross_task_traversal(tmp_path):
    client = make_client(tmp_path)
    victim = Path(tmp_path) / "other-task" / "secret.md"
    victim.parent.mkdir(parents=True, exist_ok=True)
    victim.write_text("secret", encoding="utf-8")
    r = client.get("/artifact/task-1?path=../other-task/secret.md")
    assert r.status_code == 400
    r = client.get("/artifact/task-1?path=..%2Fother-task%2Fsecret.md")
    assert r.status_code == 400


def test_artifact_rejects_bad_task_id(tmp_path):
    client = make_client(tmp_path)
    # %2E%2E 解码后为 ".."，能到达 handler 被 validate_id 拒绝；
    # 含斜杠的编码（..%2Fevil）则直接被路由层拒绝（404）
    r = client.get("/artifact/%2E%2E?path=report.md")
    assert r.status_code == 400
    r = client.get("/artifact/..%2Fevil?path=report.md")
    assert r.status_code in (400, 404)


def test_artifact_directory_not_readable(tmp_path):
    client = make_client(tmp_path)
    d = Path(tmp_path) / "task-1" / "subdir"
    d.mkdir(parents=True, exist_ok=True)
    r = client.get("/artifact/task-1?path=subdir")
    assert r.status_code == 404


def test_artifact_binary_returns_base64(tmp_path):
    client = make_client(tmp_path)
    p = Path(tmp_path) / "task-1" / "chart.png"
    p.parent.mkdir(parents=True, exist_ok=True)
    data = b"\x89PNG\r\n\x1a\n" + b"\x00\x01\x02" * 8
    p.write_bytes(data)
    r = client.get("/artifact/task-1?path=chart.png")
    assert r.status_code == 200
    body = r.json()
    assert body["encoding"] == "base64"
    assert base64.b64decode(body["content"]) == data
    assert body["size"] == len(data)


def test_artifact_oversize_rejected(tmp_path):
    runner = FakeAgentRunner(work_dir=str(tmp_path))
    cfg = WorkerConfig(work_dir=str(tmp_path), artifact_max_bytes=16)
    client = TestClient(create_worker_app(runner, cfg))
    p = Path(tmp_path) / "task-1" / "big.bin"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"x" * 100)
    r = client.get("/artifact/task-1?path=big.bin")
    assert r.status_code == 413


def _wait_terminal(client, task_id, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        r = client.get(f"/task/{task_id}")
        if r.status_code == 404 or r.json().get("status") != "running":
            return
        time.sleep(0.02)


def test_state_capped_for_finished_tasks(tmp_path, monkeypatch):
    monkeypatch.setenv("ORCH_WORKER_STATE_MAX", "3")
    client = make_client(tmp_path)
    for i in range(5):
        client.post("/task", json={
            "task_id": f"task-{i}", "prompt": "p", "context_memory": "", "model": None})
    _wait_terminal(client, "task-4")
    assert client.get("/task/task-0").status_code == 404  # 最老的被清理
    assert client.get("/task/task-1").status_code == 404
    assert client.get("/task/task-4").status_code == 200


def test_running_count_reported_as_load(tmp_path):
    started = threading.Event()
    release = threading.Event()

    class SlowRunner(FakeAgentRunner):
        def run(self, req):
            started.set()
            release.wait(5)
            return super().run(req)

    app = create_worker_app(SlowRunner(work_dir=str(tmp_path)),
                            WorkerConfig(work_dir=str(tmp_path)))
    assert app.state.get_load() == 0
    client = TestClient(app)
    client.post("/task", json={
        "task_id": "task-1", "prompt": "p", "context_memory": "", "model": None})
    assert started.wait(5)
    assert app.state.get_load() == 1
    release.set()
    _wait_terminal(client, "task-1")
    assert app.state.get_load() == 0


def test_cancel_keeps_cancelled_status(tmp_path):
    """取消竞态保护：执行体跑完后，终态保持 cancelled，不被 done 覆盖。"""
    started = threading.Event()
    release = threading.Event()

    class SlowRunner(FakeAgentRunner):
        def run(self, req):
            started.set()
            release.wait(5)
            return super().run(req)

    client = TestClient(create_worker_app(SlowRunner(work_dir=str(tmp_path)),
                                          WorkerConfig(work_dir=str(tmp_path))))
    client.post("/task", json={
        "task_id": "task-1", "prompt": "p", "context_memory": "", "model": None})
    assert started.wait(5)
    client.post("/task/task-1/cancel")
    release.set()
    _wait_terminal(client, "task-1")
    body = client.get("/task/task-1").json()
    assert body["status"] == "cancelled"


def test_cancel_event_propagates_to_runner(tmp_path):
    """cancel 端点应 set runner 收到的 cancel_event（协作式退出信号）。"""
    started = threading.Event()
    release = threading.Event()
    seen = {}

    class EventRunner(FakeAgentRunner):
        def run(self, req):
            seen["event"] = req.cancel_event
            started.set()
            release.wait(5)
            return super().run(req)

    client = TestClient(create_worker_app(EventRunner(work_dir=str(tmp_path)),
                                          WorkerConfig(work_dir=str(tmp_path))))
    client.post("/task", json={
        "task_id": "task-1", "prompt": "p", "context_memory": "", "model": None})
    assert started.wait(5)
    assert seen["event"] is not None and not seen["event"].is_set()
    client.post("/task/task-1/cancel")
    assert seen["event"].is_set()
    release.set()
    _wait_terminal(client, "task-1")


def test_runner_cancelled_result_reported_as_is(tmp_path):
    """runner 自身返回 cancelled（协作退出成功）时，状态如实上报。"""

    class CoopRunner(FakeAgentRunner):
        def run(self, req):
            from agent_runner import TaskResult
            return TaskResult(req.task_id, "cancelled", error="cancelled by request")

    client = TestClient(create_worker_app(CoopRunner(work_dir=str(tmp_path)),
                                          WorkerConfig(work_dir=str(tmp_path))))
    client.post("/task", json={
        "task_id": "task-1", "prompt": "p", "context_memory": "", "model": None})
    _wait_terminal(client, "task-1")
    assert client.get("/task/task-1").json()["status"] == "cancelled"


def test_task_meta_returned(tmp_path):
    client = make_client(tmp_path)
    client.post("/task", json={
        "task_id": "task-1", "prompt": "p", "context_memory": "", "model": None})
    _wait_terminal(client, "task-1")
    body = client.get("/task/task-1").json()
    assert "meta" in body  # 新增的执行元信息字段（Fake 为空 dict）


def test_register_to_brain_success():
    from worker_api import register_to_brain
    from worker_config import WorkerConfig
    cfg = WorkerConfig(brain_url="http://brain", worker_id="w1", role="executor",
                       listen_host="127.0.0.1", listen_port=8001)

    class FakeResp:
        status_code = 200

    calls = []

    def fake_post(url, json=None, timeout=None, headers=None):
        calls.append((url, json))
        return FakeResp()

    assert register_to_brain(cfg, http_post=fake_post) is True
    assert calls[0][0] == "http://brain/register"
    assert calls[0][1]["worker_id"] == "w1"
    assert calls[0][1]["port"] == 8001


def test_register_to_brain_retries_then_fails(monkeypatch):
    import worker_api
    from worker_api import register_to_brain
    from worker_config import WorkerConfig
    monkeypatch.setattr(worker_api.time, "sleep", lambda *a, **k: None)
    cfg = WorkerConfig(brain_url="http://brain", worker_id="w1")

    def fake_post(url, json=None, timeout=None, headers=None):
        raise ConnectionError("no brain")

    assert register_to_brain(cfg, http_post=fake_post) is False

