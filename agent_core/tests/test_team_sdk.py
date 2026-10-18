"""Team kernel isolation is independent of personal and legacy orchestration state."""
import json
import subprocess
import threading
from pathlib import Path

import pytest

from agent_core.config import AgentConfig
from agent_core.persona import Persona
from agent_core.team_sdk import IsolatedSdk, TeamConfig, TeamKernel, cost_units, tool_decision


def test_explicit_empty_tools_does_not_enable_memory_or_builtin_tools(tmp_path):
    kernel = TeamKernel(AgentConfig(project_root=str(tmp_path)))
    options = kernel._build_options(Persona("planner", tools=[], system_prompt="Frozen planning"), str(tmp_path))
    assert options.tools == []
    assert options.allowed_tools == []
    assert options.mcp_servers == {}
    assert not options.agents and not options.resume
    assert options.system_prompt == "Frozen planning"
    assert options.setting_sources == []
    assert "PreToolUse" in options.hooks


@pytest.mark.parametrize("tool,inputs,expected", [
    ("Read", {"file_path": "inputs/source.txt"}, True),
    ("Write", {"file_path": "outputs/code.py"}, True),
    ("Edit", {"file_path": "inputs/source.txt"}, False),
    ("Read", {"file_path": "../credential"}, False),
    ("Read", {"file_path": ".sdk-home/secret"}, False),
    ("Glob", {"pattern": "**/*.py"}, True),
    ("Glob", {"pattern": "../*"}, False),
    ("Grep", {"path": ".", "pattern": "TOKEN"}, False),
    ("Write", {"file_path": "outputs/.claude/settings.json"}, False),
    ("Bash", {"command": "echo hi"}, False),
    ("Agent", {"prompt": "delegate"}, False),
])
def test_file_policy(tmp_path, tool, inputs, expected):
    assert tool_decision(tool, inputs, tmp_path.resolve(), {"Read", "Write", "Edit", "Glob", "Grep"}) is expected


def test_outside_symlink_is_rejected(tmp_path):
    (tmp_path / "outputs").mkdir()
    try:
        (tmp_path / "outputs" / "link").symlink_to(tmp_path.parent, target_is_directory=True)
    except OSError:
        pytest.skip("Symlink creation unavailable")
    assert not tool_decision("Read", {"file_path": "outputs/link/secret"}, tmp_path.resolve(), {"Read"})


def test_cost_estimates_never_turn_unknown_into_zero():
    assert cost_units(None) is None
    assert cost_units(float("nan")) is None
    assert cost_units(-1) is None
    assert cost_units(True) is None
    assert cost_units(0) == 0
    assert cost_units(0.0000001) == 1
    assert cost_units(0.0123) == 12300


def test_child_receives_only_selected_provider_credentials(tmp_path, monkeypatch):
    config_path = tmp_path / "sdk.json"
    config_path.write_text(json.dumps({"version": 1, "providers": {"p": {
        "model": "model", "base_url": "https://example.com/api", "auth_token_env": "TEAM_TEST_TOKEN"}}}))
    monkeypatch.setenv("TEAM_TEST_TOKEN", "selected-test-credential")
    monkeypatch.setenv("COLLAB_INSTANCE_TOKEN", "not-for-child")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "personal-not-for-child")
    monkeypatch.setenv("NODE_OPTIONS", "not-for-child")
    seen = {}
    class Process:
        returncode = 0
        def communicate(self, input=None, timeout=None):
            seen["payload"] = json.loads(input)
            return b'{"output":"ok","cost_usd":0.001}', b""
    def popen(command, **kwargs):
        seen.update(kwargs)
        return Process()
    monkeypatch.setattr(subprocess, "Popen", popen)
    result = IsolatedSdk(TeamConfig(config_path)).invoke(
        prompt="task", system_prompt="duty", cwd=tmp_path / "run", tools=[], provider_ref="p",
        max_cost_usd=0.01, max_turns=1, timeout_seconds=10, cancel=threading.Event())
    assert result.output == "ok"
    assert seen["env"]["ANTHROPIC_AUTH_TOKEN"] == "selected-test-credential"
    assert all(key not in seen["env"] for key in ("TEAM_TEST_TOKEN", "COLLAB_INSTANCE_TOKEN", "NODE_OPTIONS"))
    assert "credential" not in json.dumps(seen["payload"])
    assert Path(seen["env"]["HOME"]).is_relative_to(tmp_path)
