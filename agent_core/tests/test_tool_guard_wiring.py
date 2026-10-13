"""tool_guard / tool_response_guard 接线测试。

config.tool_guard_enabled=True（默认）时，记忆工具 handler 必须真实经过
guarded_tool_call：输入预检、超时、读缓存、记忆工具失败重试、响应结构校验、
写后缓存失效。此前配置默认 True 但 kernel 零引用——"说谎的开关"，本文件
把接线行为钉死。
"""
import asyncio
import json

import pytest

from agent_core import tools as tools_mod
from agent_core.config import AgentConfig
from agent_core.tool_guard import _tool_cache


@pytest.fixture()
def local_cfg(tmp_path):
    return AgentConfig(experience_store_dir=str(tmp_path / "exp"),
                       memory_core_dir=str(tmp_path / "core"),
                       memory_context_dir=str(tmp_path / "ctx"),
                       work_dir=str(tmp_path))


@pytest.fixture(autouse=True)
def _clean_cache():
    _tool_cache.clear()
    yield
    _tool_cache.clear()


def _tool_by_name(cfg, name):
    """build_memory_tools 返回的 SdkMcpTool：取 name 与裸 handler。"""
    for t in tools_mod.build_memory_tools(cfg):
        tname = getattr(t, "name", None) or (t.get("name") if isinstance(t, dict) else None)
        if tname == name:
            handler = getattr(t, "func", None) or getattr(t, "handler", None) or t
            return handler
    raise AssertionError(f"tool {name} not registered")


def test_memory_tools_register(local_cfg):
    cfg = local_cfg
    names = [getattr(t, "name", None) or (t.get("name") if isinstance(t, dict) else None)
             for t in tools_mod.build_memory_tools(cfg)]
    assert names == ["search_experiences", "read_core_memory",
                     "write_core_memory", "write_task_context"]


def test_memory_cache_separates_local_stores(tmp_path):
    async def scenario():
        from agent_core.tools import _TOOL_SPECS, _guarded_run, _make_local_backend
        spec = next(s for s in _TOOL_SPECS if s["name"] == "read_core_memory")
        outputs = []
        for name in ("A", "B"):
            cfg = AgentConfig(memory_core_dir=str(tmp_path / name / "core"),
                memory_context_dir=str(tmp_path / name / "ctx"),
                experience_store_dir=str(tmp_path / name / "exp"))
            backend = _make_local_backend(cfg)
            await backend["write_core_memory"]("same-name", name)
            result = await _guarded_run(cfg, spec, backend, {"name": "same-name"})
            outputs.append(json.loads(result["content"][0]["text"])["content"])
        assert outputs == ["A", "B"]
    asyncio.run(scenario())


def test_memory_cache_separates_remote_credentials():
    async def scenario():
        from agent_core.tools import _TOOL_SPECS, _guarded_run
        spec = next(s for s in _TOOL_SPECS if s["name"] == "read_core_memory")
        for secret in ("key-A", "key-B"):
            cfg = AgentConfig(memory_backend="remote", brain_url="https://example.invalid", api_key=secret)
            async def read(name, value=secret):
                return {"content": value}
            result = await _guarded_run(cfg, spec, {"read_core_memory": read}, {"name": "same"})
            assert json.loads(result["content"][0]["text"])["content"] == secret
        assert all("key-A" not in key and "key-B" not in key for key in _tool_cache._store)
    asyncio.run(scenario())


def test_inflight_read_does_not_repopulate_cache_after_write():
    from agent_core.tool_guard import guarded_tool_call, invalidate_tool_cache
    async def scenario():
        entered, release = asyncio.Event(), asyncio.Event()
        async def stale_read(args):
            entered.set()
            await release.wait()
            return {"content": [{"type": "text", "text": "old"}]}
        read = asyncio.create_task(guarded_tool_call("read_core_memory", stale_read,
                                  {"name": "x"}, cache_scope="scope"))
        await entered.wait()
        invalidate_tool_cache("scope:")
        release.set()
        await read
        assert not _tool_cache._store
    asyncio.run(scenario())


def test_write_success_returns_mcp_format(local_cfg):
    """写工具成功：守卫路径返回 MCP 文本格式（与裸 _run_tool 一致）。"""
    handler = _tool_by_name(local_cfg, "write_task_context")
    result = asyncio.run(handler(
        {"task_id": "t1", "key": "k", "content": "v"}))
    assert not result.get("is_error")
    text = result["content"][0]["text"]
    assert json.loads(text)["status"] == "written"


def test_read_result_cached_on_second_call(local_cfg):
    """读工具（search_experiences）第二次同参调用命中缓存。"""
    handler = _tool_by_name(local_cfg, "search_experiences")
    first = asyncio.run(handler({"query": "部署"}))
    second = asyncio.run(handler({"query": "部署"}))
    assert not first.get("is_error")
    assert second == first  # 命中缓存：返回与首次完全一致的对象


def test_write_invalidates_read_cache(local_cfg):
    """写工具成功后失效读缓存（避免读到陈旧快照）。"""
    search = _tool_by_name(local_cfg, "search_experiences")
    write = _tool_by_name(local_cfg, "write_task_context")
    asyncio.run(search({"query": "缓存失效检查"}))
    assert len(_tool_cache._store) == 1
    asyncio.run(write({"task_id": "t9", "key": "k", "content": "v"}))
    assert len(_tool_cache._store) == 0


def test_guard_disabled_uses_bare_run_tool(local_cfg, monkeypatch):
    """tool_guard_enabled=False：handler 退回裸 _run_tool（无缓存/无守卫）。"""
    disabled = AgentConfig(experience_store_dir=local_cfg.experience_store_dir,
                           memory_core_dir=local_cfg.memory_core_dir,
                           memory_context_dir=local_cfg.memory_context_dir,
                           work_dir=local_cfg.work_dir,
                           tool_guard_enabled=False)
    called = []
    real_run_tool = tools_mod._run_tool

    def spy_run_tool(backend, spec, args):
        called.append(spec["name"])
        return real_run_tool(backend, spec, args)

    monkeypatch.setattr(tools_mod, "_run_tool", spy_run_tool)
    handler = _tool_by_name(disabled, "search_experiences")
    result = asyncio.run(handler({"query": "关守卫"}))
    assert not result.get("is_error")
    assert called == ["search_experiences"]  # 走裸路径
    assert len(_tool_cache._store) == 0      # 未走缓存


def test_backend_error_retried_then_reported(local_cfg, monkeypatch):
    """记忆工具失败自动重试 1 次（guarded_tool_call 契约），最终以 is_error 返回。"""
    calls = {"n": 0}

    async def boom(query, role=None, successful_only=True, limit=5):
        calls["n"] += 1
        raise RuntimeError("后端不可达")

    # 后端是 dict（name -> 协程函数），与 _make_local_backend 形状一致
    backend = {"search_experiences": boom}
    spec = next(s for s in tools_mod._TOOL_SPECS if s["name"] == "search_experiences")
    result = asyncio.run(tools_mod._guarded_run(
        local_cfg, spec, backend, {"query": "x"}))
    assert calls["n"] == 2  # 1 次原始 + 1 次重试
    assert result.get("is_error") is True
    assert "失败" in result["content"][0]["text"]
