"""Agent 面板与运行事件持久化测试。"""
import base64
import json
import os
import shutil
import sys
import threading
import time
from dataclasses import replace
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
    monkeypatch.setattr(service, "_TITLE_USAGE", {"calls": 0, "errors": 0,
        "unknown_cost_calls": 0, "total_cost_usd": 0.0, "total_turns": 0, "usage": {}})
    monkeypatch.setattr(service, "_HEALTH_CHECKS", None)
    # 限流表是模块级全局：重置避免本文件请求量污染后续测试文件（429）
    monkeypatch.setattr(service, "_RATE_LIMIT_STORE", {})
    monkeypatch.setattr(service, "_RATE_LIMIT_CALLS", 0, raising=False)
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


@pytest.mark.skipif(os.name != "nt", reason="Windows shell/encoding integration")
@pytest.mark.parametrize("shell", [None, "pwsh.exe", "powershell.exe", "cmd.exe"])
def test_terminal_windows_unicode_and_actual_shell(client, monkeypatch, shell):
    """真实 shell + WebSocket：中文文件、中文 stdout/stderr、shell 覆盖和退出码。"""
    if shell and not shutil.which(shell):
        pytest.skip(f"{shell} unavailable")
    monkeypatch.setenv("AGENT_API_KEY", "k1")
    if shell:
        monkeypatch.setenv("AGENT_TERMINAL_SHELL", shell)
    else:
        monkeypatch.delenv("AGENT_TERMINAL_SHELL", raising=False)
    filename = "中文文件名.md"
    (service._workspace_path() / filename).write_text("测试", encoding="utf-8")
    with client.websocket_connect("/agent/terminal?api_key=k1") as ws:
        ready = ws.receive_json()
        actual_shell = Path(ready["shell"]).stem.lower()
        if shell:
            assert actual_shell == Path(shell).stem.lower()
        else:
            assert actual_shell in {"pwsh", "powershell"}

        def run(command):
            ws.send_json({"type": "run", "cmd": command})
            output = ""
            while True:
                message = ws.receive_json()
                if message["type"] == "output":
                    output += message["data"]
                elif message["type"] == "done":
                    return output, message["code"]
                elif message["type"] in {"error", "confirm"}:
                    pytest.fail(str(message))

        output, code = run("dir" if actual_shell == "cmd" else "ls")
        assert code == 0 and filename in output and "\ufffd" not in output
        output, code = run("echo 中文输出")
        assert code == 0 and "中文输出" in output and "\ufffd" not in output
        output, code = run('python -c "print(chr(20013)+chr(25991))"')
        assert code == 0 and "中文" in output and "\ufffd" not in output
        output, code = run("不存在的命令_terminal_test")
        assert code != 0 and "不存在的命令_terminal_test" in output
        assert "\ufffd" not in output and "#< CLIXML" not in output
        _, code = run("exit 7")
        assert code == 7


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


# ---- 策略配置化（.agent_policy.json） ----

@pytest.fixture()
def isolated_policy_file(client, monkeypatch, tmp_path):
    """在独立项目根测试策略；即使进程中断，也不会污染开发配置。"""
    policy_root = tmp_path / "policy-project"
    policy_root.mkdir()
    cfg = replace(service.get_config(), project_root=str(policy_root))
    monkeypatch.setattr(service, "get_config", lambda: cfg)
    monkeypatch.setattr(service, "_POLICY", None)
    return policy_root / ".agent_policy.json"


def test_policy_enforce_false_disables_boundary(client, monkeypatch, tmp_path,
                                                isolated_policy_file):
    """enforce=false 时终端越界直接放行（总开关生效）。"""
    monkeypatch.setenv("AGENT_API_KEY", "k1")
    monkeypatch.setattr(service, "_POLICY", None)  # 重置缓存
    isolated_policy_file.write_text(json.dumps(
        {"workspace_boundary": {"enforce": False}}), encoding="utf-8")
    outside = tmp_path / "pol_outside"
    outside.mkdir(exist_ok=True)
    with client.websocket_connect("/agent/terminal?api_key=k1") as ws:
        ws.receive_json()
        ws.send_json({"type": "run", "cmd": f"cd {outside}"})
        msgs = []
        while True:
            m = ws.receive_json()
            msgs.append(m)
            if m["type"] == "done":
                break
        assert not any(m["type"] == "confirm" for m in msgs)
        assert msgs[-1]["code"] == 0


def test_policy_extra_allowed_dirs_passthrough(client, monkeypatch, tmp_path,
                                               isolated_policy_file):
    """extra_allowed_dirs 预授权目录：命令涉及该目录不再逐次确认。"""
    monkeypatch.setenv("AGENT_API_KEY", "k1")
    monkeypatch.setattr(service, "_POLICY", None)
    outside_parent = tmp_path / "pol_parent"
    outside_parent.mkdir(exist_ok=True)
    isolated_policy_file.write_text(json.dumps(
        {"workspace_boundary": {"extra_allowed_dirs": [str(outside_parent)]}}),
        encoding="utf-8")
    with client.websocket_connect("/agent/terminal?api_key=k1") as ws:
        ws.receive_json()
        ws.send_json({"type": "run", "cmd": f"dir {outside_parent}"})
        msgs = []
        while True:
            m = ws.receive_json()
            msgs.append(m)
            if m["type"] == "done":
                break
        assert not any(m["type"] == "confirm" for m in msgs)


def test_policy_granularity_in_confirm_frame(client, monkeypatch, isolated_policy_file):
    """confirm 帧携带策略 granularity（per_action 默认）。"""
    monkeypatch.setenv("AGENT_API_KEY", "k1")
    with client.websocket_connect("/agent/terminal?api_key=k1") as ws:
        ws.receive_json()
        ws.send_json({"type": "run", "cmd": "cd .."})
        confirm = None
        while True:
            m = ws.receive_json()
            if m["type"] == "confirm":
                confirm = m
            if m["type"] == "done":
                break
        assert confirm and confirm["granularity"] == "per_action"


def test_policy_file_corruption_warns_and_falls_back(client, monkeypatch,
                                                     isolated_policy_file, caplog):
    """策略文件损坏 → 回退默认（边界保持 enforce），并发出 warning（不再静默）。"""
    monkeypatch.setattr(service, "_POLICY", None)
    isolated_policy_file.write_text("{ 不是 JSON", encoding="utf-8")
    import logging as _logging
    with caplog.at_level(_logging.WARNING, logger="agent_core.service"):
        policy = service._load_policy()
    assert policy["workspace_boundary"]["enforce"] is True
    assert any("解析失败" in r.message for r in caplog.records)


# ---- 持久会话（连续对话：落盘 + 历史注入） ----


@pytest.mark.parametrize("policy", [
    None, [], "policy", 1,
    {"workspace_boundary": None}, {"workspace_boundary": []},
    {"workspace_boundary": {"enforce": "false"}},
    {"workspace_boundary": {"enforce": 0}},
    {"workspace_boundary": {"scan_command_paths": 1}},
    {"workspace_boundary": {"confirm_granularity": "never"}},
    {"workspace_boundary": {"confirm_granularity": None}},
    {"workspace_boundary": {"extra_allowed_dirs": "C:/trusted"}},
    {"workspace_boundary": {"extra_allowed_dirs": None}},
    {"workspace_boundary": {"extra_allowed_dirs": [1]}},
    {"workspace_boundary": {"extra_allowed_dirs": [""]}},
    {"workspace_boundary": {"extra_allowed_dirs": ["relative/path"]}},
    {"workspace_boundary": {"enforce": False, "scan_command_paths": "false"}},
])
def test_policy_invalid_structure_falls_back(policy, isolated_policy_file, caplog):
    """合法 JSON 的结构/类型错误也整体回退，不留下部分放宽的策略。"""
    isolated_policy_file.write_text(json.dumps(policy), encoding="utf-8")
    with caplog.at_level("WARNING", logger="agent_core.service"):
        loaded = service._load_policy()
    assert loaded == service._POLICY_DEFAULT
    assert any("解析失败" in record.message for record in caplog.records)


def test_policy_valid_partial_settings_preserve_defaults(isolated_policy_file, tmp_path, caplog):
    """合法的 false、确认粒度和绝对目录不被类型校验误拒。"""
    settings = {"enforce": False, "confirm_granularity": "per_session",
                "extra_allowed_dirs": [str(tmp_path.resolve())], "unknown": "ignored"}
    isolated_policy_file.write_text(json.dumps({"workspace_boundary": settings}), encoding="utf-8")
    loaded = service._load_policy()["workspace_boundary"]
    assert loaded["enforce"] is False
    assert loaded["scan_command_paths"] is True
    assert loaded["confirm_granularity"] == "per_session"
    assert loaded["extra_allowed_dirs"] == [str(tmp_path.resolve())]
    assert "unknown" not in loaded
    assert not any("解析失败" in record.message for record in caplog.records)

def test_persisted_session_endpoints(client):
    """创建 → 列表 → 详情（空轮次）→ 删除 → 404。"""
    r = client.post("/agent/session/persist")
    assert r.status_code == 200
    sid = r.json()["session_id"]
    assert len(sid) == 12
    lst = client.get("/agent/persisted-sessions").json()
    assert [s["session_id"] for s in lst["sessions"]] == [sid]
    detail = client.get(f"/agent/persisted-sessions/{sid}").json()
    assert detail["turns"] == []
    assert client.delete(f"/agent/persisted-sessions/{sid}").json()["status"] == "deleted"
    assert client.get(f"/agent/persisted-sessions/{sid}").status_code == 404


def test_delete_session_preserves_runs_and_rejects_late_save(client):
    sid = client.post("/agent/session/persist").json()["session_id"]
    other = client.post("/agent/session/persist").json()["session_id"]
    with client.stream("POST", "/agent/run/stream",
                       json={"prompt": "保留执行记录", "session_id": sid}) as response:
        "".join(response.iter_text())
    stale = service._load_session(sid)
    runs = client.get("/agent/runs").json()["runs"]
    artifact = Path(runs[0]["cwd"]) / "keep.txt"
    artifact.write_text("保留产物", encoding="utf-8")
    assert client.delete(f"/agent/persisted-sessions/{sid}").status_code == 200
    with pytest.raises(service.HTTPException) as exc:
        service._save_session(stale, require_existing=True)
    assert exc.value.status_code == 404
    assert client.get(f"/agent/persisted-sessions/{sid}").status_code == 404
    assert client.get(f"/agent/persisted-sessions/{other}").status_code == 200
    assert client.get("/agent/runs").json()["runs"] == runs
    assert artifact.read_text(encoding="utf-8") == "保留产物"


def test_deleted_session_not_recreated_by_running_async_task(client, monkeypatch):
    sid = client.post("/agent/session/persist").json()["session_id"]
    started, finish = threading.Event(), threading.Event()
    original = client.kernel.run_oneshot

    def delayed(*args, **kwargs):
        started.set()
        assert finish.wait(5)
        return original(*args, **kwargs)

    monkeypatch.setattr(client.kernel, "run_oneshot", delayed)
    response = client.post("/agent/run/async", json={"prompt": "正在执行", "session_id": sid})
    rid = response.json()["run_id"]
    try:
        assert started.wait(3)
        assert client.delete(f"/agent/persisted-sessions/{sid}").status_code == 200
    finally:
        finish.set()
    deadline = time.monotonic() + 5
    while not service._ASYNC_RUNS[rid]["task"].done() and time.monotonic() < deadline:
        time.sleep(0.02)
    assert service._ASYNC_RUNS[rid]["task"].done()
    assert client.get(f"/agent/run/{rid}").json()["status"] == "completed"
    assert client.get(f"/agent/persisted-sessions/{sid}").status_code == 404


def test_persisted_session_rejects_bad_sid(client):
    assert client.get("/agent/persisted-sessions/../evil").status_code in (400, 404)
    assert client.get("/agent/persisted-sessions/ZZZ").status_code in (400, 404)


def test_run_stream_with_session_persists_turn(client, monkeypatch):
    """带 session_id 的流式轮：结束后 prompt/output 落盘会话文件。"""
    r = client.post("/agent/session/persist")
    sid = r.json()["session_id"]
    with client.stream("POST", "/agent/run/stream",
                       json={"prompt": "第一问", "session_id": sid}) as resp:
        "".join(resp.iter_text())
    detail = client.get(f"/agent/persisted-sessions/{sid}").json()
    assert len(detail["turns"]) == 1
    turn = detail["turns"][0]
    assert turn["prompt"] == "第一问"
    assert turn["status"] == "completed"
    assert "完成" in str(turn["output"])  # FakeKernel result 输出
    assert detail["title"] == "第一问"
    # run meta 也带 session_id（回放端定位）
    runs = client.get("/agent/runs").json()["runs"]
    assert any(r.get("session_id") == sid for r in runs)


@pytest.mark.parametrize("fails", [False, True])
def test_planning_session_context_model_and_persistence(client, monkeypatch, fails):
    sid = client.post("/agent/session/persist").json()["session_id"]
    with client.stream("POST", "/agent/run/stream",
                       json={"prompt": "此前需求", "session_id": sid}) as response:
        "".join(response.iter_text())
    seen = {}

    def kernel_factory(config):
        seen["planning"] = config.planning_enabled
        return client.kernel

    original = client.kernel.run_oneshot

    def run(prompt, persona, cwd, context_memory="", *args, **kwargs):
        seen.update(prompt=prompt, model=persona.model, context=context_memory)
        if fails:
            raise RuntimeError("fixture planner failure")
        return original(prompt, persona, cwd, context_memory, *args, **kwargs)

    monkeypatch.setattr(service, "AgentKernel", kernel_factory)
    monkeypatch.setattr(client.kernel, "run_oneshot", run)
    response = client.post("/agent/run/async", json={"prompt": "继续制定并执行计划",
        "planning_enabled": True, "session_id": sid, "model": "test-model",
        "context_memory": "额外要求"})
    assert response.status_code == 202
    rid = response.json()["run_id"]
    deadline = time.monotonic() + 5
    turns = []
    while time.monotonic() < deadline:
        turns = client.get(f"/agent/persisted-sessions/{sid}").json()["turns"]
        if len(turns) == 2:
            break
        time.sleep(0.02)
    assert len(turns) == 2
    assert seen["planning"] and seen["model"] == "test-model"
    assert "此前需求" in seen["context"] and "额外要求" in seen["context"]
    assert turns[-1]["run_id"] == rid
    assert turns[-1]["status"] == ("failed" if fails else "completed")
    if not fails:
        assert turns[-1]["output"] == "最终输出"
    assert any(r.get("run_id") == rid and r.get("session_id") == sid
               for r in client.get("/agent/runs").json()["runs"])


def test_async_missing_session_rejected_before_execution(client):
    response = client.post("/agent/run/async", json={"prompt": "不得执行",
        "session_id": "012345abcdef", "planning_enabled": True})
    assert response.status_code == 404
    assert not client.kernel.oneshot_calls


def test_stream_model_override_preserves_cached_persona(client, monkeypatch):
    original = client.kernel.stream
    cached = service.resolve_persona("executor")
    previous_model = cached.model
    seen = []

    def stream(prompt, persona, *args, **kwargs):
        seen.append(persona.model)
        return original(prompt, persona, *args, **kwargs)

    monkeypatch.setattr(client.kernel, "stream", stream)
    with client.stream("POST", "/agent/run/stream",
                       json={"prompt": "指定模型", "model": "test-model"}) as response:
        "".join(response.iter_text())
    assert seen == ["test-model"]
    assert cached.model == previous_model


def test_run_stream_session_history_injected(client, monkeypatch):
    """第二次带同 session_id 的轮：内核收到的 context_memory 携带历史块。"""
    r = client.post("/agent/session/persist")
    sid = r.json()["session_id"]
    with client.stream("POST", "/agent/run/stream",
                       json={"prompt": "第一问", "session_id": sid}) as resp:
        "".join(resp.iter_text())

    seen = {}
    kernel = client.kernel

    original_stream = kernel.stream

    def spy_stream(prompt, persona, cwd, context_memory="", run_id=None,
                   event_sink=None):
        seen["context"] = context_memory
        return original_stream(prompt, persona, cwd, context_memory,
                               run_id=run_id, event_sink=event_sink)

    monkeypatch.setattr(kernel, "stream", spy_stream)
    with client.stream("POST", "/agent/run/stream",
                       json={"prompt": "第二问", "session_id": sid}) as resp:
        "".join(resp.iter_text())
    assert "会话历史" in seen["context"]
    assert "第一问" in seen["context"]
    assert "完成" in seen["context"]  # 上一轮输出注入


def test_run_stream_without_session_no_history(client, monkeypatch):
    """不带 session_id：context_memory 原样透传（无历史块），行为不变。"""
    seen = {}
    kernel = client.kernel
    original_stream = kernel.stream

    def spy_stream(prompt, persona, cwd, context_memory="", run_id=None,
                   event_sink=None):
        seen["context"] = context_memory
        return original_stream(prompt, persona, cwd, context_memory,
                               run_id=run_id, event_sink=event_sink)

    monkeypatch.setattr(kernel, "stream", spy_stream)
    with client.stream("POST", "/agent/run/stream",
                       json={"prompt": "独立任务", "context_memory": "参考资料"}) as resp:
        "".join(resp.iter_text())
    assert seen["context"] == "参考资料"


def test_run_stream_missing_session_404(client):
    """session_id 不存在 → 早失败 404，不产生 run。"""
    r = client.post("/agent/run/stream", json={"prompt": "x", "session_id": "deadbeefdead"})
    assert r.status_code == 404


# ---- 图片附件（多模态 v1） ----

_PNG_B64 = base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"0" * 64).decode()


def test_save_run_attachments_ok(tmp_path):
    import pytest as _pytest
    cwd = tmp_path / "run-cwd"
    cwd.mkdir()
    paths = service._save_run_attachments(
        str(cwd), [f"data:image/png;base64,{_PNG_B64}",
                   f"data:image/jpeg;base64,{base64.b64encode(b'jpeg-data').decode()}"])
    assert paths[0].startswith("attachments/attach-") and paths[0].endswith(".png")
    assert paths[1].endswith(".jpg")
    assert (cwd / paths[0]).exists()
    assert (cwd / paths[1]).exists()


def test_save_run_attachments_rejects_bad_input(tmp_path):
    import pytest as _pytest
    from fastapi import HTTPException
    cwd = tmp_path / "run-cwd2"
    cwd.mkdir()
    with _pytest.raises(HTTPException) as ei:  # 非 data URL
        service._save_run_attachments(str(cwd), ["http://x/a.png"])
    assert ei.value.status_code == 400
    with _pytest.raises(HTTPException) as ei:  # 非图片 mime
        service._save_run_attachments(str(cwd), ["data:text/plain;base64,AAAA"])
    assert ei.value.status_code == 400
    with _pytest.raises(HTTPException) as ei:  # 超过 5 张
        service._save_run_attachments(
            str(cwd), [f"data:image/png;base64,{_PNG_B64}"] * 6)
    assert ei.value.status_code == 400


def test_run_stream_with_images_saves_and_mentions(client, monkeypatch):
    """带图片的流式 run：附件落盘 run cwd，内核收到的 prompt 含路径说明。"""
    seen = {}
    kernel = client.kernel
    original_stream = kernel.stream

    def spy_stream(prompt, persona, cwd, context_memory="", run_id=None,
                   event_sink=None):
        seen["prompt"] = prompt
        seen["cwd"] = cwd
        return original_stream(prompt, persona, cwd, context_memory,
                               run_id=run_id, event_sink=event_sink)

    monkeypatch.setattr(kernel, "stream", spy_stream)
    with client.stream("POST", "/agent/run/stream",
                       json={"prompt": "看看这个设计稿",
                             "images": [f"data:image/png;base64,{_PNG_B64}"]}) as resp:
        "".join(resp.iter_text())
    assert "附带 1 张图片" in seen["prompt"]
    assert "attachments/attach-" in seen["prompt"]
    assert seen["cwd"]
    saved = list((Path(seen["cwd"]) / "attachments").glob("attach-*.png"))
    assert len(saved) == 1


def test_extract_initial_title():
    from agent_core.service import _extract_initial_title
    assert _extract_initial_title("第一问") == "第一问"
    assert _extract_initial_title("你好，请帮我写一个 Python 批量转 PDF 的脚本") == "Python 批量转 PDF 的脚本"
    assert _extract_initial_title("/plan 请问能不能分析一下内存泄漏？详情见...") == "内存泄漏"
    assert _extract_initial_title("") == "新会话"


def test_persisted_session_title_patch_and_summarize(client):
    r = client.post("/agent/session/persist")
    sid = r.json()["session_id"]

    # 初始为空
    detail = client.get(f"/agent/persisted-sessions/{sid}").json()
    assert detail["title"] == ""

    # PATCH 更新标题
    resp = client.patch(f"/agent/persisted-sessions/{sid}", json={"title": "自定义任务标题"})
    assert resp.status_code == 200
    assert resp.json()["title"] == "自定义任务标题"

    detail = client.get(f"/agent/persisted-sessions/{sid}").json()
    assert detail["title"] == "自定义任务标题"

    # 空标题拒绝
    bad = client.patch(f"/agent/persisted-sessions/{sid}", json={"title": "  "})
    assert bad.status_code == 400

    # 派发一轮对话后触发 summarize-title
    with client.stream("POST", "/agent/run/stream",
                       json={"prompt": "请帮我重构用户认证模块", "session_id": sid}) as s:
        "".join(s.iter_text())

    # 触发提炼
    sum_resp = client.post(f"/agent/persisted-sessions/{sid}/summarize-title")
    assert sum_resp.status_code == 200
    assert sum_resp.json()["session_id"] == sid
    assert sum_resp.json()["title"] != ""


@pytest.mark.parametrize("manual", [False, True])
def test_auto_title_merges_latest_turn_and_preserves_manual_edit(client, monkeypatch, manual):
    import asyncio
    sid = client.post("/agent/session/persist").json()["session_id"]
    service._append_session_turn(sid, {"prompt": "需要详细讨论的第一轮需求", "output": "第一轮"})

    def summary(rec, kernel):
        service._append_session_turn(sid, {"prompt": "第二轮", "output": "继续讨论"})
        if manual:
            service._update_session_title(sid, "人工标题", manual=True)
        return "自动提炼标题"

    monkeypatch.setattr(service, "_summarize_session_title_with_llm", summary)
    asyncio.run(service._trigger_async_title_summary(sid, "需要详细讨论的第一轮需求", "第一轮"))
    rec = service._load_session(sid)
    assert len(rec["turns"]) == 2
    assert rec["title"] == ("人工标题" if manual else "自动提炼标题")


def test_explicit_summary_does_not_overwrite_concurrent_rename(client, monkeypatch):
    sid = client.post("/agent/session/persist").json()["session_id"]
    service._append_session_turn(sid, {"prompt": "需要详细讨论的第一轮需求", "output": "第一轮"})

    def summary(rec, kernel):
        service._append_session_turn(sid, {"prompt": "第二轮", "output": "继续讨论"})
        service._update_session_title(sid, "手动命名", manual=True)
        return "过时的模型标题"

    monkeypatch.setattr(service, "_summarize_session_title_with_llm", summary)
    response = client.post(f"/agent/persisted-sessions/{sid}/summarize-title")
    assert response.status_code == 200
    assert response.json()["updated"] is False
    assert response.json()["title"] == "手动命名"
    assert len(service._load_session(sid)["turns"]) == 2


def test_title_edit_survives_late_turn_and_automatic_summaries(client, monkeypatch):
    import asyncio
    sid = client.post("/agent/session/persist").json()["session_id"]
    client.patch(f"/agent/persisted-sessions/{sid}", json={"title": "自选标题"})
    service._append_session_turn(sid, {"prompt": "后来的任务执行结果", "output": "完成"})
    calls = []
    monkeypatch.setattr(service, "_summarize_session_title_with_llm", lambda *args: calls.append(args))
    asyncio.run(service._trigger_async_title_summary(sid, "后来的任务执行结果", "完成"))
    assert not calls
    assert service._load_session(sid)["title"] == "自选标题"


@pytest.mark.parametrize("cost,error", [(0.123, False), (0.05, True), (None, False)])
def test_title_summary_reports_cost_tokens_and_unknown_cost(client, monkeypatch, cost, error):
    from agent_core.kernel import KernelResult
    monkeypatch.setattr(client.kernel, "run_oneshot", lambda **kwargs: KernelResult(
        output="会话提炼标题", cost_usd=cost, is_error=error, turns=1,
        usage={"input_tokens": 25, "output_tokens": 5, "cache_read_input_tokens": 3}))
    title = service._summarize_session_title_with_llm({"turns":[
        {"prompt":"需要详细讨论的第一轮需求","output":"第一轮"}]}, client.kernel)
    assert bool(title) is not error
    usage = client.get("/agent/status").json()["title_usage"]
    assert usage["calls"] == 1 and usage["errors"] == int(error)
    assert usage["total_cost_usd"] == (cost or 0)
    assert usage["unknown_cost_calls"] == int(cost is None)
    assert usage["total_turns"] == 1
    assert usage["usage"] == {"input_tokens":25,"output_tokens":5,"cache_read_input_tokens":3}
