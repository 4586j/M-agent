"""Opt-in real SDK contract test; never runs in the ordinary deterministic suite."""
import json
import os
from pathlib import Path

import pytest

from agent_core.config import AgentConfig
from agent_core.kernel import AgentKernel
from agent_core.persona import Persona


def _text_only_kernel(kernel, monkeypatch, before_query=None):
    build_options = kernel._build_options

    def text_only_options(*args, **kwargs):
        if before_query:
            before_query()
        options = build_options(*args, **kwargs)
        options.tools = []
        options.mcp_servers = {}
        return options

    monkeypatch.setattr(kernel, "_build_options", text_only_options)


@pytest.mark.sdk_smoke
@pytest.mark.skipif(os.environ.get("RUN_AGENT_SDK_SMOKE") != "1",
                    reason="explicit opt-in required for a paid SDK call")
def test_real_sdk_minimal_response(tmp_path, monkeypatch):
    has_auth = bool(os.environ.get("ANTHROPIC_API_KEY") or (
        os.environ.get("ANTHROPIC_AUTH_TOKEN") and os.environ.get("ANTHROPIC_BASE_URL")))
    assert has_auth, "Set ANTHROPIC_API_KEY or ANTHROPIC_AUTH_TOKEN + ANTHROPIC_BASE_URL"
    config = AgentConfig(work_dir=str(tmp_path), project_root=str(tmp_path),
        model=os.environ.get("ANTHROPIC_MODEL") or None,
        max_turns=2, task_timeout=60, task_budget_usd=0.05,
        mcp_servers={}, auto_record_experience=False,
        output_guard_enabled=False, reflection_enabled=False, rag_enabled=False,
        correction_enabled=False, subagents_enabled=False)
    kernel = AgentKernel(config)
    _text_only_kernel(kernel, monkeypatch)
    result = kernel.run_oneshot(
        "Reply with exactly SDK_SMOKE_OK. Do not use any tools.",
        Persona("smoke", system_prompt="Follow the user instruction."), str(tmp_path))
    assert not result.is_error, result.error_type
    assert "SDK_SMOKE_OK" in result.output
    print(json.dumps({"model": config.model, "output": result.output,
        "cost_usd": result.cost_usd, "usage": result.usage,
        "turns": result.turns, "duration_ms": result.duration_ms}, ensure_ascii=True))


@pytest.mark.sdk_smoke
@pytest.mark.skipif(os.environ.get("RUN_AGENT_SDK_SMOKE") != "1",
                    reason="explicit opt-in required for a paid SDK call")
def test_real_sdk_budget_settlement(tmp_path, monkeypatch):
    """Real model cost flows through local dispatch, then replays without a new call."""
    runtime = Path(__file__).resolve().parents[2] / "workspace/agent/orchestrator/runtime"
    monkeypatch.syspath_prepend(str(runtime))
    from budget_store import BudgetStore
    from cost_accounting import replay_costs
    from dispatch_context import AdmittedDispatcher, BudgetAdmissionRejected
    from local_executor_dispatcher import LocalExecutorDispatcher

    from task_store import TaskStore

    has_auth = bool(os.environ.get("ANTHROPIC_API_KEY") or (
        os.environ.get("ANTHROPIC_AUTH_TOKEN") and os.environ.get("ANTHROPIC_BASE_URL")))
    assert has_auth, "Set ANTHROPIC_API_KEY or ANTHROPIC_AUTH_TOKEN + ANTHROPIC_BASE_URL"
    config = AgentConfig(project_root=str(tmp_path), work_dir=str(tmp_path / "work"),
        persona_dir=str(tmp_path / "personas"), model=os.environ.get("ANTHROPIC_MODEL") or None,
        max_turns=2, task_timeout=60, task_budget_usd=0.05, mcp_servers={},
        auto_record_experience=False, output_guard_enabled=False, reflection_enabled=False,
        rag_enabled=False, correction_enabled=False, subagents_enabled=False)
    store = TaskStore(str(tmp_path / "tasks"))
    book_path = str(tmp_path / "budget.json")
    book = BudgetStore(book_path)
    book.set_limits(global_limit_usd=0.10)
    legacy_costs = []
    delegate = LocalExecutorDispatcher(store, config=config, on_spend=legacy_costs.append)
    observed = []
    _text_only_kernel(delegate._kernel, monkeypatch,
                      before_query=lambda: observed.append(book.snapshot()["reserved_usd"]))
    guarded = AdmittedDispatcher(delegate, book, lambda _: {"verdict": "pass"}, store)
    result = guarded.dispatch("smoke", "Reply with exactly ARK_BUDGET_OK. Do not use any tools.",
                              use_experience=False)
    assert result.status == "done", result.status
    rec = store.get(result.task_id)
    assert "ARK_BUDGET_OK" in rec.output
    assert observed == [0.05]
    assert rec.cost_report is not None
    cost = rec.cost_report["cost_usd"]
    assert cost >= 0
    assert legacy_costs == []
    settled = book.snapshot()
    assert settled["spent_total_usd"] == pytest.approx(cost, abs=0.000001)
    assert settled["reserved_usd"] == 0
    assert settled["unsettled_count"] == 0
    restored = BudgetStore(book_path)
    reloaded_store = TaskStore(str(tmp_path / "tasks"))
    replay_costs(reloaded_store, restored, strict=True)
    assert not restored.settle(result.task_id, cost)
    assert restored.snapshot()["spent_total_usd"] == settled["spent_total_usd"]
    restored.set_limits(global_limit_usd=cost + 0.01)
    blocked = AdmittedDispatcher(delegate, restored, lambda _: {"verdict": "pass"}, reloaded_store)
    with pytest.raises(BudgetAdmissionRejected):
        blocked.dispatch("smoke", "This must be rejected before model execution.")
    assert observed == [0.05]
    print(json.dumps({"model": config.model, "status": rec.status, "output": rec.output,
        "cost_usd": cost, "usage": rec.meta.get("usage"), "reservation_observed": observed,
        "reserved_after": settled["reserved_usd"], "replay_idempotent": True,
        "insufficient_budget_rejected": True}, ensure_ascii=True))
