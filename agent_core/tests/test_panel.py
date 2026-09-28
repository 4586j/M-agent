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
    # 工作区隔离到 tmp（模块级全局，测试间必须重置）
    ws_dir = tmp_path / "ws"
    ws_dir.mkdir(parents=True, exist_ok=True)
    (ws_dir / "sub").mkdir(exist_ok=True)
    monkeypatch.setattr(service, "_WORKSPACE", {"path": ws_dir.resolve()})
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


# ---- 内嵌终端（WebSocket） ----

def test_terminal_ws_requires_auth(client, monkeypatch):
    """配了 AGENT_API_KEY 时，无 key 连接应被拒（4401）。"""
    monkeypatch.setenv("AGENT_API_KEY", "k1")
    with pytest.raises(Exception):
        with client.websocket_connect("/agent/terminal") as ws:
            ws.receive_json()


def test_terminal_ws_run_and_cd_persist(client, monkeypatch):
    """终端跑命令并保持会话 cwd（cd 特判）。"""
    monkeypatch.setenv("AGENT_API_KEY", "k1")
    with client.websocket_connect("/agent/terminal?api_key=k1") as ws:
        ready = ws.receive_json()
        assert ready["type"] == "ready"
        assert ready["cwd"]
        assert ready["shell"]

        def run(cmd):
            ws.send_json({"type": "run", "cmd": cmd})
            out, done = "", None
            while True:
                m = ws.receive_json()
                if m["type"] == "output":
                    out += m["data"]
                elif m["type"] == "error":
                    out += m["message"]
                elif m["type"] == "done":
                    done = m
                    break
            return out, done

        out, done = run("echo term_ok")
        assert done["code"] == 0 and "term_ok" in out

        before = done["cwd"]
        _, done = run("cd sub")           # 工作区内切换
        assert done["cwd"] != before
        assert done["cwd"].endswith("sub")
        after = done["cwd"]
        _, done = run("cd .")
        assert done["cwd"] == after       # 会话 cwd 持久


def test_terminal_ws_bad_cd_reports_error(client, monkeypatch):
    monkeypatch.setenv("AGENT_API_KEY", "k1")
    with client.websocket_connect("/agent/terminal?api_key=k1") as ws:
        ws.receive_json()
        ws.send_json({"type": "run", "cmd": "cd no_such_dir_xyz"})
        msgs = []
        while True:
            m = ws.receive_json()
            msgs.append(m)
            if m["type"] == "done":
                break
        assert any(m["type"] == "error" for m in msgs)
        assert msgs[-1]["code"] == 1


def test_terminal_ws_ping(client, monkeypatch):
    monkeypatch.setenv("AGENT_API_KEY", "k1")
    with client.websocket_connect("/agent/terminal?api_key=k1") as ws:
        ws.receive_json()
        ws.send_json({"type": "ping"})
        assert ws.receive_json()["type"] == "pong"


def test_panel_has_terminal_ui(client):
    r = client.get("/panel")
    assert "⌨ 终端" in r.text
    assert "switchView" in r.text and "termRun" in r.text


# ---- 工作区边界 ----

def test_workspace_get_and_set(client, tmp_path):
    ws = client.get("/agent/workspace").json()
    assert ws["path"] and ws["exists"] is True
    other = tmp_path / "other_ws"
    other.mkdir()
    r = client.put("/agent/workspace", json={"path": str(other)})
    assert r.status_code == 200 and r.json()["path"].endswith("other_ws")
    assert client.put("/agent/workspace",
                      json={"path": str(tmp_path / "nope")}).status_code == 400


def test_terminal_blocks_outside_path(client, monkeypatch, tmp_path):
    """命令涉及工作区外绝对路径 → 回 confirm 帧而非直接执行。"""
    monkeypatch.setenv("AGENT_API_KEY", "k1")
    outside = tmp_path / "outside_dir"
    outside.mkdir(exist_ok=True)
    with client.websocket_connect("/agent/terminal?api_key=k1") as ws:
        ready = ws.receive_json()
        assert ready["workspace"]  # ready 帧带工作区
        ws.send_json({"type": "run", "cmd": f"dir {outside}"})
        msgs = []
        while True:
            m = ws.receive_json()
            msgs.append(m)
            if m["type"] == "done":
                break
        confirm = next((m for m in msgs if m["type"] == "confirm"), None)
        assert confirm is not None, "越界命令应触发 confirm"
        assert confirm["kind"] == "path"
        assert any("outside_dir" in p for p in confirm["paths"])

        # 用户同意（allow_once）后应真正执行
        ws.send_json({"type": "run", "cmd": f"dir {outside}", "allow_once": True})
        msgs = []
        while True:
            m = ws.receive_json()
            msgs.append(m)
            if m["type"] == "done":
                break
        assert msgs[-1]["code"] == 0
        assert not any(m["type"] == "confirm" for m in msgs)


def test_terminal_blocks_cd_outside(client, monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_API_KEY", "k1")
    with client.websocket_connect("/agent/terminal?api_key=k1") as ws:
        ws.receive_json()
        ws.send_json({"type": "run", "cmd": f"cd {tmp_path}"})
        msgs = []
        while True:
            m = ws.receive_json()
            msgs.append(m)
            if m["type"] == "done":
                break
        confirm = next((m for m in msgs if m["type"] == "confirm"), None)
        assert confirm is not None and confirm["kind"] == "cd"


def test_terminal_allow_session(client, monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_API_KEY", "k1")
    outside = tmp_path / "o2"
    outside.mkdir(exist_ok=True)
    with client.websocket_connect("/agent/terminal?api_key=k1") as ws:
        ws.receive_json()
        ws.send_json({"type": "run", "cmd": f"cd {outside}",
                      "allow_once": True, "allow_session": True})
        while True:
            m = ws.receive_json()
            if m["type"] == "done":
                break
        # 会话级授权后，后续越界命令不再 confirm
        ws.send_json({"type": "run", "cmd": f"cd {tmp_path}"})
        msgs = []
        while True:
            m = ws.receive_json()
            msgs.append(m)
            if m["type"] == "done":
                break
        assert not any(m["type"] == "confirm" for m in msgs)
        assert msgs[-1]["code"] == 0


def test_prepare_cwd_rejects_outside():
    """显式 cwd 越界 → HTTP 400（服务层直接拒绝）。"""
    import agent_core.service as service
    from fastapi import HTTPException
    import pytest as _pytest
    with _pytest.raises(HTTPException) as ei:
        service._prepare_cwd(str(Path(service._workspace_path()).parent), tag="x")
    assert ei.value.status_code == 400
