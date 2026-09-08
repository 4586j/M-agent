"""测试 agent_core.config：默认值、环境变量覆盖、默认路径定位。"""
import os
from pathlib import Path

import pytest

from agent_core.config import (AgentConfig, _to_bool, _to_optional_float,
                               _to_optional_int, load_agent_config)


def _clear_agent_env(monkeypatch):
    """清掉所有 AGENT_ / ORCH_ 前缀环境变量，确保默认值断言不受宿主环境干扰。"""
    for key in list(os.environ.keys()):
        if key.startswith("AGENT_") or key.startswith("ORCH_"):
            monkeypatch.delenv(key, raising=False)


# ---- 默认值 ----

def test_load_agent_config_returns_agentconfig(monkeypatch):
    _clear_agent_env(monkeypatch)
    cfg = load_agent_config()
    assert isinstance(cfg, AgentConfig)


def test_default_values(monkeypatch):
    _clear_agent_env(monkeypatch)
    cfg = load_agent_config()
    # 模型与执行控制
    assert cfg.model is None
    assert cfg.max_turns == 50
    assert cfg.max_thinking_tokens == 10000
    assert cfg.task_budget_usd is None
    assert cfg.task_timeout == 900
    assert cfg.permission_mode == "bypassPermissions"
    # 记忆后端与大脑地址
    assert cfg.memory_backend == "local"
    assert cfg.brain_url == "http://127.0.0.1:7000"
    assert cfg.api_key == ""
    # 增强功能开关
    assert cfg.planning_enabled is False
    assert cfg.reflection_enabled is False
    assert cfg.rag_enabled is False
    assert cfg.streaming is True
    assert cfg.auto_record_experience is True


# ---- 环境变量覆盖 ----

def test_env_override_model_and_turns(monkeypatch):
    _clear_agent_env(monkeypatch)
    monkeypatch.setenv("AGENT_MODEL", "opus")
    monkeypatch.setenv("AGENT_MAX_TURNS", "7")
    monkeypatch.setenv("AGENT_MAX_THINKING_TOKENS", "4096")
    monkeypatch.setenv("AGENT_TASK_TIMEOUT", "120")
    monkeypatch.setenv("AGENT_PERMISSION_MODE", "acceptEdits")
    cfg = load_agent_config()
    assert cfg.model == "opus"
    assert cfg.max_turns == 7
    assert cfg.max_thinking_tokens == 4096
    assert cfg.task_timeout == 120
    assert cfg.permission_mode == "acceptEdits"


def test_env_override_orch_fallback(monkeypatch):
    """AGENT_ 未设置时回退到 ORCH_ 前缀。"""
    _clear_agent_env(monkeypatch)
    monkeypatch.setenv("ORCH_MODEL", "haiku")
    monkeypatch.setenv("ORCH_MAX_TURNS", "3")
    cfg = load_agent_config()
    assert cfg.model == "haiku"
    assert cfg.max_turns == 3


def test_agent_prefix_takes_priority_over_orch(monkeypatch):
    _clear_agent_env(monkeypatch)
    monkeypatch.setenv("AGENT_MODEL", "opus")
    monkeypatch.setenv("ORCH_MODEL", "haiku")
    cfg = load_agent_config()
    assert cfg.model == "opus"  # AGENT_ 优先


def test_env_override_bool_flags(monkeypatch):
    _clear_agent_env(monkeypatch)
    monkeypatch.setenv("AGENT_PLANNING_ENABLED", "true")
    monkeypatch.setenv("AGENT_REFLECTION_ENABLED", "1")
    monkeypatch.setenv("AGENT_RAG_ENABLED", "yes")
    monkeypatch.setenv("AGENT_STREAMING", "0")
    cfg = load_agent_config()
    assert cfg.planning_enabled is True
    assert cfg.reflection_enabled is True
    assert cfg.rag_enabled is True
    assert cfg.streaming is False


def test_env_override_budget_and_backend(monkeypatch):
    _clear_agent_env(monkeypatch)
    monkeypatch.setenv("AGENT_TASK_BUDGET_USD", "0.5")
    monkeypatch.setenv("AGENT_MEMORY_BACKEND", "remote")
    monkeypatch.setenv("AGENT_BRAIN_URL", "http://10.0.0.1:9000")
    monkeypatch.setenv("AGENT_API_KEY", "secret-key")
    cfg = load_agent_config()
    assert cfg.task_budget_usd == 0.5
    assert cfg.memory_backend == "remote"
    assert cfg.brain_url == "http://10.0.0.1:9000"
    assert cfg.api_key == "secret-key"


def test_kwargs_overrides_win_over_env(monkeypatch):
    _clear_agent_env(monkeypatch)
    monkeypatch.setenv("AGENT_MODEL", "opus")
    cfg = load_agent_config(model="sonnet", max_turns=99)
    assert cfg.model == "sonnet"   # 显式 override 压过环境变量
    assert cfg.max_turns == 99


def test_kwargs_override_ignores_unknown_key(monkeypatch):
    _clear_agent_env(monkeypatch)
    cfg = load_agent_config(not_a_field="x")  # 未知键被忽略，不报错
    assert not hasattr(cfg, "not_a_field")


# ---- 默认路径定位 ----

def test_default_paths_locate_real_dirs(monkeypatch):
    _clear_agent_env(monkeypatch)
    cfg = load_agent_config()
    assert Path(cfg.project_root).is_dir()
    assert Path(cfg.persona_dir).is_dir()
    # persona_dir 应指向 .claude/agents
    assert Path(cfg.persona_dir).name == "agents"
    assert Path(cfg.persona_dir).parent.name == ".claude"
    # work_dir 基于项目根定位
    assert Path(cfg.work_dir).is_absolute()


def test_default_memory_dirs_under_project_root(monkeypatch):
    _clear_agent_env(monkeypatch)
    cfg = load_agent_config()
    root = Path(cfg.project_root).resolve()
    assert Path(cfg.memory_core_dir).is_absolute()
    assert root in Path(cfg.memory_core_dir).parents or Path(cfg.memory_core_dir) == root
    assert Path(cfg.experience_store_dir).is_absolute()


def test_env_override_paths(monkeypatch, tmp_path):
    _clear_agent_env(monkeypatch)
    monkeypatch.setenv("AGENT_PROJECT_ROOT", str(tmp_path))
    monkeypatch.setenv("AGENT_WORK_DIR", str(tmp_path / "work"))
    monkeypatch.setenv("AGENT_PERSONA_DIR", str(tmp_path / "agents"))
    cfg = load_agent_config()
    assert cfg.project_root == str(tmp_path)
    assert cfg.work_dir == str(tmp_path / "work")
    assert cfg.persona_dir == str(tmp_path / "agents")


# ---- 内部解析辅助函数 ----

@pytest.mark.parametrize("raw,default,expected", [
    ("", False, False),
    ("", True, True),
    ("1", False, True),
    ("true", False, True),
    ("YES", False, True),
    ("on", False, True),
    ("0", True, False),
    ("off", True, False),
    ("garbage", True, False),
])
def test_to_bool(raw, default, expected):
    assert _to_bool(raw, default) is expected


@pytest.mark.parametrize("raw,expected", [
    ("", None),
    ("   ", None),
    ("0", None),        # 0 视为不限制
    ("10000", 10000),
    ("abc", None),
])
def test_to_optional_int(raw, expected):
    assert _to_optional_int(raw) == expected


@pytest.mark.parametrize("raw,expected", [
    ("", None),
    ("0", None),        # 0 视为不限制
    ("0.5", 0.5),
    ("abc", None),
])
def test_to_optional_float(raw, expected):
    assert _to_optional_float(raw) == expected
