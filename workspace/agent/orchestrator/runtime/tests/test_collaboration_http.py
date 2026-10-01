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


def test_real_process_machine_enrollment_and_instance_claim(tmp_path):
    root = RUNTIME.parents[3]
    data = tmp_path / "collaboration"
    database = Database(data / "metadata.sqlite3")
    database.initialize()
    AuthService(database).bootstrap("admin", "管理员", "local-test-password-42")
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("AGENT_", "ORCH_", "ANTHROPIC_", "OPENAI_"))}
    env.update(AGENT_ENV_FILE=str(tmp_path / "missing.env"), PYTHONIOENCODING="utf-8",
               PYTHONPATH=os.pathsep.join((str(root), str(RUNTIME))))
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    url = f"http://127.0.0.1:{port}"
    log = (tmp_path / "server.log").open("w", encoding="utf-8")
    process = subprocess.Popen(
        [sys.executable, "-m", "collaboration", "serve", "--data-dir", str(data),
         "--port", str(port), "--origin", url],
        cwd=tmp_path, env=env, stdout=log, stderr=subprocess.STDOUT)
    try:
        with httpx.Client(base_url=url, headers={"Origin": url}, trust_env=False) as client:
            deadline = time.monotonic() + 30
            while True:
                assert process.poll() is None, "Test service exited during startup"
                assert time.monotonic() < deadline, "Test service startup timed out"
                try:
                    assert client.get("/collab/v1/me", timeout=1).status_code == 401
                    break
                except (httpx.ConnectError, httpx.ConnectTimeout):
                    time.sleep(0.05)
            enrolled = subprocess.run(
                [sys.executable, "-m", "collaboration", "node-enroll",
                 "--data-dir", str(data), "--name", "node-1"],
                cwd=tmp_path, env=env, capture_output=True, text=True, timeout=60)
            assert enrolled.returncode == 0, enrolled.stderr
            node_id, node_token = enrolled.stdout.splitlines()
            assert len(node_id) == 32 and len(node_token) == 43
            login = client.post("/collab/v1/auth/login",
                                json={"username": "admin", "password": "local-test-password-42"})
            assert login.status_code == 200
            csrf = login.json()["csrf_token"]
            user_id = login.json()["user"]["user_id"]
            agent = client.post("/collab/v1/agents", json={"name": "甲的 Agent"},
                                headers={"X-CSRF-Token": csrf})
            assert agent.status_code == 201, agent.text
            project = client.post("/collab/v1/projects", json={"name": "真项目"},
                                  headers={"X-CSRF-Token": csrf})
            assert project.status_code == 201, project.text
            pid = project.json()["project_id"]
            binding = client.post(f"/collab/v1/projects/{pid}/agent-bindings", json={
                "agent_id": agent.json()["agent_id"], "member_user_id": user_id,
                "duty": "执行", "persona_version": "v1", "allowed_tools": ["Read"],
                "workspace_ref": "workspace-a", "provider_ref": "provider-a"},
                headers={"X-CSRF-Token": csrf})
            assert binding.status_code == 201, binding.text
            bid = binding.json()["binding_id"]
            slot = client.post(f"/collab/v1/projects/{pid}/agent-bindings/{bid}/instances",
                               json={"node_id": node_id}, headers={"X-CSRF-Token": csrf})
            assert slot.status_code == 201, slot.text
            claim = client.post("/collab/v1/machine/instances/claim", json={
                "claim_token": slot.json()["claim_token"], "protocol_version": "1", "capacity": 1,
                "isolation_mode": "container", "isolation_attestation": "podman sandbox",
                "policy": {"workspace_ref": "workspace-a", "provider_ref": "provider-a",
                           "allowed_tools": ["Read"], "env_allowlist": ["PYTHONPATH"]}},
                headers={"X-Node-Token": node_token})
            assert claim.status_code == 201, claim.text
            instance_token = claim.json()["instance_token"]
            assert claim.json()["project_id"] == pid
            assert claim.json()["binding_version"] == 1
            beat = client.post("/collab/v1/machine/instances/heartbeat",
                               headers={"X-Instance-Token": instance_token})
            assert beat.status_code == 200 and beat.json()["status"] == "active"
            me = client.get("/collab/v1/machine/instances/me",
                            headers={"X-Instance-Token": instance_token})
            assert me.status_code == 200, me.text
            assert me.json()["instance_id"] == slot.json()["instance_id"]
            assert me.json()["project_id"] == pid
        stored = (data / "metadata.sqlite3").read_bytes()
        for secret in (node_token, instance_token, slot.json()["claim_token"]):
            assert secret.encode() not in stored
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=10)
        log.close()
