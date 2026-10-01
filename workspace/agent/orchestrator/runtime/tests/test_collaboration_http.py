from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx

RUNTIME = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RUNTIME))

from collaboration.auth import AuthService
from collaboration.database import Database
from task_store import TaskRecord, TaskStore


def test_independent_http_services_do_not_share_credentials_or_data(tmp_path):
    root = RUNTIME.parents[3]
    data = tmp_path / "collaboration"
    db = Database(data / "metadata.sqlite3")
    db.initialize()
    AuthService(db).bootstrap("admin", "管理员", "local-test-password-42")
    old_tasks = tmp_path / "legacy-tasks"
    old_store = TaskStore(str(old_tasks))
    record = TaskRecord(task_id="legacy-1", role="executor", prompt="preserve",
                        worker_id=None, status="done", created_at="t0", updated_at="t1",
                        output="old output", artifacts=[], error=None)
    old_store.write(record)
    before = {path.name: path.read_bytes() for path in old_tasks.iterdir() if path.is_file()}
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("AGENT_", "ORCH_", "ANTHROPIC_", "OPENAI_"))}
    env.update(AGENT_ENV_FILE=str(tmp_path / "missing.env"), PYTHONIOENCODING="utf-8",
               PYTHONPATH=os.pathsep.join((str(root), str(RUNTIME), str(root / "worker"))),
               ORCH_MODE="local", ORCH_LOCAL_EXEC="0", ORCH_SECURITY_LLM="0",
               ORCH_API_KEY="legacy-test-key", ORCH_TASK_STORE_DIR=str(old_tasks))
    for key in ("EXPERIENCE_STORE_DIR", "SCHEDULE_STORE_DIR", "PROCEDURAL_STORE_DIR",
                "MEMORY_CORE_DIR", "MEMORY_CONTEXT_DIR", "SESSIONS_DIR", "KNOWLEDGE_DIR"):
        env["ORCH_" + key] = str(tmp_path / key.lower())
    env.update(ORCH_PROVIDERS_FILE=str(tmp_path / "providers.json"),
               ORCH_BUDGETS_FILE=str(tmp_path / "budgets.json"),
               ORCH_AUDIT_LOG=str(tmp_path / "audit.jsonl"))
    ports = []
    sockets = []
    try:
        for _ in range(2):
            sock = socket.socket()
            sock.bind(("127.0.0.1", 0))
            sockets.append(sock)
            ports.append(sock.getsockname()[1])
    finally:
        for sock in sockets:
            sock.close()
    collab_url, legacy_url = (f"http://127.0.0.1:{port}" for port in ports)
    commands = [
        [sys.executable, "-m", "collaboration", "serve", "--data-dir", str(data),
         "--port", str(ports[0]), "--origin", collab_url],
        [sys.executable, "-m", "uvicorn", "brain_api:_build_app", "--factory",
         "--host", "127.0.0.1", "--port", str(ports[1]), "--log-level", "warning"],
    ]
    processes, logs = [], []
    try:
        for index, command in enumerate(commands):
            log = (tmp_path / f"server-{index}.log").open("w", encoding="utf-8")
            logs.append(log)
            processes.append(subprocess.Popen(command, cwd=tmp_path, env=env, stdout=log, stderr=subprocess.STDOUT))
        with httpx.Client(base_url=collab_url, headers={"Origin": collab_url}, trust_env=False) as collab, \
                httpx.Client(base_url=legacy_url, trust_env=False) as legacy:
            for client, path, process in ((collab, "/collab/v1/me", processes[0]), (legacy, "/stats", processes[1])):
                deadline = time.monotonic() + 30
                while True:
                    assert process.poll() is None, "Test service exited during startup"
                    assert time.monotonic() < deadline, "Test service startup timed out"
                    try:
                        response = client.get(path, timeout=1)
                        assert response.status_code == 401
                        break
                    except (httpx.ConnectError, httpx.ConnectTimeout):
                        time.sleep(0.05)
            assert legacy.get("/stats", headers={"X-API-Key": "legacy-test-key"}).status_code == 200
            assert collab.get("/collab/v1/projects", headers={"X-API-Key": "legacy-test-key"}).status_code == 401
            response = collab.post("/collab/v1/auth/login", json={"username": "admin", "password": "local-test-password-42"})
            assert response.status_code == 200
            session = collab.get("/collab/v1/me").json()
            token = collab.cookies.get("collab_session")
            assert token
            assert legacy.get("/stats", headers={"X-API-Key": token}).status_code == 401
            assert legacy.get("/stats", headers={"Cookie": f"collab_session={token}"}).status_code == 401
            result = collab.post("/collab/v1/projects", json={"name": "独立项目"},
                                 headers={"X-CSRF-Token": session["csrf_token"]})
            assert result.status_code in (200, 201)
            assert collab.get("/collab/v1/projects").json()[0]["name"] == "独立项目"
            tasks = legacy.get("/tasks", headers={"X-API-Key": "legacy-test-key"})
            assert tasks.status_code == 200
            assert "legacy-1" in tasks.text and "独立项目" not in tasks.text
            assert collab.post("/dispatch", json={"prompt": "must not execute"}).status_code == 404
            assert {path.name: path.read_bytes() for path in old_tasks.iterdir() if path.is_file()} == before
    finally:
        for process in processes:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=10)
        for log in logs:
            log.close()
