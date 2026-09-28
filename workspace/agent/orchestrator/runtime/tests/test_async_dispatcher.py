import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from async_dispatcher import AsyncRemoteDispatcher
from task_store import TaskStore
from worker_registry import WorkerRegistry, WorkerInfo


def make_info(wid="worker-A"):
    return WorkerInfo(
        worker_id=wid, role="executor", host="127.0.0.1", port=8000,
        capabilities={}, last_heartbeat=0.0,
    )


def _run(coro):
    return asyncio.run(coro)


def test_dispatch_async_creates_task_and_returns_id(tmp_path):
    store = TaskStore(str(tmp_path))
    reg = WorkerRegistry(heartbeat_timeout=45, now_fn=lambda: 0.0)
    reg.register(make_info())

    async def fake_http(method, url, **kwargs):
        return {"status_code": 200, "json": {"task_id": "task-x", "status": "accepted"}}

    d = AsyncRemoteDispatcher(
        task_store=store, registry=reg, async_http=fake_http,
        now_fn=lambda: "t0", poll_interval=0.01, poll_max=5,
    )
    task_id = _run(d.dispatch_async(role="executor", prompt="do x"))
    assert task_id.startswith("task-")
    rec = store.get(task_id)
    assert rec is not None
    assert rec.status == "running"
    assert rec.worker_id == "worker-A"


def test_dispatch_async_no_worker_raises(tmp_path):
    store = TaskStore(str(tmp_path))
    reg = WorkerRegistry(heartbeat_timeout=45, now_fn=lambda: 0.0)

    async def fake_http(method, url, **kwargs):
        return {"status_code": 200, "json": {}}

    d = AsyncRemoteDispatcher(
        task_store=store, registry=reg, async_http=fake_http,
        now_fn=lambda: "t0",
    )
    try:
        _run(d.dispatch_async(role="executor", prompt="do x"))
        assert False, "应抛 RuntimeError"
    except RuntimeError as e:
        assert "无可用" in str(e)


def test_dispatch_async_post_failed_marks_task_failed(tmp_path):
    store = TaskStore(str(tmp_path))
    reg = WorkerRegistry(heartbeat_timeout=45, now_fn=lambda: 0.0)
    reg.register(make_info())

    async def fake_http(method, url, **kwargs):
        return {"status_code": 500, "json": {"error": "internal"}}

    d = AsyncRemoteDispatcher(
        task_store=store, registry=reg, async_http=fake_http,
        now_fn=lambda: "t0",
    )
    task_id = _run(d.dispatch_async(role="executor", prompt="do x"))
    rec = store.get(task_id)
    assert rec.status == "failed"
    assert "post failed" in rec.error


def test_poll_worker_detects_done(tmp_path):
    store = TaskStore(str(tmp_path))
    reg = WorkerRegistry(heartbeat_timeout=45, now_fn=lambda: 0.0)
    reg.register(make_info())

    calls = {"get": 0}

    async def fake_http(method, url, **kwargs):
        if method == "POST":
            return {"status_code": 200, "json": {"status": "accepted"}}
        calls["get"] += 1
        if calls["get"] == 1:
            return {"status_code": 200, "json": {"status": "running"}}
        return {"status_code": 200, "json": {
            "status": "done", "output": "结果",
            "artifacts": [], "error": None}}

    d = AsyncRemoteDispatcher(
        task_store=store, registry=reg, async_http=fake_http,
        now_fn=lambda: "t0", poll_interval=0.01, poll_max=10,
    )
    task_id = _run(d.dispatch_async(role="executor", prompt="do x"))
    _run(d._poll_worker(task_id, "http://127.0.0.1:8000",
                        "worker-A", "executor", "do x", "t0"))
    rec = store.get(task_id)
    assert rec.status == "done"
    assert rec.output == "结果"
    assert calls["get"] == 2


def test_poll_worker_detects_failed(tmp_path):
    store = TaskStore(str(tmp_path))
    reg = WorkerRegistry(heartbeat_timeout=45, now_fn=lambda: 0.0)
    reg.register(make_info())

    async def fake_http(method, url, **kwargs):
        if method == "POST":
            return {"status_code": 200, "json": {"status": "accepted"}}
        return {"status_code": 200, "json": {
            "status": "failed", "output": None,
            "artifacts": [], "error": "boom"}}

    d = AsyncRemoteDispatcher(
        task_store=store, registry=reg, async_http=fake_http,
        now_fn=lambda: "t0", poll_interval=0.01, poll_max=5,
    )
    task_id = _run(d.dispatch_async(role="executor", prompt="do x"))
    _run(d._poll_worker(task_id, "http://127.0.0.1:8000",
                        "worker-A", "executor", "do x", "t0"))
    rec = store.get(task_id)
    assert rec.status == "failed"
    assert rec.error == "boom"


def test_poll_worker_timeout(tmp_path):
    store = TaskStore(str(tmp_path))
    reg = WorkerRegistry(heartbeat_timeout=45, now_fn=lambda: 0.0)
    reg.register(make_info())

    async def fake_http(method, url, **kwargs):
        if method == "POST":
            return {"status_code": 200, "json": {"status": "accepted"}}
        return {"status_code": 200, "json": {"status": "running"}}

    d = AsyncRemoteDispatcher(
        task_store=store, registry=reg, async_http=fake_http,
        now_fn=lambda: "t0", poll_interval=0.01, poll_max=3,
    )
    task_id = _run(d.dispatch_async(role="executor", prompt="do x"))
    _run(d._poll_worker(task_id, "http://127.0.0.1:8000",
                        "worker-A", "executor", "do x", "t0"))
    rec = store.get(task_id)
    assert rec.status == "failed"
    assert rec.error == "async poll timeout"


def test_handle_callback_finalizes_task(tmp_path):
    store = TaskStore(str(tmp_path))
    reg = WorkerRegistry(heartbeat_timeout=45, now_fn=lambda: 0.0)
    reg.register(make_info())

    async def fake_http(method, url, **kwargs):
        return {"status_code": 200, "json": {}}

    d = AsyncRemoteDispatcher(
        task_store=store, registry=reg, async_http=fake_http,
        now_fn=lambda: "t0",
    )
    task_id = _run(d.dispatch_async(role="executor", prompt="do x"))
    rec = store.get(task_id)
    assert rec.status == "running"

    handled = _run(d.handle_callback(task_id, {
        "status": "done", "output": "回调结果",
        "artifacts": ["report.md"], "error": None,
    }))
    assert handled is True
    rec = store.get(task_id)
    assert rec.status == "done"
    assert rec.output == "回调结果"
    assert rec.artifacts == ["report.md"]


def test_handle_callback_already_done_returns_false(tmp_path):
    store = TaskStore(str(tmp_path))
    reg = WorkerRegistry(heartbeat_timeout=45, now_fn=lambda: 0.0)
    reg.register(make_info())

    async def fake_http(method, url, **kwargs):
        return {"status_code": 200, "json": {}}

    d = AsyncRemoteDispatcher(
        task_store=store, registry=reg, async_http=fake_http,
        now_fn=lambda: "t0",
    )
    task_id = _run(d.dispatch_async(role="executor", prompt="do x"))
    store.update(task_id, status="done", output="already done")

    handled = _run(d.handle_callback(task_id, {
        "status": "done", "output": "late callback",
        "artifacts": [], "error": None,
    }))
    assert handled is False
    rec = store.get(task_id)
    assert rec.output == "already done"


def test_handle_callback_unknown_task_returns_false(tmp_path):
    store = TaskStore(str(tmp_path))
    reg = WorkerRegistry(heartbeat_timeout=45, now_fn=lambda: 0.0)

    async def fake_http(method, url, **kwargs):
        return {"status_code": 200, "json": {}}

    d = AsyncRemoteDispatcher(
        task_store=store, registry=reg, async_http=fake_http,
        now_fn=lambda: "t0",
    )
    handled = _run(d.handle_callback("nope", {"status": "done"}))
    assert handled is False


def test_finalize_with_product_check(tmp_path):
    store = TaskStore(str(tmp_path))
    reg = WorkerRegistry(heartbeat_timeout=45, now_fn=lambda: 0.0)
    reg.register(make_info())

    seen = {"content": ""}

    def product_check(content):
        seen["content"] = content
        return {"verdict": "pass", "reason": "ok",
                "matched_rules": [], "suggestion": ""}

    async def fake_http(method, url, **kwargs):
        if method == "POST":
            return {"status_code": 200, "json": {"status": "accepted"}}
        if "/artifact/" in url:
            return {"status_code": 200, "json": {"content": "artifact body"}}
        return {"status_code": 200, "json": {
            "status": "done", "output": "结果",
            "artifacts": ["report.md"], "error": None}}

    d = AsyncRemoteDispatcher(
        task_store=store, registry=reg, async_http=fake_http,
        now_fn=lambda: "t0", product_check=product_check,
        poll_interval=0.01, poll_max=5,
    )
    task_id = _run(d.dispatch_async(role="executor", prompt="do x"))
    _run(d._poll_worker(task_id, "http://127.0.0.1:8000",
                        "worker-A", "executor", "do x", "t0"))
    rec = store.get(task_id)
    assert rec.verdict is not None
    assert rec.verdict["verdict"] == "pass"
    assert "结果" in seen["content"]
    assert "artifact body" in seen["content"]


def test_finalize_with_experience_store(tmp_path):
    store = TaskStore(str(tmp_path))
    reg = WorkerRegistry(heartbeat_timeout=45, now_fn=lambda: 0.0)
    reg.register(make_info())

    from experience_store import ExperienceStore
    exp_store = ExperienceStore(str(tmp_path / "exp"))

    async def fake_http(method, url, **kwargs):
        if method == "POST":
            return {"status_code": 200, "json": {"status": "accepted"}}
        return {"status_code": 200, "json": {
            "status": "done", "output": "结果",
            "artifacts": [], "error": None}}

    d = AsyncRemoteDispatcher(
        task_store=store, registry=reg, async_http=fake_http,
        now_fn=lambda: "t0", experience_store=exp_store,
        poll_interval=0.01, poll_max=5,
    )
    task_id = _run(d.dispatch_async(role="executor", prompt="do x"))
    _run(d._poll_worker(task_id, "http://127.0.0.1:8000",
                        "worker-A", "executor", "do x", "t0"))
    exps = exp_store.list_all()
    assert len(exps) == 1
    assert exps[0].success is True
    assert exps[0].prompt == "do x"


def test_callback_and_poll_race_only_one_wins(tmp_path):
    """回调和轮询都试图 finalize，只有先到的生效。"""
    store = TaskStore(str(tmp_path))
    reg = WorkerRegistry(heartbeat_timeout=45, now_fn=lambda: 0.0)
    reg.register(make_info())

    async def fake_http(method, url, **kwargs):
        return {"status_code": 200, "json": {}}

    d = AsyncRemoteDispatcher(
        task_store=store, registry=reg, async_http=fake_http,
        now_fn=lambda: "t0",
    )
    task_id = _run(d.dispatch_async(role="executor", prompt="do x"))

    _run(d.handle_callback(task_id, {
        "status": "done", "output": "from callback",
        "artifacts": [], "error": None,
    }))
    _run(d._finalize(task_id, "worker-A", "executor", "do x", "t0",
                     {"status": "done", "output": "from poll",
                      "artifacts": [], "error": None}))
    rec = store.get(task_id)
    assert rec.output == "from callback"


def test_dispatch_async_sends_callback_url(tmp_path):
    """dispatch_async 向 worker POST 时包含 callback_url。"""
    store = TaskStore(str(tmp_path))
    reg = WorkerRegistry(heartbeat_timeout=45, now_fn=lambda: 0.0)
    reg.register(make_info())

    seen_body = {}

    async def fake_http(method, url, **kwargs):
        if method == "POST":
            seen_body.update(kwargs.get("json", {}))
            return {"status_code": 200, "json": {"status": "accepted"}}
        return {"status_code": 200, "json": {"status": "running"}}

    d = AsyncRemoteDispatcher(
        task_store=store, registry=reg, async_http=fake_http,
        now_fn=lambda: "t0", brain_url="http://brain:7000",
    )
    task_id = _run(d.dispatch_async(role="executor", prompt="do x"))
    assert "callback_url" in seen_body
    assert seen_body["callback_url"] == f"http://brain:7000/tasks/{task_id}/callback"


def test_poll_skips_already_finalized(tmp_path):
    """轮询发现任务已被回调终结，不再覆盖。"""
    store = TaskStore(str(tmp_path))
    reg = WorkerRegistry(heartbeat_timeout=45, now_fn=lambda: 0.0)
    reg.register(make_info())

    async def fake_http(method, url, **kwargs):
        return {"status_code": 200, "json": {
            "status": "done", "output": "poll result",
            "artifacts": [], "error": None}}

    d = AsyncRemoteDispatcher(
        task_store=store, registry=reg, async_http=fake_http,
        now_fn=lambda: "t0", poll_interval=0.01, poll_max=5,
    )
    task_id = _run(d.dispatch_async(role="executor", prompt="do x"))
    store.update(task_id, status="done", output="callback result")

    _run(d._poll_worker(task_id, "http://127.0.0.1:8000",
                        "worker-A", "executor", "do x", "t0"))
    rec = store.get(task_id)
    assert rec.output == "callback result"


def test_async_ablation_skips_experience_write(tmp_path):
    """use_experience=False 时终态化不沉淀经验（写关）。"""
    store = TaskStore(str(tmp_path))
    reg = WorkerRegistry(heartbeat_timeout=45, now_fn=lambda: 0.0)
    reg.register(make_info())

    from experience_store import ExperienceStore
    exp_store = ExperienceStore(str(tmp_path / "exp"))

    async def fake_http(method, url, **kwargs):
        if method == "POST":
            return {"status_code": 200, "json": {"status": "accepted"}}
        return {"status_code": 200, "json": {
            "status": "done", "output": "结果",
            "artifacts": [], "error": None}}

    d = AsyncRemoteDispatcher(
        task_store=store, registry=reg, async_http=fake_http,
        now_fn=lambda: "t0", experience_store=exp_store,
        poll_interval=0.01, poll_max=5,
    )
    task_id = _run(d.dispatch_async(role="executor", prompt="do x",
                                    use_experience=False))
    _run(d._poll_worker(task_id, "http://127.0.0.1:8000",
                        "worker-A", "executor", "do x", "t0"))
    assert store.get(task_id).status == "done"
    assert exp_store.list_all() == []


def test_async_ablation_skips_experience_read(tmp_path):
    """use_experience=False 时不检索/不注入历史经验块（读关）；默认注入。"""
    store = TaskStore(str(tmp_path))
    reg = WorkerRegistry(heartbeat_timeout=45, now_fn=lambda: 0.0)
    reg.register(make_info())

    from experience_store import ExperienceStore
    exp_store = ExperienceStore(str(tmp_path / "exp"))
    exp_store.record_from_task(task_id="seed", role="executor",
                               prompt="compute the total order amount",
                               output="TOTAL=42", success=True, created_at="t-1")

    seen = {}

    async def fake_http(method, url, **kwargs):
        if method == "POST":
            seen.update(kwargs.get("json", {}))
            return {"status_code": 200, "json": {"status": "accepted"}}
        return {"status_code": 200, "json": {"status": "running"}}

    d = AsyncRemoteDispatcher(
        task_store=store, registry=reg, async_http=fake_http,
        now_fn=lambda: "t0", experience_store=exp_store,
    )
    _run(d.dispatch_async(role="executor",
                          prompt="compute the total order amount",
                          use_experience=False))
    assert "历史经验参考" not in (seen.get("context_memory") or "")

    seen.clear()
    _run(d.dispatch_async(role="executor",
                          prompt="compute the total order amount"))
    assert "历史经验参考" in (seen.get("context_memory") or "")


# ---- 定点派发（worker_id）----

def test_dispatch_async_pinned_posts_to_target(tmp_path):
    store = TaskStore(str(tmp_path))
    reg = WorkerRegistry(heartbeat_timeout=45, now_fn=lambda: 0.0)
    reg.register(make_info("A"))  # 127.0.0.1:8000
    reg.register(WorkerInfo(
        worker_id="B", role="executor", host="10.0.0.5", port=9001,
        capabilities={}, last_heartbeat=0.0))
    seen = {}

    async def fake_http(method, url, **kwargs):
        if method == "POST":
            seen["post_url"] = url
            return {"status_code": 200, "json": {"status": "accepted"}}
        return {"status_code": 200, "json": {"status": "running"}}

    d = AsyncRemoteDispatcher(
        task_store=store, registry=reg, async_http=fake_http,
        now_fn=lambda: "t0", poll_interval=0.01, poll_max=5,
    )
    task_id = _run(d.dispatch_async(role="executor", prompt="do x",
                                    worker_id="B"))
    assert seen["post_url"] == "http://10.0.0.5:9001/task"
    rec = store.get(task_id)
    assert rec.worker_id == "B"
    assert rec.meta.get("pinned") is True


def test_dispatch_async_pinned_unknown_raises_no_task(tmp_path):
    store = TaskStore(str(tmp_path))
    reg = WorkerRegistry(heartbeat_timeout=45, now_fn=lambda: 0.0)
    reg.register(make_info("A"))

    async def fake_http(method, url, **kwargs):
        return {"status_code": 200, "json": {}}

    d = AsyncRemoteDispatcher(
        task_store=store, registry=reg, async_http=fake_http,
        now_fn=lambda: "t0",
    )
    try:
        _run(d.dispatch_async(role="executor", prompt="do x", worker_id="NOPE"))
        raise AssertionError("未知节点异步定点应抛 RuntimeError")
    except RuntimeError as e:
        assert "从未注册" in str(e)
    assert store.list_all() == []  # 零任务落库


def test_dispatch_async_pinned_role_follows_worker(tmp_path):
    store = TaskStore(str(tmp_path))
    reg = WorkerRegistry(heartbeat_timeout=45, now_fn=lambda: 0.0)
    reg.register(make_info("A"))  # executor
    reg.register(WorkerInfo(
        worker_id="G", role="data-expert", host="127.0.0.1", port=8005,
        capabilities={}, last_heartbeat=0.0))

    async def fake_http(method, url, **kwargs):
        return {"status_code": 200, "json": {"status": "accepted"}}

    d = AsyncRemoteDispatcher(
        task_store=store, registry=reg, async_http=fake_http,
        now_fn=lambda: "t0", poll_interval=0.01, poll_max=5,
    )
    task_id = _run(d.dispatch_async(role="executor", prompt="do x",
                                    worker_id="G"))
    rec = store.get(task_id)
    assert rec.role == "data-expert"  # 节点真实 role 覆盖请求 role
    assert rec.meta.get("pinned") is True
