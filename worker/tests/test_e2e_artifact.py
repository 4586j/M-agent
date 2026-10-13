"""端到端：真实 HTTP 起 brain+worker，验证产物代理链路（防穿越 / 二进制 base64 / 认证）。"""
import base64
import importlib
import os
import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parent.parent / "workspace" / "agent" / "orchestrator" / "runtime"))

requests = pytest.importorskip("requests")
uvicorn = pytest.importorskip("uvicorn")

from agent_runner import FakeAgentRunner  # noqa: E402
from worker_config import WorkerConfig  # noqa: E402
import worker_api  # noqa: E402
import brain_api  # noqa: E402

BRAIN_PORT = 8900
WORKER_PORT = 8901
KEY = "test-key-123"


@pytest.fixture(scope="module")
def e2e(tmp_path_factory):
    work_dir = str(tmp_path_factory.mktemp("e2e_work"))
    # 保存旧值，恢复时避免环境变量泄露到其他测试模块。
    # 加固：try/finally 包裹 setup+yield（module 级 fixture 无法用 function 级
    # monkeypatch fixture），setup 中途失败（如服务未就绪 pytest.fail）也能恢复
    # 环境，杜绝 ORCH_MODE 等泄露到其他测试模块的跨模块 flake。
    _saved = {k: os.environ.get(k) for k in ("ORCH_API_KEY", "ORCH_MODE", "ORCH_WORK_DIR", "ORCH_BUDGETS_FILE")}
    os.environ["ORCH_API_KEY"] = KEY
    os.environ["ORCH_MODE"] = "remote"
    os.environ["ORCH_WORK_DIR"] = work_dir
    os.environ["ORCH_BUDGETS_FILE"] = str(Path(work_dir) / "budgets.json")
    try:
        wapp = worker_api.create_worker_app(
            FakeAgentRunner(work_dir),
            WorkerConfig(work_dir=work_dir, auto_register=False, api_key=KEY))
        bapp = brain_api._build_app()

        def _run(app, port):
            uvicorn.run(app, host="127.0.0.1", port=port, log_level="error")

        threads = [threading.Thread(target=_run, args=(wapp, WORKER_PORT), daemon=True),
                   threading.Thread(target=_run, args=(bapp, BRAIN_PORT), daemon=True)]
        for t in threads:
            t.start()
        deadline = time.time() + 10
        import httpx
        while time.time() < deadline:
            try:
                httpx.get(f"http://127.0.0.1:{BRAIN_PORT}/stats", timeout=0.5)
                break
            except Exception:
                time.sleep(0.2)
        else:
            pytest.fail("brain 服务未在 10s 内就绪")

        headers = {"X-API-Key": KEY}
        r = requests.post(f"http://127.0.0.1:{BRAIN_PORT}/register", json={})
        assert r.status_code == 401, "无 API Key 的注册应被拒绝"
        r = requests.post(f"http://127.0.0.1:{BRAIN_PORT}/register", json={
            "worker_id": "w-e2e", "role": "executor", "host": "127.0.0.1",
            "port": WORKER_PORT, "capabilities": {}}, headers=headers)
        assert r.status_code == 200

        task_dir = Path(work_dir) / "task-e2e"
        task_dir.mkdir(parents=True, exist_ok=True)
        binary = b"\x89PNG\r\n\x1a\n\x00\xff\xfe" * 4
        (task_dir / "chart.png").write_bytes(binary)
        (task_dir / "report.md").write_text("# 报告", encoding="utf-8")
        victim = Path(work_dir) / "victim-task"
        victim.mkdir(exist_ok=True)
        (victim / "secret.md").write_text("secret", encoding="utf-8")

        def proxy(path, task_id="task-e2e"):
            return requests.get(
                f"http://127.0.0.1:{BRAIN_PORT}/proxy/artifact"
                f"?worker_id=w-e2e&task_id={task_id}&path={path}", headers=headers)

        yield {"proxy": proxy, "binary": binary}
    finally:
        for k, v in _saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def test_e2e_text_artifact(e2e):
    r = e2e["proxy"]("report.md")
    assert r.status_code == 200
    assert r.json()["encoding"] == "utf-8"
    assert "# 报告" in r.json()["content"]


def test_e2e_binary_artifact_base64_roundtrip(e2e):
    r = e2e["proxy"]("chart.png")
    assert r.status_code == 200
    body = r.json()
    assert body["encoding"] == "base64"
    assert base64.b64decode(body["content"]) == e2e["binary"]


def test_e2e_cross_task_traversal_rejected(e2e):
    r = e2e["proxy"]("../victim-task/secret.md")
    assert r.status_code == 400


def test_e2e_bad_task_id_rejected(e2e):
    r = e2e["proxy"]("x", task_id="..")
    assert r.status_code == 400
