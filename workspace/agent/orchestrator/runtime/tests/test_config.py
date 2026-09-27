import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from config import Config, load_config


def test_default_config_is_local(monkeypatch):
    # 自防：宿主环境可能注入 ORCH_MODE（如 remote），先清理避免假失败
    monkeypatch.delenv("ORCH_MODE", raising=False)
    monkeypatch.delenv("ORCH_SECURITY_LLM", raising=False)
    cfg = load_config()
    assert cfg.mode == "local"
    assert cfg.heartbeat_interval == 15
    assert cfg.heartbeat_timeout == 45
    assert cfg.brain_url == "http://127.0.0.1:7000"
    # 阶段一安全加固：security_llm 默认开启（LLM 缺失时规则引擎兜底）
    assert cfg.security_llm is True


def test_security_llm_env_disable(monkeypatch):
    """ORCH_SECURITY_LLM=0/false 显式关闭 LLM 增强。"""
    monkeypatch.setenv("ORCH_SECURITY_LLM", "0")
    assert load_config().security_llm is False
    monkeypatch.setenv("ORCH_SECURITY_LLM", "false")
    assert load_config().security_llm is False
    monkeypatch.setenv("ORCH_SECURITY_LLM", "1")
    assert load_config().security_llm is True


def test_config_reads_env(monkeypatch):
    monkeypatch.setenv("ORCH_MODE", "remote")
    monkeypatch.setenv("ORCH_BRAIN_URL", "http://10.0.0.1:7000")
    cfg = load_config()
    assert cfg.mode == "remote"
    assert cfg.brain_url == "http://10.0.0.1:7000"


def test_invalid_mode_falls_back_to_local(monkeypatch):
    monkeypatch.setenv("ORCH_MODE", "bogus")
    cfg = load_config()
    assert cfg.mode == "local"
