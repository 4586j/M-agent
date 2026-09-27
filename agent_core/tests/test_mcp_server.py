"""测试 agent_core.mcp_agent_server：session_close 防泄漏、只读记忆、persona 兜底警告。

直接调用被 @mcp.tool() 注册的原函数（FastMCP 装饰器返回原函数），
内核/配置全部替换为假对象，不触真实 SDK。
"""
import asyncio
import logging
from pathlib import Path

import pytest

import agent_core.mcp_agent_server as mcp_mod
from agent_core.config import load_agent_config


class FakeMCPSession:
    def __init__(self):
        self.closed = False

    async def close(self):
        self.closed = True


class BoomCloseSession:
    async def close(self):
        raise RuntimeError("子进程已死")


@pytest.fixture
def cfg(tmp_path, project_root):
    # project_root 用真实仓库根（记忆后端延迟导入 runtime）；
    # persona / work / 记忆 / 经验目录全部显式隔离到 tmp_path。
    # .env 隔离由 conftest 的 autouse 夹具统一负责（AGENT_ENV_FILE 哨兵路径）。
    return load_agent_config(
        project_root=project_root,
        work_dir=str(tmp_path / "work"),
        persona_dir=str(tmp_path / "claude-agents"),
        memory_backend="local",
        memory_core_dir=str(tmp_path / "mem" / "core"),
        memory_context_dir=str(tmp_path / "mem" / "context"),
        experience_store_dir=str(tmp_path / "exp"),
        mcp_config_path="",
    )


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, cfg):
    monkeypatch.setattr(mcp_mod, "_CONFIG", cfg)
    monkeypatch.setattr(mcp_mod, "_KERNEL", object())
    monkeypatch.setattr(mcp_mod, "_SESSIONS", {})
    monkeypatch.setattr(mcp_mod, "_PERSONAS", {})
    yield


# ---- agent_session_close（修复只能建不能关的子进程泄漏） ----

def test_session_close_removes_and_closes():
    fake = FakeMCPSession()
    mcp_mod._SESSIONS["s1"] = {"session": fake, "cwd": "x", "persona": "p"}
    out = asyncio.run(mcp_mod.agent_session_close("s1"))
    assert out == {"status": "closed", "session_id": "s1"}
    assert fake.closed is True
    assert "s1" not in mcp_mod._SESSIONS  # 已移除，无泄漏


def test_session_close_unknown_id():
    out = asyncio.run(mcp_mod.agent_session_close("ghost"))
    assert out["status"] == "error"


def test_session_close_tolerates_close_exception():
    mcp_mod._SESSIONS["s2"] = {"session": BoomCloseSession(), "cwd": "x",
                               "persona": "p"}
    out = asyncio.run(mcp_mod.agent_session_close("s2"))
    assert out["status"] == "closed_with_warning"
    assert "s2" not in mcp_mod._SESSIONS


# ---- 只读记忆工具 ----

def test_memory_list_empty_and_backend(tmp_path):
    out = mcp_mod.agent_memory_list()
    assert out["memories"] == []
    assert out["backend"] == "local"


def test_memory_read_local_roundtrip(tmp_path):
    core = tmp_path / "mem" / "core"
    core.mkdir(parents=True)
    (core / "note.md").write_text("记住这件事", encoding="utf-8")
    out = asyncio.run(mcp_mod.agent_memory_read("note"))
    assert out == {"status": "ok", "name": "note", "content": "记住这件事"}


def test_memory_read_not_found():
    out = asyncio.run(mcp_mod.agent_memory_read("nothere"))
    assert out["status"] == "not_found"
    assert "agent_memory_list" in out["detail"]


# ---- resolve_persona 未命中告警（列出可用人设名） ----

def test_resolve_persona_unknown_warns_with_available_names(caplog, tmp_path, cfg):
    agents = Path(cfg.persona_dir)
    agents.mkdir(parents=True)
    (agents / "known.md").write_text("---\nname: known\n---\nx",
                                     encoding="utf-8")
    with caplog.at_level(logging.WARNING, logger="agent.mcp"):
        p = mcp_mod.resolve_persona("mystery")
    assert p.name == "mystery"
    assert any("mystery" in r.message and "known" in r.message
               for r in caplog.records)
    assert mcp_mod._PERSONAS["mystery"] is p  # 已缓存


def test_resolve_persona_orchestrator_fallback_warns(caplog, cfg):
    Path(cfg.persona_dir).mkdir(parents=True, exist_ok=True)
    with caplog.at_level(logging.WARNING, logger="agent.mcp"):
        p = mcp_mod.resolve_persona("orchestrator")
    assert p.name == "orchestrator"
    assert any("orchestrator" in r.message for r in caplog.records)


def test_resolve_persona_known_name_no_warning(caplog, cfg):
    agents = Path(cfg.persona_dir)
    agents.mkdir(parents=True, exist_ok=True)
    (agents / "real.md").write_text("---\nname: real\n---\n正文",
                                    encoding="utf-8")
    with caplog.at_level(logging.WARNING, logger="agent.mcp"):
        p = mcp_mod.resolve_persona("real")
    assert p.name == "real"
    assert not [r for r in caplog.records if "real" in r.message]
