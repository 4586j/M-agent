"""阶段三可观测性测试：trace_id 贯穿 / worker meta 透传 / SSE 事件流 / 成本聚合。

参考 test_async_dispatcher.py / test_brain_api.py 既有模式：
dispatcher + fake http + TaskStore 临时目录 + TestClient。
"""
import asyncio
import json
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from fastapi.testclient import TestClient

from async_dispatcher import AsyncRemoteDispatcher
from brain_api import create_brain_app
from config import Config
from dispatcher import RemoteDispatcher
from event_bus import TaskEventBus, truncate_summary
from task_store import TaskStore, TaskRecord
from worker_registry import WorkerRegistry, WorkerInfo


def make_info(wid="worker-A"):
    return WorkerInfo(
        worker_id=wid, role="executor", host="127.0.0.1", port=8000,
        capabilities={}, last_heartbeat=0.0,
    )


def _reg():
    reg = WorkerRegistry(heartbeat_timeout=45, now_fn=lambda: 0.0)
    reg.register(make_info())
    return reg


def _remote_dispatcher(store, reg, bus=None, meta=None):
    """fake http：POST accepted；GET 返回 done + 可选 meta（模拟 worker 轮询响应）。"""
    def fake_http(method, url, **kwargs):
        if method == "POST":
            return {"status_code": 200, "json": {"status": "accepted"}}
        return {"status_code": 200, "json": {
            "status": "done", "output": "结果", "artifacts": [],
            "error": None, "meta": meta or {}}}
    return RemoteDispatcher(task_store=store, registry=reg, http_call=fake_http,
                            now_fn=lambda: "t0", event_bus=bus)


def _async_dispatcher(store, reg, bus=None):
    async def fake_async_http(method, url, **kwargs):
        return {"status_code": 200, "json": {"status": "accepted"}}
    return AsyncRemoteDispatcher(
        task_store=store, registry=reg, async_http=fake_async_http,
        now_fn=lambda: "t0", event_bus=bus, brain_url="http://brain:7000")


# ── trace_id 生成与持久化 ──

def test_remote_dispatch_generates_and_persists_trace_id(tmp_path):
    store = TaskStore(str(tmp_path))
    reg = _reg()
    seen = {}

    def fake_http(method, url, **kwargs):
        if method == "POST":
            seen["body"] = kwargs.get("json", {})
            return {"status_code": 200, "json": {"status": "accepted"}}
        # dispatch 同步轮询直到终态：GET 必须返回 done，否则轮询 500s 才超时
        return {"status_code": 200, "json": {
            "status": "done", "output": "ok", "artifacts": [], "error": None}}

    d = RemoteDispatcher(task_store=store, registry=reg, http_call=fake_http,
                         now_fn=lambda: "t0")
    res = d.dispatch(role="executor", prompt="do x")
    assert res.trace_id and res.trace_id.startswith("trc-")
    rec = store.get(res.task_id)
    assert rec is not None
    assert rec.trace_id == res.trace_id
    # 派发给 worker 的 payload 携带 trace_id
    assert seen["body"]["trace_id"] == res.trace_id


def test_async_dispatch_generates_trace_id_and_payload(tmp_path):
    store = TaskStore(str(tmp_path))
    reg = _reg()
    seen = {}

    async def fake_http(method, url, **kwargs):
        if method == "POST":
            seen["body"] = kwargs.get("json", {})
            return {"status_code": 200, "json": {"status": "accepted"}}
        return {"status_code": 200, "json": {"status": "running"}}

    d = AsyncRemoteDispatcher(task_store=store, registry=reg, async_http=fake_http,
                              now_fn=lambda: "t0")
    task_id = asyncio.run(d.dispatch_async(role="executor", prompt="do x"))
    rec = store.get(task_id)
    assert rec.trace_id and rec.trace_id.startswith("trc-")
    assert seen["body"]["trace_id"] == rec.trace_id


def test_task_record_compat_read_without_new_fields(tmp_path):
    """旧任务 JSON 文件缺 trace_id/meta 字段时兼容读取为 None / 空 dict。"""
    store = TaskStore(str(tmp_path))
    p = Path(tmp_path) / "task-old.json"
    p.write_text(json.dumps({
        "task_id": "task-old", "role": "executor", "prompt": "x",
        "worker_id": "A", "status": "done", "created_at": "t", "updated_at": "t",
        "output": None, "artifacts": [], "error": None,
    }), encoding="utf-8")
    rec = store.get("task-old")
    assert rec is not None
    assert rec.trace_id is None
    assert rec.meta == {}


# ── worker meta 透传 ──

def test_remote_dispatch_stores_worker_meta(tmp_path):
    """轮询终态时把 worker 回传的 meta（usage/cost_usd/num_turns）写入 TaskRecord。"""
    store = TaskStore(str(tmp_path))
    reg = _reg()
    meta = {"num_turns": 3, "cost_usd": 0.01,
            "usage": {"input_tokens": 10, "output_tokens": 5}}
    d = _remote_dispatcher(store, reg, meta=meta)
    d.dispatch(role="executor", prompt="do x", model="sonnet")
    rec = next(r for r in store.list_all() if r.status == "done")
    assert rec.meta["num_turns"] == 3
    assert rec.meta["cost_usd"] == 0.01
    assert rec.meta["usage"]["output_tokens"] == 5
    assert rec.meta["model"] == "sonnet"  # brain 侧路由模型保留在 meta


def test_callback_merges_meta_and_keeps_brain_model(tmp_path):
    store = TaskStore(str(tmp_path))
    reg = _reg()
    d = _async_dispatcher(store, reg)
    task_id = asyncio.run(d.dispatch_async(role="executor", prompt="do x", model="sonnet"))
    rec = store.get(task_id)
    # 派发时 brain 先记录路由 model；use_experience 默认开启并入库供终态化写关判定
    assert rec.meta["model"] == "sonnet"
    assert rec.meta["use_experience"] is True

    handled = asyncio.run(d.handle_callback(task_id, {
        "status": "done", "output": "回调结果", "artifacts": [], "error": None,
        "meta": {"num_turns": 2, "cost_usd": 0.02,
                 "usage": {"input_tokens": 7, "output_tokens": 3}},
    }))
    assert handled is True
    rec = store.get(task_id)
    assert rec.status == "done"
    assert rec.meta["num_turns"] == 2
    assert rec.meta["cost_usd"] == 0.02
    assert rec.meta["model"] == "sonnet"  # callback 无 model 时保留 brain 侧


def test_task_endpoints_expose_trace_id_and_meta(tmp_path):
    reg = WorkerRegistry(heartbeat_timeout=45, now_fn=lambda: 0.0)
    store = TaskStore(str(tmp_path))
    cfg = Config(mode="remote", task_store_dir=str(tmp_path), security_llm=False)
    store.write(TaskRecord(
        task_id="t1", role="executor", prompt="x", worker_id="A",
        status="done", created_at="t", updated_at="t",
        trace_id="trc-abc", meta={"cost_usd": 0.5}))
    client = TestClient(create_brain_app(reg, store, cfg))
    body = client.get("/tasks/t1").json()
    assert body["trace_id"] == "trc-abc"
    assert body["meta"]["cost_usd"] == 0.5
    lst = client.get("/tasks").json()
    hit = next(t for t in lst if t["task_id"] == "t1")
    assert hit["trace_id"] == "trc-abc"
    assert hit["meta"]["cost_usd"] == 0.5


# ── SSE 事件流 ──

def _make_sse_app(tmp_path, api_key=""):
    reg = WorkerRegistry(heartbeat_timeout=45, now_fn=lambda: 0.0)
    reg.register(make_info())
    store = TaskStore(str(tmp_path))
    cfg = Config(mode="remote", task_store_dir=str(tmp_path), security_llm=False,
                 api_key=api_key)
    bus = TaskEventBus()
    async_disp = _async_dispatcher(store, reg, bus=bus)
    app = create_brain_app(reg, store, cfg, async_dispatcher=async_disp, event_bus=bus)
    return app, store, bus


def test_sse_stream_receives_events_and_closes_on_done(tmp_path):
    """直接向总线发布（模拟 callback 路径）：订阅者收到 status/callback/done，终态后关流。"""
    app, store, bus = _make_sse_app(tmp_path)
    store.write(TaskRecord(
        task_id="t1", role="executor", prompt="x", worker_id="A",
        status="running", created_at="t", updated_at="t", trace_id="trc-abc"))
    client = TestClient(app)

    def publish_events():
        time.sleep(0.3)
        bus.publish("t1", "status", state="running", trace_id="trc-abc")
        time.sleep(0.2)
        bus.publish("t1", "callback", state="running", trace_id="trc-abc",
                    summary="回" + "调" * 2200)  # 触发 2000 字符截断
        bus.publish("t1", "done", state="done", trace_id="trc-abc", summary="ok")

    t = threading.Thread(target=publish_events)
    t.start()
    frames = []
    with client.stream("GET", "/tasks/t1/events") as r:
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("text/event-stream")
        for line in r.iter_lines():
            frames.append(line)
    t.join()
    text = "\n".join(frames)
    # 快照 + 三个事件帧均送达，trace_id 全程携带
    assert "event: status" in text
    assert "event: callback" in text
    assert "event: done" in text
    assert '"trace_id": "trc-abc"' in text
    assert '"state": "done"' in text
    assert '"snapshot": true' in text
    # callback 摘要截断到 2000 字符并标注
    assert "[已截断]" in text
    assert truncate_summary("x" * 3000) == "x" * 2000 + "…[已截断]"


def test_sse_callback_endpoint_integration(tmp_path):
    """端到端：真实 POST /tasks/{id}/callback → SSE 收到 callback + done，meta 入库。"""
    app, store, bus = _make_sse_app(tmp_path)
    store.write(TaskRecord(
        task_id="t1", role="executor", prompt="x", worker_id="A",
        status="running", created_at="t", updated_at="t", trace_id="trc-int"))
    client = TestClient(app)
    poster = TestClient(app)  # 独立 portal：跨线程 POST，验证跨 loop 投递

    def fire_callback():
        time.sleep(0.3)
        r = poster.post("/tasks/t1/callback", json={
            "status": "done", "output": "cb result", "artifacts": [],
            "error": None, "meta": {"num_turns": 1, "cost_usd": 0.003},
            "trace_id": "trc-int"})
        assert r.status_code == 200

    t = threading.Thread(target=fire_callback)
    t.start()
    events = []
    with client.stream("GET", "/tasks/t1/events") as r:
        for line in r.iter_lines():
            events.append(line)
            if line == "event: done":
                break
    t.join()
    text = "\n".join(events)
    assert "event: callback" in text
    assert "cb result" in text
    assert "event: done" in text
    # callback 到达后任务终态化且 meta 已入库
    rec = store.get("t1")
    assert rec.status == "done"
    assert rec.meta["cost_usd"] == 0.003


def test_sse_terminal_task_returns_snapshot_only(tmp_path):
    app, store, _ = _make_sse_app(tmp_path)
    store.write(TaskRecord(
        task_id="t9", role="executor", prompt="x", worker_id="A",
        status="done", created_at="t", updated_at="t", trace_id="trc-done"))
    client = TestClient(app)
    frames = []
    with client.stream("GET", "/tasks/t9/events") as r:
        assert r.status_code == 200
        for line in r.iter_lines():
            frames.append(line)
    text = "\n".join(frames)
    assert "event: status" in text
    assert '"state": "done"' in text
    assert "event: done" not in text  # 快照已终态，直接结束不再等 done 事件


def test_sse_auth_via_header_and_query_key(tmp_path):
    """SSE 鉴权：EventSource 无法带 header，须同时接受 X-API-Key 与 ?api_key=。

    用终态任务开流：TestClient 的 ASGITransport 会缓冲整个 SSE 响应，
    running 任务（无事件）永不完成会挂起；终态任务快照后立即关流。
    """
    app, store, _ = _make_sse_app(tmp_path, api_key="secret")
    store.write(TaskRecord(
        task_id="t1", role="executor", prompt="x", worker_id="A",
        status="done", created_at="t", updated_at="t", trace_id="trc-k"))
    client = TestClient(app)
    assert client.get("/tasks/t1/events").status_code == 401
    assert client.get("/tasks/t1/events?api_key=wrong").status_code == 401
    with client.stream("GET", "/tasks/t1/events",
                       headers={"X-API-Key": "secret"}) as r:
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("text/event-stream")
        for _ in r.iter_lines():
            pass
    with client.stream("GET", "/tasks/t1/events?api_key=secret") as r:
        assert r.status_code == 200
        for _ in r.iter_lines():
            pass


def test_sse_404_for_unknown_task(tmp_path):
    app, _, _ = _make_sse_app(tmp_path)
    client = TestClient(app)
    assert client.get("/tasks/missing/events").status_code == 404


# ── /stats 成本聚合 ──

def test_stats_cost_aggregation(tmp_path):
    reg = WorkerRegistry(heartbeat_timeout=45, now_fn=lambda: 0.0)
    store = TaskStore(str(tmp_path))
    cfg = Config(mode="remote", task_store_dir=str(tmp_path), security_llm=False)
    now = time.time()
    today = str(now)
    yesterday = str(now - 86400)
    nine_days_ago = str(now - 9 * 86400)

    store.write(TaskRecord(
        task_id="t1", role="executor", prompt="x", worker_id="A",
        status="done", created_at=yesterday, updated_at=yesterday,
        trace_id="trc-1", meta={"model": "sonnet", "cost_usd": 0.02,
                                "usage": {"input_tokens": 100, "output_tokens": 50}}))
    store.write(TaskRecord(
        task_id="t2", role="web-expert", prompt="y", worker_id="A",
        status="done", created_at=today, updated_at=today,
        trace_id="trc-2", meta={"model": "sonnet", "cost_usd": 0.01,
                                "usage": {"input_tokens": 10, "output_tokens": 20}}))
    store.write(TaskRecord(
        task_id="t3", role="executor", prompt="z", worker_id="A",
        status="done", created_at=nine_days_ago, updated_at=nine_days_ago,
        trace_id="trc-3", meta={"model": "sonnet", "cost_usd": 9.99,
                                "usage": {"input_tokens": 1}}))
    store.write(TaskRecord(
        task_id="t4", role="executor", prompt="w", worker_id="A",
        status="running", created_at=today, updated_at=today,
        trace_id="trc-4"))  # 无 meta：计入 tasks，tokens/cost 为 null 源
    client = TestClient(create_brain_app(reg, store, cfg))
    s = client.get("/stats").json()["cost"]

    # total 聚合全部任务（不限窗口）
    assert s["total"]["tasks"] == 4
    assert s["total"]["tokens"] == 181
    assert abs(s["total"]["cost_usd"] - 10.02) < 1e-9
    # 按日仅近 7 天：t3（9 天前）不进 by_day；t1/t2 有数值，t4 无 meta
    assert len(s["by_day"]) == 2
    today_b = next(b for b in s["by_day"] if b["tasks"] >= 2)
    assert today_b["tasks"] == 2  # t2 + t4
    assert today_b["tokens"] == 30
    assert abs(today_b["cost_usd"] - 0.01) < 1e-9
    # 按 role / 按 model 全量聚合
    assert s["by_role"]["executor"]["tokens"] == 151
    assert s["by_role"]["web-expert"]["tokens"] == 30
    assert abs(s["by_model"]["sonnet"]["cost_usd"] - 10.02) < 1e-9
    assert s["by_model"]["sonnet"]["tasks"] == 3


def test_stats_cost_empty_returns_null_fields(tmp_path):
    """无任何 meta 数据时聚合结构仍完整，数值字段为 null/空。"""
    reg = WorkerRegistry(heartbeat_timeout=45, now_fn=lambda: 0.0)
    store = TaskStore(str(tmp_path))
    cfg = Config(mode="remote", task_store_dir=str(tmp_path), security_llm=False)
    store.write(TaskRecord(
        task_id="t1", role="executor", prompt="x", worker_id="A",
        status="done", created_at="t", updated_at=str(time.time()),
        trace_id="trc-x"))
    client = TestClient(create_brain_app(reg, store, cfg))
    s = client.get("/stats").json()["cost"]
    assert s["total"] == {"tasks": 1, "tokens": None, "cost_usd": None}
    assert s["by_day"][0]["tokens"] is None
    assert s["by_role"]["executor"]["cost_usd"] is None
    assert s["by_model"] == {}


# ── /stats 输出质量聚合（degenerate）──

def test_stats_quality_aggregation(tmp_path):
    """按 model 统计退化任务数与比率：发生过检出（detected=True）即计入。"""
    reg = WorkerRegistry(heartbeat_timeout=45, now_fn=lambda: 0.0)
    store = TaskStore(str(tmp_path))
    cfg = Config(mode="remote", task_store_dir=str(tmp_path), security_llm=False)
    now = str(time.time())

    def _rec(task_id, model=None, degen=None):
        meta = {}
        if model is not None:
            meta["model"] = model
        if degen is not None:
            meta["degenerate"] = degen
        store.write(TaskRecord(
            task_id=task_id, role="executor", prompt="x", worker_id="A",
            status="done", created_at=now, updated_at=now,
            trace_id=f"trc-{task_id}", meta=meta))

    _rec("t1", model="sonnet", degen={"detected": True, "recovered": True})
    _rec("t2", model="sonnet", degen=None)  # 正常任务
    _rec("t3", model=None, degen={"detected": True, "recovered": False})  # model 缺失
    _rec("t4", model="haiku", degen={"detected": False})  # 检出 False 不计入
    _rec("t5")  # 无 meta：model 归 "" 键，视为非退化

    client = TestClient(create_brain_app(reg, store, cfg))
    q = client.get("/stats").json()["quality"]
    assert q["by_model"]["sonnet"] == {"tasks": 2, "degenerate": 1, "rate": 0.5}
    assert q["by_model"]["haiku"] == {"tasks": 1, "degenerate": 0, "rate": 0.0}
    # model 缺失的任务归入空字符串键（缺失为空）
    assert q["by_model"][""] == {"tasks": 2, "degenerate": 1, "rate": 0.5}


def test_stats_quality_empty(tmp_path):
    """无任务时 quality 聚合为空结构（不报错）。"""
    reg = WorkerRegistry(heartbeat_timeout=45, now_fn=lambda: 0.0)
    store = TaskStore(str(tmp_path))
    cfg = Config(mode="remote", task_store_dir=str(tmp_path), security_llm=False)
    client = TestClient(create_brain_app(reg, store, cfg))
    q = client.get("/stats").json()["quality"]
    assert q == {"by_model": {}}


# ── 聚合纯函数单测（不启 HTTP 层，覆盖 meta 形态边界）──

def test_aggregate_quality_pure_function():
    """meta=None/空/非法 degenerate/model 缺失归空键/rate 精度（round 4 位）。"""
    import pytest
    from types import SimpleNamespace
    from brain_routers import _aggregate_quality
    tasks = [
        SimpleNamespace(meta={"model": "sonnet",
                              "degenerate": {"detected": True}}),
        SimpleNamespace(meta={"model": "sonnet"}),                     # 无标记
        SimpleNamespace(meta={"model": "sonnet",
                              "degenerate": {"detected": False}}),     # 不计入
        SimpleNamespace(meta={"model": "haiku",
                              "degenerate": {"detected": True}}),
        SimpleNamespace(meta={}),                           # model 缺失 → "" 键
        SimpleNamespace(meta=None),                         # meta=None 视为 {}
        SimpleNamespace(meta={"degenerate": "not-a-dict"}),  # 非法形态不误计
    ]
    q = _aggregate_quality(tasks)
    # sonnet 3 任务 1 退化：rate = round(1/3, 4) = 0.3333（精度断言）
    assert q["by_model"]["sonnet"] == {"tasks": 3, "degenerate": 1,
                                       "rate": pytest.approx(0.3333)}
    assert q["by_model"]["haiku"] == {"tasks": 1, "degenerate": 1, "rate": 1.0}
    assert q["by_model"][""] == {"tasks": 3, "degenerate": 0, "rate": 0.0}
    assert set(q) == {"by_model"}


def test_aggregate_costs_pure_function_edges():
    """tokens/cost 缺失置 null、非法 updated_at/窗口外不入 by_day、cost 非数值。"""
    import pytest
    from datetime import datetime
    from types import SimpleNamespace
    from brain_routers import _aggregate_costs
    now = time.time()
    ts = now - 1800  # 窗口内（近 7 天）
    tasks = [
        SimpleNamespace(role="executor", updated_at=str(ts),
                        meta={"usage": {"input_tokens": 100,
                                        "output_tokens": 50},
                              "cost_usd": 0.01, "model": "sonnet"}),
        SimpleNamespace(role="executor", updated_at=str(ts),
                        meta={"model": "sonnet"}),        # usage/cost 缺失
        SimpleNamespace(role="planner", updated_at=str(ts), meta=None),
        SimpleNamespace(role="executor", updated_at="not-a-number",
                        meta={"usage": {"input_tokens": 1}, "cost_usd": 0.02}),
        SimpleNamespace(role="executor", updated_at=str(now - 8 * 86400),
                        meta={"usage": {"input_tokens": 7}, "cost_usd": 0.03}),
        SimpleNamespace(role="executor", updated_at=str(ts),
                        meta={"cost_usd": "free"}),       # 非数值 → 缺失
    ]
    c = _aggregate_costs(tasks)
    assert c["total"]["tasks"] == 6
    assert c["total"]["tokens"] == 158
    assert c["total"]["cost_usd"] == pytest.approx(0.06)
    # by_day 只含窗口内的天（非法 updated_at 与 8 天前不入）
    day = datetime.fromtimestamp(ts).strftime("%Y-%m-%d")
    assert [d["date"] for d in c["by_day"]] == [day]
    assert c["by_day"][0]["tasks"] == 4
    assert c["by_day"][0]["tokens"] == 150
    assert c["by_day"][0]["cost_usd"] == pytest.approx(0.01)
    # by_role：executor 5 条（158 tokens / $0.06），planner 1 条（null/null）
    assert c["by_role"]["executor"] == {"tasks": 5, "tokens": 158,
                                        "cost_usd": pytest.approx(0.06)}
    assert c["by_role"]["planner"] == {"tasks": 1, "tokens": None,
                                       "cost_usd": None}
    # by_model：只计 meta.model 非空的任务
    assert c["by_model"]["sonnet"] == {"tasks": 2, "tokens": 150,
                                       "cost_usd": pytest.approx(0.01)}
