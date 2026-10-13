"""Opt-in real SDK contract test; never runs in the ordinary deterministic suite."""
import os

import pytest

from agent_core.config import AgentConfig
from agent_core.kernel import AgentKernel
from agent_core.persona import Persona


@pytest.mark.sdk_smoke
@pytest.mark.skipif(os.environ.get("RUN_AGENT_SDK_SMOKE") != "1",
                    reason="explicit opt-in required for a paid SDK call")
def test_real_sdk_minimal_response(tmp_path):
    assert os.environ.get("ANTHROPIC_API_KEY"), "ANTHROPIC_API_KEY is required"
    config = AgentConfig(work_dir=str(tmp_path), project_root=str(tmp_path),
        max_turns=2, task_timeout=60, task_budget_usd=0.05,
        mcp_servers={}, auto_record_experience=False,
        output_guard_enabled=False, reflection_enabled=False, rag_enabled=False)
    result = AgentKernel(config).run_oneshot(
        "Reply with exactly SDK_SMOKE_OK. Do not use any tools.",
        Persona("smoke", system_prompt="Follow the user instruction."), str(tmp_path))
    assert not result.is_error, result.error_type
    assert "SDK_SMOKE_OK" in result.output
