"""worker 配置默认值与环境变量覆盖（阶段一安全加固：permission_mode 收敛为 default）。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from worker_config import WorkerConfig, load_worker_config


def test_default_permission_mode_is_default():
    """dataclass 默认值收敛为 default；bypassPermissions 须显式指定。"""
    cfg = WorkerConfig()
    assert cfg.permission_mode == "default"


def test_load_worker_config_default(monkeypatch):
    monkeypatch.delenv("ORCH_PERMISSION_MODE", raising=False)
    assert load_worker_config().permission_mode == "default"


def test_load_worker_config_env_override(monkeypatch):
    """环境变量覆盖机制保留：显式 ORCH_PERMISSION_MODE=bypassPermissions 生效。"""
    monkeypatch.setenv("ORCH_PERMISSION_MODE", "bypassPermissions")
    assert load_worker_config().permission_mode == "bypassPermissions"


# ---- 输出质量守卫三配置（集群模式透传给 kernel 的前提）----

def test_output_guard_defaults():
    """守卫三配置默认值：开启、重试 1 次（与 agent_core 同步）、不换模型。"""
    cfg = WorkerConfig()
    assert cfg.output_guard_enabled is True
    assert cfg.output_guard_max_retries == 1
    assert cfg.fallback_model == ""


def test_load_worker_config_output_guard_agent_prefix(monkeypatch):
    """AGENT_ 前缀设值生效（AGENT_ 优先）。"""
    for key in ("ORCH_OUTPUT_GUARD_ENABLED", "ORCH_OUTPUT_GUARD_MAX_RETRIES",
                "ORCH_FALLBACK_MODEL", "AGENT_OUTPUT_GUARD_ENABLED",
                "AGENT_OUTPUT_GUARD_MAX_RETRIES", "AGENT_FALLBACK_MODEL"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("AGENT_OUTPUT_GUARD_ENABLED", "0")
    monkeypatch.setenv("AGENT_OUTPUT_GUARD_MAX_RETRIES", "2")
    monkeypatch.setenv("AGENT_FALLBACK_MODEL", "fb")
    cfg = load_worker_config()
    assert cfg.output_guard_enabled is False
    assert cfg.output_guard_max_retries == 2
    assert cfg.fallback_model == "fb"


# ---- role 人设（单机多 agent：ORCH_WORKER_ROLE 决定人设与派发身份）----

def test_default_role_is_executor(monkeypatch):
    monkeypatch.delenv("ORCH_WORKER_ROLE", raising=False)
    monkeypatch.delenv("ORCH_WORKER_PERSONA_DEF", raising=False)
    monkeypatch.delenv("ORCH_EXECUTOR_DEF", raising=False)
    cfg = load_worker_config()
    assert cfg.role == "executor"
    assert cfg.persona_def_path.endswith(f"executor.md")


def test_role_env_overrides_and_persona_follows(monkeypatch):
    """ORCH_WORKER_ROLE 覆盖角色，人设路径默认跟随 role（role 即人设名）。"""
    monkeypatch.delenv("ORCH_WORKER_PERSONA_DEF", raising=False)
    monkeypatch.delenv("ORCH_EXECUTOR_DEF", raising=False)
    monkeypatch.setenv("ORCH_WORKER_ROLE", "security-guard")
    cfg = load_worker_config()
    assert cfg.role == "security-guard"
    assert cfg.persona_def_path.endswith("security-guard.md")


def test_persona_def_override_preferred_then_compat(monkeypatch):
    """ORCH_WORKER_PERSONA_DEF 优先；旧名 ORCH_EXECUTOR_DEF 兼容回退。"""
    monkeypatch.delenv("ORCH_EXECUTOR_DEF", raising=False)
    monkeypatch.setenv("ORCH_WORKER_ROLE", "data-expert")
    monkeypatch.setenv("ORCH_WORKER_PERSONA_DEF", "/custom/persona.md")
    assert load_worker_config().persona_def_path == "/custom/persona.md"
    monkeypatch.delenv("ORCH_WORKER_PERSONA_DEF")
    monkeypatch.setenv("ORCH_EXECUTOR_DEF", "/legacy/executor.md")
    assert load_worker_config().persona_def_path == "/legacy/executor.md"


def test_load_worker_config_output_guard_orch_fallback(monkeypatch):
    """AGENT_ 未设置时回退 ORCH_ 前缀（与集群既有 ORCH_ 配置并存）。"""
    for key in ("AGENT_OUTPUT_GUARD_ENABLED", "AGENT_OUTPUT_GUARD_MAX_RETRIES",
                "AGENT_FALLBACK_MODEL"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("ORCH_OUTPUT_GUARD_ENABLED", "false")
    monkeypatch.setenv("ORCH_OUTPUT_GUARD_MAX_RETRIES", "3")
    monkeypatch.setenv("ORCH_FALLBACK_MODEL", "orch-fb")
    cfg = load_worker_config()
    assert cfg.output_guard_enabled is False
    assert cfg.output_guard_max_retries == 3
    assert cfg.fallback_model == "orch-fb"
