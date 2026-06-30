import sys
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
    # 先放一个文件再拉
    p = Path(tmp_path) / "task-1" / "report.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("报告内容", encoding="utf-8")
    r = client.get("/artifact/task-1?path=task-1/report.md")
    assert r.status_code == 200
    assert "报告内容" in r.text
