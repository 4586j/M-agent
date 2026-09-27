"""测试 agent_core.service：HTTP 端点（SSE 流式 / 异步运行 / 取消 / 记忆 / status）。

用 FastAPI TestClient + 假内核（FakeKernel/FakeSession），不触真实 SDK、不触网。
SSE 直接解析 text/event-stream 帧断言事件序列。
"""
import asyncio
import json
import time
from dataclasses import asdict

import pytest
from fastapi.testclient import TestClient

import agent_core.service as service
from agent_core.config import load_agent_config
from agent_core.doctor import CheckResult
from agent_core.kernel import AgentEvent, KernelResult


# ---- 假内核 / 假会话 ----

class FakeSession:
    def __init__(self, events=None, sid="sdk-sess-1"):
        self._events = events or []
        self._sid = sid
        self.total_turns = 0
        self.total_cost = 0.0
        self.connected = False
        self.closed = False
        self.interrupted = False
        self.send_calls = []          # (prompt, timeout)
        self.closed_exception = None

    @property
    def session_id(self):
        return self._sid

    async def connect(self):
        self.connected = True

    async def send(self, prompt, timeout=None):
        self.send_calls.append((prompt, timeout))
        for ev in self._events:
            yield ev
            if ev.type == "result" and isinstance(ev.data, dict):
                self.total_turns += ev.data.get("num_turns") or 0
                if ev.data.get("cost_usd") is not None:
                    self.total_cost += ev.data["cost_usd"]
                if ev.data.get("session_id"):
                    self._sid = ev.data["session_id"]

    async def interrupt(self):
        self.interrupted = True

    async def close(self):
        self.closed = True


class FakeKernel:
    """假内核：预置 stream 事件与 oneshot 结果，支持取消与延迟。"""

    def __init__(self, events=None, result=None, oneshot_delay=0.0):
        self.events = events or []
        self.result = result or KernelResult(
            output="最终输出", turns=3, is_error=False,
            usage={"input_tokens": 10, "output_tokens": 5},
            cost_usd=0.05, duration_ms=120, session_id="sdk-run-1")
        self.oneshot_delay = oneshot_delay
        self.oneshot_calls = []       # (prompt, cancel_event)
        self.sessions = []

    def stream(self, prompt, persona, cwd, context_memory=""):
        events = self.events

        async def gen():
            for ev in events:
                yield ev
        return gen()

    def run_oneshot(self, prompt, persona, cwd, context_memory="",
                    cancel_event=None):
        self.oneshot_calls.append((prompt, cancel_event))
        if self.oneshot_delay:
            deadline = time.monotonic() + self.oneshot_delay
            while time.monotonic() < deadline:
                if cancel_event is not None and cancel_event.is_set():
                    break
                time.sleep(0.01)
        if cancel_event is not None and cancel_event.is_set():
            return KernelResult(output="部分输出", turns=1, cancelled=True)
        return self.result

    def open_session(self, persona, cwd):
        s = FakeSession(events=self.events)
        self.sessions.append(s)
        return s


# ---- 夹具 ----

@pytest.fixture
def cfg(tmp_path, project_root, monkeypatch):
    # project_root 用真实仓库根：记忆后端延迟导入 runtime 的 store；
    # 工作目录/记忆/经验目录全部隔离到 tmp_path，避免污染真实数据。
    # .env 隔离由 conftest 的 autouse 夹具统一负责（AGENT_ENV_FILE 哨兵路径）。
    return load_agent_config(
        project_root=project_root,
        work_dir=str(tmp_path / "work"),
        memory_backend="local",
        memory_core_dir=str(tmp_path / "mem" / "core"),
        memory_context_dir=str(tmp_path / "mem" / "context"),
        experience_store_dir=str(tmp_path / "exp"),
        mcp_config_path="",
    )


@pytest.fixture
def client(monkeypatch, cfg):
    """构造 TestClient：替换模块级 get_config/get_kernel，重置共享状态。"""
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
    from agent_core.service import create_app
    with TestClient(create_app("executor")) as c:
        c.kernel = kernel
        yield c


def _default_events():
    return [
        AgentEvent("text_delta", "你好"),
        AgentEvent("tool_use", {"name": "Read", "input": {"path": "a.txt"}}),
        AgentEvent("tool_result", "文件内容"),
        AgentEvent("result", {"result": "完成", "num_turns": 2, "is_error": False,
                              "usage": {"input_tokens": 10, "output_tokens": 4},
                              "cost_usd": 0.03, "duration_ms": 80,
                              "session_id": "sdk-1"}),
    ]


def _parse_sse(text: str) -> list[dict]:
    frames = []
    for block in text.split("\n\n"):
        block = block.strip()
        if not block:
            continue
        frame = {}
        for line in block.split("\n"):
            if line.startswith("event: "):
                frame["event"] = line[len("event: "):]
            elif line.startswith("data: "):
                frame["data"] = json.loads(line[len("data: "):])
        frames.append(frame)
    return frames


# ---- /health ----

def test_health_includes_checks_summary(client, monkeypatch):
    monkeypatch.setattr(service, "run_checks", lambda cfg, default_persona="x": [
        CheckResult("sdk", "ok", "可导入"),
        CheckResult("auth", "warn", "缺 BASE_URL"),
    ])
    r = client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["checks"]["total"] == 2
    assert body["checks"]["warn"] == 1
    assert body["checks"]["items"][0]["name"] == "sdk"


# ---- 同步 / 流式 单发 ----

def test_agent_run_accumulates_oneshot_usage(client):
    r = client.post("/agent/run", json={"prompt": "做任务"})
    assert r.status_code == 200
    body = r.json()
    assert body["output"] == "最终输出"
    assert body["turns"] == 3
    assert body["is_error"] is False
    status = client.get("/agent/status").json()
    assert status["oneshot_usage"]["runs"] == 1
    assert status["oneshot_usage"]["total_turns"] == 3
    assert status["oneshot_usage"]["total_cost_usd"] == pytest.approx(0.05)


def test_agent_run_stream_sse_sequence(client):
    with client.stream("POST", "/agent/run/stream",
                       json={"prompt": "流式任务"}) as resp:
        assert resp.status_code == 200
        assert "text/event-stream" in resp.headers["content-type"]
        text = "".join(resp.iter_text())
    frames = _parse_sse(text)
    assert [f["event"] for f in frames] == [
        "text_delta", "tool_use", "tool_result", "result"]
    assert frames[0]["data"] == {"text": "你好"}
    assert frames[1]["data"] == {"name": "Read", "input": {"path": "a.txt"}}
    assert frames[2]["data"] == {"content": "文件内容"}
    assert frames[3]["data"]["result"] == "完成"
    assert frames[3]["data"]["session_id"] == "sdk-1"


def test_sse_payload_truncated_over_2000_chars(client):
    big = "x" * 5000
    client.kernel.events = [AgentEvent("tool_result", big)]
    with client.stream("POST", "/agent/run/stream", json={"prompt": "p"}) as resp:
        text = "".join(resp.iter_text())
    assert "x" * 100 in text                       # 内容仍在（截断后的前缀）
    # 序列化前限幅：超长 payload 也必须是合法 JSON（旧实现截断字符串会炸 JSON.parse）
    frames = _parse_sse(text)
    assert [f["event"] for f in frames] == ["tool_result"]
    assert frames[0]["data"]["content"].endswith("...(truncated)")
    assert len(frames[0]["data"]["content"]) <= 2100
    for block in text.split("\n\n"):
        if block.strip():
            for line in block.split("\n"):
                if line.startswith("data: "):
                    # 2000 字符 + "...(truncated)" 标记 + 帧头，余量收紧到 2048
                    assert len(line) <= 2048


# ---- 异步运行 / 查询 / 取消 ----

def test_async_run_completes(client):
    r = client.post("/agent/run/async", json={"prompt": "长任务"})
    assert r.status_code == 202
    run_id = r.json()["run_id"]
    deadline = time.monotonic() + 5
    status = "running"
    while time.monotonic() < deadline:
        status = client.get(f"/agent/run/{run_id}").json()["status"]
        if status != "running":
            break
        time.sleep(0.02)
    assert status == "completed"
    body = client.get(f"/agent/run/{run_id}").json()
    assert body["result"]["output"] == "最终输出"
    assert body["result"]["turns"] == 3


def test_async_run_cancel(client):
    client.kernel.oneshot_delay = 0.8
    r = client.post("/agent/run/async", json={"prompt": "可取消任务"})
    run_id = r.json()["run_id"]
    # 等任务真正开跑（进入 oneshot_calls）
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline and not client.kernel.oneshot_calls:
        time.sleep(0.02)
    resp = client.delete(f"/agent/run/{run_id}")
    assert resp.status_code == 200
    assert resp.json()["cancel_requested"] is True
    deadline = time.monotonic() + 5
    status = "running"
    while time.monotonic() < deadline:
        status = client.get(f"/agent/run/{run_id}").json()["status"]
        if status != "running":
            break
        time.sleep(0.02)
    assert status == "cancelled"
    body = client.get(f"/agent/run/{run_id}").json()
    assert body["result"]["cancelled"] is True


def test_async_run_unknown_id_unified_error(client):
    r = client.get("/agent/run/does-not-exist")
    assert r.status_code == 404
    body = r.json()
    assert body["code"] == "not_found"
    assert "message" in body and "detail" in body


# ---- 多轮会话 ----

def test_session_lifecycle_message_interrupt_close(client):
    r = client.post("/agent/session", json={"persona": "executor"})
    assert r.status_code == 200
    sid = r.json()["session_id"]
    assert client.kernel.sessions[-1].connected is True

    msg = client.post(f"/agent/session/{sid}/message", json={"prompt": "hi"})
    assert msg.status_code == 200
    body = msg.json()
    assert body["output"] == "完成"
    assert body["turns"] == 2
    assert body["session_id"] == "sdk-1"
    assert body["budget_warnings"] == []

    intr = client.post(f"/agent/session/{sid}/interrupt")
    assert intr.json()["status"] == "interrupt_requested"
    assert client.kernel.sessions[-1].interrupted is True

    lst = client.get("/agent/sessions").json()
    assert lst["count"] == 1
    assert lst["sessions"][0]["session_id"] == sid

    close = client.delete(f"/agent/session/{sid}")
    assert close.json() == {"status": "closed", "session_id": sid}
    assert client.kernel.sessions[-1].closed is True


def test_session_message_passes_timeout(client):
    r = client.post("/agent/session", json={})
    sid = r.json()["session_id"]
    client.post(f"/agent/session/{sid}/message",
                json={"prompt": "hi", "timeout": 5})
    assert client.kernel.sessions[-1].send_calls[0][1] == 5
    client.post(f"/agent/session/{sid}/message", json={"prompt": "hi"})
    assert client.kernel.sessions[-1].send_calls[1][1] is None


def test_session_message_stream_sse(client):
    r = client.post("/agent/session", json={})
    sid = r.json()["session_id"]
    with client.stream("POST", f"/agent/session/{sid}/message/stream",
                       json={"prompt": "hi"}) as resp:
        assert resp.status_code == 200
        text = "".join(resp.iter_text())
    frames = _parse_sse(text)
    assert [f["event"] for f in frames] == [
        "text_delta", "tool_use", "tool_result", "result"]


def test_session_message_unknown_id_http_exception_shape(client):
    r = client.post("/agent/session/nope/message", json={"prompt": "x"})
    assert r.status_code == 404
    body = r.json()
    # HTTPException 也被收敛为统一形状
    assert body["code"] == "http_404"
    assert body["message"] == "session not found"


def test_session_budget_warnings_in_response(client):
    events = [
        AgentEvent("result", {"result": "r", "num_turns": 1, "cost_usd": 0.09,
                              "session_id": "s"}),
        AgentEvent("budget_warning", {"threshold": 0.8, "spent_usd": 0.09,
                                      "budget_usd": 0.1}),
    ]
    client.kernel.events = events
    r = client.post("/agent/session", json={})
    sid = r.json()["session_id"]
    body = client.post(f"/agent/session/{sid}/message",
                       json={"prompt": "hi"}).json()
    assert body["budget_warnings"] == [{"threshold": 0.8, "spent_usd": 0.09,
                                        "budget_usd": 0.1}]


# ---- 记忆端点 ----

def test_memory_core_write_read_list(client, cfg):
    put = client.put("/agent/memory/core/style",
                     json={"content": "永远写测试"})
    assert put.status_code == 200
    assert put.json()["status"] == "written"
    assert put.json()["path"].endswith("style.md")

    got = client.get("/agent/memory/core/style")
    assert got.status_code == 200
    assert got.json() == {"name": "style", "content": "永远写测试"}

    lst = client.get("/agent/memory/core").json()
    assert lst["count"] == 1
    assert lst["memories"][0]["name"] == "style"
    assert lst["backend"] == "local"

    missing = client.get("/agent/memory/core/nothere")
    assert missing.status_code == 404
    assert missing.json()["code"] == "not_found"


def test_memory_core_invalid_name_rejected(client):
    # 空格名（%20）不在合法字符集，应被拒（路径穿越类名字到不了路由层）
    r = client.get("/agent/memory/core/a%20b")
    assert r.status_code == 400
    assert r.json()["code"] == "invalid_name"
    # 直接调用校验函数验证穿越名
    assert not service._valid_memory_name("a/b")
    assert not service._valid_memory_name("..")
    # Windows 保留设备名一律拒绝（大小写不敏感）：写入会被设备吞
    for bad in ("NUL", "nul", "CON", "con", "AUX", "PRN",
                "com1", "COM1", "lpt9", "LPT9"):
        assert not service._valid_memory_name(bad), bad
    # 保留名走 HTTP 层同样 400（GET/PUT 一致）
    assert client.get("/agent/memory/core/NUL").status_code == 400
    r = client.put("/agent/memory/core/CON", json={"content": "x"})
    assert r.status_code == 400
    assert r.json()["code"] == "invalid_name"
    # 含保留名子串的正常名字不受影响
    assert service._valid_memory_name("nul_backup")


# ---- /agent/status 拆分 ----

def test_status_splits_session_and_oneshot_usage(client):
    # oneshot 用量
    client.post("/agent/run", json={"prompt": "p"})
    # session 用量
    sid = client.post("/agent/session", json={}).json()["session_id"]
    client.post(f"/agent/session/{sid}/message", json={"prompt": "hi"})
    status = client.get("/agent/status").json()
    assert status["active_sessions"] == 1
    assert status["oneshot_usage"]["runs"] == 1
    assert status["oneshot_usage"]["total_turns"] == 3
    assert status["session_usage"]["total_turns"] == 2
    assert status["session_usage"]["total_cost_usd"] == pytest.approx(0.03)
    # 旧字段兼容保留（会话口径）
    assert status["total_turns"] == 2
