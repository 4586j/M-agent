"""持久会话（派发链路连续对话）测试：SessionStore 账本、/sessions API、
dispatch session_id 注入历史 + 轮次落盘。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import pytest
from fastapi.testclient import TestClient

from session_store import SessionStore
from config import Config


def _make_app(tmp_path, **cfg_kw):
    from brain_api import create_brain_app
    from dispatcher import RemoteDispatcher
    from worker_registry import WorkerRegistry, WorkerInfo
    from task_store import TaskStore
    from event_bus import TaskEventBus

    seen_bodies = []

    def fake_http(method, url, **kwargs):
        if method == "POST":
            seen_bodies.append(kwargs.get("json") or {})
            return {"status_code": 200, "json": {"task_id": "x", "status": "accepted"}}
        return {"status_code": 200, "json": {
            "task_id": "x", "status": "done", "output": "第一次的产出", "artifacts": [],
            "error": None, "meta": {"cost_usd": 0.01}}}

    reg = WorkerRegistry(heartbeat_timeout=45, now_fn=lambda: 0.0)
    reg.register(WorkerInfo(worker_id="worker-A", role="executor", host="127.0.0.1",
                            port=8000, capabilities={}, last_heartbeat=0.0))
    store = TaskStore(str(tmp_path / ".tasks"))
    cfg = Config(mode="remote", task_store_dir=str(tmp_path / ".tasks"),
                 security_llm=False, sessions_dir=str(tmp_path / ".sessions"),
                 **cfg_kw)
    disp = RemoteDispatcher(task_store=store, registry=reg, http_call=fake_http,
                            now_fn=lambda: "t0", event_bus=TaskEventBus())
    app = create_brain_app(reg, store, cfg, dispatcher=disp)
    return TestClient(app), reg, store, seen_bodies


# ---- SessionStore 单元 ----

def test_create_list_delete(tmp_path):
    s = SessionStore(str(tmp_path / ".s"))
    rec = s.create()
    sid = rec["session_id"]
    assert len(sid) == 12 and rec["turns"] == []
    assert [x["session_id"] for x in s.list_all()] == [sid]
    assert s.delete(sid)
    assert s.get(sid) is None
    assert not s.delete(sid)


def test_append_turn_and_history_block(tmp_path):
    s = SessionStore(str(tmp_path / ".s"))
    sid = s.create()["session_id"]

    class FakeTask:
        def __init__(self, status, output):
            self.status, self.output = status, output

    tasks = {"task-1": FakeTask("done", "产出A")}
    s.append_turn(sid, "第一问", "task-1")
    s.append_turn(sid, "第二问", "task-missing")
    rec = s.get(sid)
    assert rec["title"] == "第一问"
    assert [t["prompt"] for t in rec["turns"]] == ["第一问", "第二问"]
    block = s.history_block(sid, task_lookup=lambda tid: tasks.get(tid))
    assert "会话历史" in block
    assert "第一问" in block and "产出A" in block
    assert "第二问" in block and "（执行中或无输出）" in block


def test_history_block_missing_session(tmp_path):
    s = SessionStore(str(tmp_path / ".s"))
    assert s.history_block("nope01234567", task_lookup=lambda _: None) == ""


def test_invalid_sid_rejected(tmp_path):
    s = SessionStore(str(tmp_path / ".s"))
    with pytest.raises(KeyError):
        s.append_turn("../evil", "p", "t1")
    assert s.get("../../etc/passwd") is None


# ---- /sessions API + dispatch 透传 ----

def test_sessions_endpoints_roundtrip(tmp_path):
    client, _, _, _ = _make_app(tmp_path)
    r = client.post("/sessions")
    assert r.status_code == 200
    sid = r.json()["session_id"]
    r = client.get("/sessions")
    assert [s["session_id"] for s in r.json()["sessions"]] == [sid]
    detail = client.get(f"/sessions/{sid}").json()
    assert detail["turns"] == []
    assert client.delete(f"/sessions/{sid}").json()["status"] == "deleted"
    assert client.get(f"/sessions/{sid}").status_code == 404


def test_dispatch_with_session_records_turn_and_injects_history(tmp_path):
    """两次带 session_id 的派发：第一次的 prompt+task_id 落盘；第二次派发的
    context_memory 携带会话历史（含第一次任务的联表输出）。"""
    client, _, store, seen_bodies = _make_app(tmp_path)
    sid = client.post("/sessions").json()["session_id"]

    r1 = client.post("/dispatch", json={
        "prompt": "第一问：分析数据", "role": "executor", "session_id": sid})
    assert r1.status_code == 200
    task1 = r1.json()["task_id"]

    detail = client.get(f"/sessions/{sid}").json()
    assert len(detail["turns"]) == 1
    assert detail["turns"][0]["task_id"] == task1
    assert detail["turns"][0]["status"] == "done"
    assert "第一次的产出" in detail["turns"][0]["output"]

    r2 = client.post("/dispatch", json={
        "prompt": "第二问：继续刚才的结果", "role": "executor", "session_id": sid})
    assert r2.status_code == 200
    # 第二次派发给 worker 的 context_memory 应包含第一问历史（联表输出）
    assert any("会话历史" in (b.get("context_memory") or "")
               and "第一问" in (b.get("context_memory") or "")
               and "第一次的产出" in (b.get("context_memory") or "")
               for b in seen_bodies[1:])
    detail2 = client.get(f"/sessions/{sid}").json()
    assert len(detail2["turns"]) == 2


def test_dispatch_without_session_unchanged(tmp_path):
    """不带 session_id 的派发不写会话（行为不变）。"""
    client, _, _, seen_bodies = _make_app(tmp_path)
    r = client.post("/dispatch", json={"prompt": "p", "role": "executor"})
    assert r.status_code == 200
    assert all("会话历史" not in (b.get("context_memory") or "") for b in seen_bodies)


def test_dispatch_bad_session_id_graceful(tmp_path):
    """session_id 非法（文件不存在）时轮次记录被跳过，派发不受影响。"""
    client, _, _, _ = _make_app(tmp_path)
    r = client.post("/dispatch", json={
        "prompt": "p", "role": "executor", "session_id": "deadbeefdead"})
    assert r.status_code == 200
    # 历史块注入也安全（session_store.get 返回 None → 空块）
