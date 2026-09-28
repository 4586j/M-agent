"""Agent 面板与运行事件持久化测试。"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

from agent_core.config import load_agent_config
from agent_core.kernel import _RunEventEmitter

# ---- 本地夹具：复刻 test_service.client（tmp 隔离） ----

import agent_core.service as service
from fastapi.testclient import TestClient
sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_service import FakeKernel, _default_events


@pytest.fixture
def client(monkeypatch, tmp_path, project_root):
    cfg = load_agent_config(
        project_root=project_root,
        work_dir=str(tmp_path / "work"),
        memory_backend="local",
        memory_core_dir=str(tmp_path / "mem" / "core"),
        memory_context_dir=str(tmp_path / "mem" / "context"),
        experience_store_dir=str(tmp_path / "exp"),
        mcp_config_path="",
    )
    monkeypatch.setattr(service, "get_config", lambda: cfg)
    monkeypatch.setattr(service, "_CONFIG", None, raising=False)
    kernel = FakeKernel(events=_default_events())
    monkeypatch.setattr(service, "get_kernel", lambda: kernel)
    monkeypatch.setattr(service, "_KERNEL", None, raising=False)
    monkeypatch.setattr(service, "_SESSIONS", {})
    monkeypatch.setattr(service, "_ASYNC_RUNS", {})
    monkeypatch.setattr(service, "_PERSONAS", {})
    monkeypatch.setattr(service, "_ONESHOT_TOTALS",
                        {"runs": 0, "errors": 0, "total_cost_usd": 0.0,
                         "total_turns": 0})
    monkeypatch.setattr(service, "_HEALTH_CHECKS", None)
    with TestClient(service.create_app("executor")) as c:
        c.kernel = kernel
        yield c



# ---- _RunEventEmitter 单元 ----

def test_emitter_adds_seq_and_ts():
    events = []
    em = _RunEventEmitter("run-1", lambda rid, e: events.append((rid, e)))
    em.emit("text_delta", "a")
    em.emit("tool_use", {"name": "Bash", "input": {"command": "ls"}})
    assert len(events) == 2
    rid1, e1 = events[0]
    rid2, e2 = events[1]
    assert rid1 == rid2 == "run-1"
    assert e1["seq"] == 1 and e2["seq"] == 2
    assert e1["ts"] > 0 and e2["ts"] >= e1["ts"]
    assert e1["type"] == "text_delta" and e2["type"] == "tool_use"


def test_emitter_swallows_sink_errors():
    def bad_sink(rid, e):
        raise RuntimeError("观测失效")
    em = _RunEventEmitter("run-1", bad_sink)
    em.emit("text_delta", "a")  # 不应抛出
    assert em.seq == 1


# ---- 面板托管与端点（复用 test_service 的 client fixture）----

def test_panel_served(client):
    r = client.get("/panel")
    assert r.status_code == 200
    assert "Agent 面板" in r.text
    assert "handleEvent" in r.text  # 前端事件路由函数存在


def test_root_redirects_to_panel(client):
    r = client.get("/", follow_redirects=False)
    assert r.status_code in (301, 302, 307)
    assert "/panel" in r.headers.get("location", "")


def test_runs_listed_after_stream(client, tmp_path, monkeypatch):
    # stream 一次（FakeKernel），事件 JSONL 与 meta 应落盘
    with client.stream("POST", "/agent/run/stream",
                       json={"prompt": "回放测试"}) as resp:
        "".join(resp.iter_text())
    runs = client.get("/agent/runs").json()["runs"]
    assert len(runs) >= 1
    assert runs[0]["status"] == "completed"
    assert "cwd" in runs[0]


def test_run_events_replay_matches_jsonl(client):
    with client.stream("POST", "/agent/run/stream",
                       json={"prompt": "回放测试"}) as resp:
        "".join(resp.iter_text())
    runs = client.get("/agent/runs").json()["runs"]
    run_id = runs[0]["run_id"]
    events = client.get(f"/agent/run/{run_id}/events").json()
    types = [e["type"] for e in events]
    assert "text_delta" in types and "result" in types
    seqs = [e["seq"] for e in events]
    assert seqs == sorted(seqs)  # 按序回放
    # 404：不存在的 run
    assert client.get("/agent/run/run-missing/events").status_code == 404


def test_streamed_run_enters_history(client):
    with client.stream("POST", "/agent/run/stream",
                       json={"prompt": "入历史测试"}) as resp:
        "".join(resp.iter_text())
    history = client.get("/agent/history").json()
    runs = client.get("/agent/runs").json()["runs"]
    assert runs[0]["run_id"]  # 面板数据源就绪
    # 历史条目应包含本次流式 run（修复"流式不入历史"）；
    # 响应形状 {"records": [...], "total": N, ...}
    records = history.get("records", history if isinstance(history, list) else [])
    assert any(h.get("prompt_preview", "").startswith("入历史测试") for h in records)


def test_artifact_readonly_and_traversal(client):
    with client.stream("POST", "/agent/run/stream",
                       json={"prompt": "产物测试"}) as resp:
        "".join(resp.iter_text())
    runs = client.get("/agent/runs").json()["runs"]
    run_id = runs[0]["run_id"]
    cwd = Path(runs[0]["cwd"])
    (cwd / "out.html").write_text("<html>ok</html>", encoding="utf-8")
    r = client.get(f"/agent/artifact/{run_id}", params={"path": "out.html"})
    assert r.status_code == 200
    assert "<html>ok</html>" in r.json()["content"]
    # 路径穿越拒绝
    assert client.get(f"/agent/artifact/{run_id}",
                      params={"path": "../secret.txt"}).status_code == 400
    assert client.get(f"/agent/artifact/{run_id}",
                      params={"path": "sub/../../x"}).status_code == 400
    # 不存在的产物
    assert client.get(f"/agent/artifact/{run_id}",
                      params={"path": "missing.txt"}).status_code == 404
    # 不存在的 run
    assert client.get("/agent/artifact/run-missing",
                      params={"path": "a.txt"}).status_code == 404
