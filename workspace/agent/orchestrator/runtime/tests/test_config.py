import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from config import Config, load_config


def test_default_config_is_local():
    cfg = load_config()
    assert cfg.mode == "local"
    assert cfg.heartbeat_interval == 15
    assert cfg.heartbeat_timeout == 45
    assert cfg.brain_url == "http://127.0.0.1:7000"


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
