import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from starlette.websockets import WebSocketDisconnect  # noqa: E402

# ---------------------------------------------------------------------------
# 工具：用 FakePanelExecutor（不烧 LLM）构造带面板的 worker app
# ---------------------------------------------------------------------------


class FakePanelExecutor:
    """面板 run 执行替身：预设事件流 / oneshot 结果，仍经 sink 写事件 JSONL。"""

    def __init__(self, stream_plan=None, oneshot_result=None):
        self.seq = 0
        # text_delta 事件 data 为纯字符串（与真实内核一致，agent_panel
        # 的 _event_payload 对 text_delta 会 str() 非字符串数据）
        self.stream_plan = stream_plan or [
            ("text_delta", "你好"),
            ("result", {"result": "ok", "is_error": False, "num_turns": 1,
                        "cost_usd": 0.0, "duration_ms": 5}),
        ]
        self.oneshot_result = oneshot_result or {
            "result": "ok", "is_error": False, "num_turns": 2,
            "cost_usd": 0.01, "duration_ms": 10,
        }

    def _emit(self, sink, run_id, etype, data):
        self.seq += 1
        sink(run_id, {"seq": self.seq, "ts": time.time(), "type": etype, "data": data})

    def stream_events(self, *, prompt, persona, cwd, run_id, sink):
        from agent_core.kernel import AgentEvent

        async def gen():
            for etype, data in self.stream_plan:
                self._emit(sink, run_id, etype, data)
                yield AgentEvent(etype, data)

        return gen()

    def oneshot(self, *, prompt, persona, cwd, cancel_event, run_id, sink):
        self._emit(sink, run_id, "result", self.oneshot_result)
        return dict(self.oneshot_result), "completed"


def make_app(tmp_path, *, api_key: str = "", worker_panel: bool = False,
             executor: FakePanelExecutor | None = None):
    """构造 worker app，默认只挂 FakePanelExecutor 路由（不触碰真实内核）。

    worker_panel=False 时 create_worker_app 不自动挂 KernelPanelExecutor 路由，
    FakePanelExecutor 再作为唯一挂载点——避免真实内核路由先行匹配（首匹配生效）
    而导致测试误烧 LLM。

    仅测试异步后台 run 时用 `with make_app(...) as client`：裸 TestClient 的
    每次请求新建/销毁 portal，create_task 的后台任务会随请求循环关闭而提前
    夭折，状态永远停在 running。其余端点单请求内完成，裸客户端即可。
    """
    from agent_panel import build_agent_panel_router
    from worker_api import create_worker_app
    from agent_runner import FakeAgentRunner
    from worker_config import WorkerConfig

    runner = FakeAgentRunner(work_dir=str(tmp_path))
    cfg = WorkerConfig(work_dir=str(tmp_path), api_key=api_key,
                       worker_panel=worker_panel)
    app = create_worker_app(runner, cfg)
    if not worker_panel:
        app.include_router(build_agent_panel_router(
            cfg, executor=executor or FakePanelExecutor()))
    return TestClient(app)


def poll_run(client, run_id, timeout=5.0):
    """轮询异步 run 直到终态。调用方须用 `with make_app(...) as client`
    保持常驻事件循环（裸 TestClient 的 per-request portal 会杀死后台任务）。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        r = client.get(f"/agent/run/{run_id}")
        if r.status_code == 200 and r.json()["status"] in ("completed", "failed", "cancelled"):
            return r.json()
        time.sleep(0.02)
    raise AssertionError(f"run {run_id} 未在 {timeout}s 内终态")


# ---------------------------------------------------------------------------
# 静态面板页
# ---------------------------------------------------------------------------


def test_panel_page_served(tmp_path):
    client = make_app(tmp_path)
    r = client.get("/panel")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]
    r2 = client.get("/", follow_redirects=False)
    assert r2.status_code in (302, 307)
    assert "/panel" in r2.headers["location"]


def test_panel_disabled_returns_404(tmp_path):
    from worker_api import create_worker_app
    from agent_runner import FakeAgentRunner
    from worker_config import WorkerConfig
    cfg = WorkerConfig(work_dir=str(tmp_path), worker_panel=False)
    app = create_worker_app(FakeAgentRunner(work_dir=str(tmp_path)), cfg)
    assert TestClient(app).get("/panel").status_code == 404


def test_worker_panel_auto_mounted_by_default(tmp_path):
    """create_worker_app 默认挂面板路由（/panel 可达，静态页零内核调用）。"""
    from worker_api import create_worker_app
    from agent_runner import FakeAgentRunner
    from worker_config import WorkerConfig
    cfg = WorkerConfig(work_dir=str(tmp_path), worker_panel=True)
    app = create_worker_app(FakeAgentRunner(work_dir=str(tmp_path)), cfg)
    r = TestClient(app).get("/panel")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]


def test_worker_panel_config_from_env(monkeypatch, tmp_path):
    from worker_config import load_worker_config
    monkeypatch.setenv("ORCH_WORK_PANEL", "cleared")  # 与正确名字无关，仅清环境
    monkeypatch.delenv("ORCH_WORKER_PANEL", raising=False)
    assert load_worker_config().worker_panel is True
    monkeypatch.setenv("ORCH_WORKER_PANEL", "0")
    assert load_worker_config().worker_panel is False


# ---------------------------------------------------------------------------
# 运行端点契约
# ---------------------------------------------------------------------------


def test_runs_empty_and_unknown_404(tmp_path):
    client = make_app(tmp_path)
    assert client.get("/agent/runs").json() == {"runs": []}
    assert client.get("/agent/run/nope").status_code == 404
    assert client.delete("/agent/run/nope").status_code == 404


def test_async_run_replay_and_events(tmp_path):
    # 裸 TestClient 每次请求新建/销毁级 portal 事件循环，后台 asyncio 任务
    # 会随 POST 循环关闭而夭折；with 进入后整个测试共享同一个常驻循环
    # （与 agent_core/tests/test_service.py 的 fixture 一致），create_task
    # 才能在轮询的 GET 之间被调度。
    with make_app(tmp_path) as client:
        r = client.post("/agent/run/async", json={"prompt": "hi"})
        assert r.status_code == 202
        run_id = r.json()["run_id"]
        assert r.json()["status"] == "running"

        done = poll_run(client, run_id)
        assert done["status"] == "completed"
        assert done["result"]["result"] == "ok"
        assert done["result"]["num_turns"] == 2

        events = client.get(f"/agent/run/{run_id}/events").json()
        assert events and events[-1]["type"] == "result"
        assert all("seq" in e for e in events)
        # 终态任务触发 cancel 无副作用
        assert client.delete(f"/agent/run/{run_id}").json()["cancel_requested"] is True


def test_stream_sse_frames(tmp_path):
    client = make_app(tmp_path)
    r = client.post("/agent/run/stream", json={"prompt": "hi"})
    assert r.status_code == 200
    assert "text/event-stream" in r.headers["content-type"]
    text = r.text
    assert "event: text_delta" in text
    assert "event: result" in text
    assert '"text": "你好"' in text


def test_sse_truncates_long_payload(tmp_path):
    huge = "x" * 5000
    plan = [("text_delta", huge), ("result", {"result": "ok", "is_error": False})]
    client = make_app(tmp_path, executor=FakePanelExecutor(stream_plan=plan))
    body = client.post("/agent/run/stream", json={"prompt": "hi"}).text
    assert f'"text": "{"x"*5000}"' not in body
    assert "...(truncated)" in body


# ---------------------------------------------------------------------------
# 工作区
# ---------------------------------------------------------------------------


def test_workspace_get_put(tmp_path):
    client = make_app(tmp_path)
    ws = client.get("/agent/workspace").json()
    assert Path(ws["path"]) == Path(tmp_path).resolve()
    assert ws["exists"] is True

    # 不存在目录 → 400
    r = client.put("/agent/workspace", json={"path": str(tmp_path / "no-such-dir")})
    assert r.status_code == 400

    # 合法子目录 → 切换成功
    sub = tmp_path / "sub"
    sub.mkdir()
    r = client.put("/agent/workspace", json={"path": str(sub)})
    assert r.status_code == 200
    assert Path(r.json()["path"]) == sub.resolve()


# ---------------------------------------------------------------------------
# 节点状态（技能 / MCP / 工作区 / run 计数；编排面板经 brain 代理拉取）
# ---------------------------------------------------------------------------


def test_agent_status_reports_skills_mcp_workspace(tmp_path):
    """/agent/status 汇报 scan_skills + load_mcp_servers 解析结果与当前工作区。"""
    # 技能：project_root/workspace/agent/skills/<name>/SKILL.md（scan_skills 扫描点）
    proj = tmp_path.parent
    skill_dir = proj / "workspace" / "agent" / "skills" / "demo-skill"
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: demo-skill\ndescription: 演示技能\n"
        "triggers:\n  - 演示\n---\n正文\n", encoding="utf-8")
    # MCP：临时 JSON（load_mcp_servers 读 ORCH/AGENT_MCP_CONFIG 指向的文件，
    # 格式与 .mcp.json 一致：mcpServers 映射）
    mcp_json = tmp_path / "mcp.json"
    mcp_json.write_text(json.dumps({"mcpServers": {
        "alpha": {"command": "python", "args": ["-m", "demo"]}}}), encoding="utf-8")

    from worker_config import WorkerConfig
    from worker_api import create_worker_app
    from agent_panel import build_agent_panel_router
    from agent_runner import FakeAgentRunner
    cfg = WorkerConfig(work_dir=str(tmp_path), project_root=str(proj),
                       mcp_config_path=str(mcp_json))
    app = create_worker_app(FakeAgentRunner(work_dir=str(tmp_path)), cfg)
    app.include_router(build_agent_panel_router(cfg, executor=FakePanelExecutor()))
    client = TestClient(app)

    r = client.get("/agent/status")
    assert r.status_code == 200
    data = r.json()
    assert data["worker_id"] == cfg.worker_id
    assert data["role"] == cfg.role
    assert data["workspace"]["path"] == str(Path(tmp_path).resolve())
    assert data["workspace"]["exists"] is True
    assert any(s["name"] == "demo-skill"
               and s["triggers"] == ["演示"] for s in data["skills"])
    assert any(m["name"] == "alpha" and m["command"] == "python"
               for m in data["mcp_servers"])
    assert data["runs"] == {"running": 0, "total": 0}


def test_agent_status_auth(tmp_path):
    """状态归属 /agent/*：无 key → 401，?api_key= 放行。"""
    client = make_app(tmp_path, api_key="secret")
    assert client.get("/agent/status").status_code == 401
    assert client.get("/agent/status?api_key=secret").status_code == 200
    # 未配 key：环回直通
    assert make_app(tmp_path).get("/agent/status").status_code == 200


# ---------------------------------------------------------------------------
# 内嵌终端 WS
# ---------------------------------------------------------------------------


def test_terminal_ready_and_command(tmp_path):
    client = make_app(tmp_path)
    with client.websocket_connect("/agent/terminal") as ws:
        ready = ws.receive_json()
        assert ready["type"] == "ready"
        assert Path(ready["cwd"]) == Path(tmp_path).resolve()
        assert ready["workspace"] == str(Path(tmp_path).resolve())
        assert ready["shell"]

        ws.send_json({"type": "run", "cmd": "echo hi"})
        frames = [ws.receive_json(), ws.receive_json()]
        done = frames[-1]
        assert done["type"] == "done"
        assert done["code"] == 0

        ws.send_json({"type": "ping"})
        assert ws.receive_json()["type"] == "pong"


def test_terminal_confirm_workspace_boundary(tmp_path):
    """越界 cd 先回 confirm 帧，不执行。"""
    client = make_app(tmp_path)
    outside = tmp_path.parent.resolve()  # 工作区之外
    with client.websocket_connect("/agent/terminal") as ws:
        ws.receive_json()  # ready
        ws.send_json({"type": "run", "cmd": f"cd {outside}"})
        frames = [ws.receive_json(), ws.receive_json()]
        confirm = frames[0]
        assert confirm["type"] == "confirm"
        assert confirm["kind"] == "cd"
        assert frames[1]["type"] == "done" and frames[1]["code"] == 1


# ---------------------------------------------------------------------------
# 鉴权
# ---------------------------------------------------------------------------


def test_auth_api_key(tmp_path):
    client = make_app(tmp_path, api_key="secret")
    # 面板页公开
    assert client.get("/panel").status_code == 200
    # /agent/* 无 key → 401
    assert client.get("/agent/runs").status_code == 401
    # ?api_key= 放行
    assert client.get("/agent/runs?api_key=secret").status_code == 200
    # WS 无 key → 拒绝握手
    with pytest.raises(WebSocketDisconnect) as excinfo:
        with client.websocket_connect("/agent/terminal"):
            pass
    # TestClient 在 close(4401) 于 accept 前抛出
    assert excinfo.value.code == 4401


def test_no_key_loopback_allows(tmp_path):
    """未配 key：环回请求直接放行（测试 client 来源视为本机）。"""
    client = make_app(tmp_path)
    assert client.get("/agent/runs").status_code == 200


# ---------------------------------------------------------------------------
# build_worker_kernel 与 SdkAgentRunner._get_kernel 配方一致
# ---------------------------------------------------------------------------


def test_build_worker_kernel_matches_runner(tmp_path):
    from agent_runner import SdkAgentRunner, build_worker_kernel
    from worker_config import WorkerConfig

    cfg = WorkerConfig(
        work_dir=str(tmp_path),
        project_root=str(tmp_path.parent),
        task_timeout=123, max_turns=7, max_thinking_tokens=0,
        task_budget_usd=2.5, permission_mode="bypassPermissions",
        experience_store_dir=str(tmp_path / "exp"),
        output_guard_enabled=False, output_guard_max_retries=3,
        fallback_model="haiku", mcp_config_path="",
    )
    runner = SdkAgentRunner(
        work_dir=str(tmp_path), persona_def_path=str(tmp_path / "p.md"),
        project_root=str(tmp_path.parent),
        task_timeout=123, max_turns=7, max_thinking_tokens=0,
        task_budget_usd=2.5, permission_mode="bypassPermissions",
        experience_store_dir=str(tmp_path / "exp"),
        output_guard_enabled=False, output_guard_max_retries=3,
        fallback_model="haiku", mcp_servers=None,
    )
    k1 = build_worker_kernel(cfg)
    k2 = runner._get_kernel()
    for field in ("max_turns", "task_timeout", "permission_mode",
                  "max_thinking_tokens", "task_budget_usd", "project_root",
                  "experience_store_dir", "output_guard_enabled",
                  "output_guard_max_retries", "fallback_model",
                  "mcp_config_path"):
        assert getattr(k1.config, field) == getattr(k2.config, field), field
    # 0 → 不限 的语义（与既有“非正数视为不限”一致）
    assert k1.config.max_thinking_tokens is None
    assert k1.config.task_budget_usd == 2.5