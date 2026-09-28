import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from fastapi.testclient import TestClient

from brain_api import create_brain_app
from config import Config
from dispatch_common import assemble_context, clamp_context, write_result_note
from dispatcher import Dispatcher, DispatchResult
from memory_store import FileMemoryStore
from worker_registry import WorkerRegistry
from task_store import TaskStore


def make_memory(tmp_path) -> FileMemoryStore:
    return FileMemoryStore(core_dir=str(tmp_path / "core"),
                           context_dir=str(tmp_path / "ctx"))


# ---- clamp_context ----

def test_clamp_context_within_limit():
    assert clamp_context("短内容", 100) == "短内容"
    assert clamp_context(None, 100) == ""
    assert clamp_context("abc", 0) == "abc"  # 非正上限不裁剪


def test_clamp_context_truncates_with_marker():
    text = "x" * 300
    out = clamp_context(text, 100)
    assert len(out) < 300
    assert out.startswith("x" * 100)
    assert "已截断" in out


# ---- write_result_note ----

def test_write_result_note(tmp_path):
    mem = make_memory(tmp_path)
    write_result_note(mem, "task-abc1", "done", "产出内容" * 300, None)
    note = mem.read_context("task-abc1", "result")
    assert note is not None
    assert "status: done" in note
    assert len(note) < 1200  # 输出截断到 800 字符左右


def test_write_result_note_none_store_noop():
    write_result_note(None, "task-abc1", "done", "out", None)  # 不抛异常


# ---- assemble_context ----

def test_assemble_context_merges_referenced_tasks(tmp_path):
    mem = make_memory(tmp_path)
    mem.write_context("task-a1", "result", "任务A的结论")
    mem.write_context("task-b2", "result", "任务B的结论")
    out = assemble_context(mem, "用户偏好：中文", ["task-a1", "task-b2"])
    assert out.startswith("用户偏好：中文")
    assert "[历史任务上下文 task-a1]" in out
    assert "任务A的结论" in out
    assert "任务B的结论" in out


def test_assemble_context_skips_unknown_tasks(tmp_path):
    mem = make_memory(tmp_path)
    assert assemble_context(mem, None, ["task-404"]) == ""


def test_assemble_context_rejects_invalid_task_id(tmp_path):
    mem = make_memory(tmp_path)
    try:
        assemble_context(mem, None, ["../evil"])
        raise AssertionError("should raise")
    except ValueError:
        pass


# ---- brain_api 记忆端点 ----

def make_client(tmp_path, dispatcher=None):
    reg = WorkerRegistry(heartbeat_timeout=45, now_fn=lambda: 0.0)
    store = TaskStore(str(tmp_path / "tasks"))
    cfg = Config(mode="remote", task_store_dir=str(tmp_path / "tasks"), security_llm=False)
    mem = make_memory(tmp_path)
    app = create_brain_app(reg, store, cfg, dispatcher=dispatcher, memory_store=mem)
    return TestClient(app), mem


def test_core_memory_crud_endpoints(tmp_path):
    client, mem = make_client(tmp_path)
    r = client.put("/memory/core/proj-pref", json={"content": "报告必须含改进建议"})
    assert r.json()["status"] == "written"
    assert mem.read_core("proj-pref") == "报告必须含改进建议"

    r = client.get("/memory/core")
    assert {"name": "proj-pref", "content": "报告必须含改进建议"} in r.json()["memories"]

    r = client.get("/memory/core/proj-pref")
    assert r.json()["content"] == "报告必须含改进建议"

    r = client.delete("/memory/core/proj-pref")
    assert r.json()["status"] == "deleted"
    assert client.get("/memory/core/proj-pref").status_code == 404
    assert client.get("/memory/core/missing").status_code == 404


def test_context_memory_endpoints_and_archive(tmp_path):
    client, mem = make_client(tmp_path)
    client.put("/memory/context/task-a1/plan", json={"content": "先拆解再派发"})
    client.put("/memory/context/task-a1/result", json={"content": "status: done"})
    r = client.get("/memory/context/task-a1")
    notes = r.json()["notes"]
    assert notes["plan"] == "先拆解再派发"
    assert notes["result"] == "status: done"

    r = client.delete("/memory/context/task-a1")
    assert r.json()["status"] == "archived"
    assert mem.list_context("task-a1") == []
    assert client.delete("/memory/context/task-a1").json()["status"] == "noop"


def test_memory_endpoints_require_store():
    import tempfile
    from fastapi.testclient import TestClient as TC
    from brain_api import create_brain_app as cab
    reg = WorkerRegistry(heartbeat_timeout=45, now_fn=lambda: 0.0)
    with tempfile.TemporaryDirectory() as td:
        cfg = Config(mode="remote", task_store_dir=td, security_llm=False)
        app = cab(reg, TaskStore(td), cfg)  # 不传 memory_store
        client = TC(app)
        assert client.get("/memory/core").status_code == 400
        assert client.put("/memory/core/x", json={"content": "y"}).status_code == 400


class CapturingDispatcher(Dispatcher):
    def __init__(self):
        self.calls = []

    def dispatch(self, role, prompt, context_memory=None, model=None,
                 use_experience=True, enable_subagents=None,
                 worker_id=None):
        self.calls.append({"role": role, "prompt": prompt,
                           "context_memory": context_memory, "model": model})
        return DispatchResult("task-cap1", "done", output="ok", artifacts=[], error=None)

    def redispatch(self, task_id, model=None, worker_id=None):
        raise KeyError(task_id)


def test_dispatch_context_assembly_e2e(tmp_path):
    disp = CapturingDispatcher()
    client, mem = make_client(tmp_path, dispatcher=disp)
    mem.write_context("task-a1", "result", "任务A的结论")
    r = client.post("/dispatch", json={
        "prompt": "继续任务A的后续工作", "role": "executor",
        "context_task_ids": ["task-a1"], "force": True,
    })
    assert r.status_code == 200
    ctx = disp.calls[0]["context_memory"]
    assert "[历史任务上下文 task-a1]" in ctx
    assert "任务A的结论" in ctx


def test_dispatch_context_clamped(tmp_path):
    disp = CapturingDispatcher()
    client, mem = make_client(tmp_path, dispatcher=disp)
    big = "y" * 20000
    r = client.post("/dispatch", json={
        "prompt": "p", "role": "executor",
        "context_memory": big, "force": True,
    })
    assert r.status_code == 200
    ctx = disp.calls[0]["context_memory"]
    assert len(ctx) <= 9000  # 默认 8000 上限 + 截断标注
    assert "已截断" in ctx


def test_dispatch_rejects_invalid_context_task_id(tmp_path):
    client, _ = make_client(tmp_path, dispatcher=CapturingDispatcher())
    r = client.post("/dispatch", json={
        "prompt": "p", "role": "executor",
        "context_task_ids": ["../evil"], "force": True,
    })
    assert r.status_code == 400


# ---- token 成本优化：检验限幅与模型路由 ----

def test_check_product_caps_combined_input():
    from dispatcher import RemoteDispatcher
    captured = {}

    def product_check(content):
        captured["len"] = len(content)
        return {"verdict": "pass"}

    def fake_http(method, url, **kw):
        return {"status_code": 200, "json": {"content": "z" * 20000}}

    d = RemoteDispatcher(task_store=None, registry=None, http_call=fake_http,
                         product_check=product_check, check_max_chars=6000)
    d._check_product("http://w", "task-x", "head", ["out.md"])
    assert captured["len"] == 6000  # 全量产物不再整包送给 LLM 检验


def make_routing_client(tmp_path, cfg_kwargs, dispatcher):
    reg = WorkerRegistry(heartbeat_timeout=45, now_fn=lambda: 0.0)
    store = TaskStore(str(tmp_path / "tasks"))
    cfg = Config(mode="remote", task_store_dir=str(tmp_path / "tasks"),
                 security_llm=False, **cfg_kwargs)
    app = create_brain_app(reg, store, cfg, dispatcher=dispatcher)
    return TestClient(app)


def test_model_routing_routes_simple_task(tmp_path):
    class Cap(Dispatcher):
        calls = []

        def dispatch(self, role, prompt, context_memory=None, model=None,
                     use_experience=True, enable_subagents=None,
                     worker_id=None):
            Cap.calls.append(model)
            return DispatchResult("task-r1", "done", output="ok", artifacts=[], error=None)

        def redispatch(self, task_id, model=None, worker_id=None):
            raise KeyError(task_id)

    disp = Cap()
    client = make_routing_client(tmp_path, {"model_routing": "heuristic",
                                            "cheap_model": "haiku"}, disp)
    r = client.post("/dispatch", json={
        "prompt": "修复 README 里的一个错别字", "role": "executor", "force": True})
    assert disp.calls[-1] == "haiku"  # 短任务、无技能标记 → 便宜模型
    assert r.json()["model"] == "haiku"


def test_model_routing_skips_skill_and_long_tasks(tmp_path):
    class Cap(Dispatcher):
        calls = []

        def dispatch(self, role, prompt, context_memory=None, model=None,
                     use_experience=True, enable_subagents=None,
                     worker_id=None):
            Cap.calls.append(model)
            return DispatchResult("task-r2", "done", output="ok", artifacts=[], error=None)

        def redispatch(self, task_id, model=None, worker_id=None):
            raise KeyError(task_id)

    disp = Cap()
    client = make_routing_client(tmp_path, {"model_routing": "heuristic"}, disp)
    client.post("/dispatch", json={
        "prompt": "[web-expert] 阶段：S0；任务：出 spec", "role": "executor", "force": True})
    assert disp.calls[-1] is None  # 技能流程任务不降级
    client.post("/dispatch", json={
        "prompt": "长任务 " * 200, "role": "executor", "force": True})
    assert disp.calls[-1] is None  # 超长 prompt 不降级


def test_model_routing_respects_explicit_model(tmp_path):
    class Cap(Dispatcher):
        calls = []

        def dispatch(self, role, prompt, context_memory=None, model=None,
                     use_experience=True, enable_subagents=None,
                     worker_id=None):
            Cap.calls.append(model)
            return DispatchResult("task-r3", "done", output="ok", artifacts=[], error=None)

        def redispatch(self, task_id, model=None, worker_id=None):
            raise KeyError(task_id)

    disp = Cap()
    client = make_routing_client(tmp_path, {"model_routing": "heuristic"}, disp)
    client.post("/dispatch", json={
        "prompt": "短任务", "role": "executor", "model": "sonnet", "force": True})
    assert disp.calls[-1] == "sonnet"  # 显式指定优先
