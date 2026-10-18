"""Real adapter contract with a stubbed SDK boundary, real SQLite and authorization."""
# ruff: noqa: F811
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from collaboration.auth import Forbidden
from collaboration.models import Conflict, InvalidState
from collaboration.sdk_planning import SdkProposalService

from .test_collaboration_planning import (  # noqa: F401
    approved_requirement,
    current,
    item,
    manual_plan,
    session,
    team,
)


def attach(team, tmp_path, callback=None):
    from agent_core.kernel import KernelResult
    from agent_core.team_sdk import TeamConfig
    path = tmp_path / "sdk.json"
    path.write_text(json.dumps({"version": 1, "providers": {"p": {
        "model": "model", "base_url": "https://example.com/api", "auth_token_env": "TEAM_TEST_TOKEN"}},
        "planner": {"provider_ref": "p", "max_cost_usd": 0.01}, "execution": {"max_cost_usd": 0.02}}))
    calls = []
    def invoke(**kwargs):
        calls.append(kwargs)
        if callback:
            return callback(kwargs)
        return KernelResult(output=json.dumps({"analysis": "按职责交付", "items": [item(team, "A", estimate=10000)]}), cost_usd=0.001)
    proposals = SdkProposalService(team.database, team.budgets, TeamConfig(path),
                                   sdk=SimpleNamespace(invoke=invoke), clock=team.clock)
    team.planning.proposals = proposals
    return proposals, calls


def generate(team, requirement):
    return manual_plan(team, requirement, None)


def decide(team, requirement, proposal, *, actor=None, **kwargs):
    with session(team, actor or team.carol) as access:
        return team.planning.proposals.decide(access, project_id=requirement.project_id,
            requirement_id=requirement.requirement_id, proposal_id=proposal["proposal_id"],
            expected_version=proposal["version"], **kwargs)


def test_paid_planner_is_idempotent_and_waits_for_human_before_dispatch(team, tmp_path):
    requirement = approved_requirement(team)
    proposals, calls = attach(team, tmp_path)
    proposal = generate(team, requirement)
    assert proposal["status"] == "pending" and proposal["cost_units"] == 1000
    assert current(team, requirement).status == "approved"
    assert team.store.published_plan(requirement.project_id, requirement.requirement_id) is None
    assert generate(team, requirement)["proposal_id"] == proposal["proposal_id"]
    assert len(calls) == 1 and calls[0]["tools"] == []
    assert team.budgets._used(team.store, "project", requirement.project_id) == 1000
    published = decide(team, requirement, proposal, decision="approve")
    assert published["status"] == "published"
    plan = team.store.published_plan(requirement.project_id, requirement.requirement_id)
    assert plan.plan_id == published["plan_id"]
    assert current(team, requirement).status == "executing"
    assert team.budgets._used(team.store, "project", requirement.project_id) == 11000
    with pytest.raises(Conflict):
        decide(team, requirement, proposal, decision="approve")
    assert len(calls) == 1


def test_member_cannot_approve_and_changed_binding_invalidates_candidate(team, tmp_path):
    requirement = approved_requirement(team)
    attach(team, tmp_path)
    proposal = generate(team, requirement)
    with pytest.raises(Forbidden):
        decide(team, requirement, proposal, actor=team.bob, decision="approve")
    with team.database.transaction() as conn:
        conn.execute("UPDATE agent_bindings SET version=version+1 WHERE binding_id=?", (team.binding.binding_id,))
    with pytest.raises(Conflict):
        decide(team, requirement, proposal, decision="approve")
    assert team.store.published_plan(requirement.project_id, requirement.requirement_id) is None


@pytest.mark.parametrize("mode,expected", [("invalid", "blocked"), ("clarify", "clarification"),
                                           ("unknown", "unknown"), ("malformed", "blocked")])
def test_unusable_or_incomplete_model_result_never_publishes(team, tmp_path, mode, expected):
    from agent_core.kernel import KernelResult
    requirement = approved_requirement(team)
    payload = {"invalid": {"items": [item(team, "A", binding_id="invented")]},
               "clarify": {"clarification_questions": ["缺少哪个接口的验收定义？"]},
               "unknown": {"items": [item(team, "A", estimate=10000)]}}.get(mode)
    attach(team, tmp_path, lambda kwargs: KernelResult(
        output=json.dumps(payload) if mode != "malformed" else "not json",
        cost_usd=None if mode == "unknown" else 0.001))
    proposal = generate(team, requirement)
    assert proposal["status"] == expected
    with pytest.raises(InvalidState):
        decide(team, requirement, proposal, decision="approve")
    assert team.store.published_plan(requirement.project_id, requirement.requirement_id) is None


def test_unknown_call_is_held_until_explicit_cost_reconciliation(team, tmp_path):
    def broken(kwargs):
        raise RuntimeError("Do not log provider secrets")
    requirement = approved_requirement(team)
    proposals, calls = attach(team, tmp_path, broken)
    proposal = generate(team, requirement)
    assert proposal["status"] == "unknown" and proposal["cost_units"] is None
    assert team.budgets._used(team.store, "project", requirement.project_id) == 10000
    assert generate(team, requirement)["proposal_id"] == proposal["proposal_id"]
    assert len(calls) == 1
    with pytest.raises(InvalidState):
        decide(team, requirement, proposal, decision="reject", reason="retry")
    reconciled = decide(team, requirement, proposal, decision="reconcile", usage_units=2500,
                       reason="核实进程已停止，并核对调用估算记录")
    assert reconciled["status"] == "rejected"
    assert team.budgets._used(team.store, "project", requirement.project_id) == 2500
    generate(team, requirement)
    assert len(calls) == 2


def test_budget_rechecked_at_confirmation(team, tmp_path):
    requirement = approved_requirement(team)
    attach(team, tmp_path)
    proposal = generate(team, requirement)
    with session(team, team.carol) as access:
        team.budgets.set_limit(access, scope="project", ref_id=requirement.project_id,
            limit_units=1000, expected_version=None, project_id=requirement.project_id)
    with pytest.raises(Conflict):
        decide(team, requirement, proposal, decision="approve")
    assert team.store.published_plan(requirement.project_id, requirement.requirement_id) is None


def test_v14_upgrade_preserves_requirements_and_identity_snapshots_are_immutable(team, tmp_path):
    import sqlite3
    requirement = approved_requirement(team)
    with team.database.transaction() as conn:
        conn.execute("DROP TABLE planning_proposals")
        conn.execute("PRAGMA user_version=14")
    team.database.initialize()
    assert current(team, requirement).approved_revision_id == requirement.approved_revision_id
    attach(team, tmp_path)
    proposal = generate(team, requirement)
    with pytest.raises(sqlite3.IntegrityError):
        with team.database.transaction() as conn:
            conn.execute("UPDATE planning_proposals SET context_json='{}' WHERE proposal_id=?", (proposal["proposal_id"],))


def test_non_finite_model_json_is_blocked_and_cost_still_settled(team, tmp_path):
    from agent_core.kernel import KernelResult
    requirement = approved_requirement(team)
    attach(team, tmp_path, lambda kwargs: KernelResult(output='{"analysis":NaN,"items":[]}', cost_usd=0.001))
    proposal = generate(team, requirement)
    assert proposal["status"] == "blocked" and proposal["cost_units"] == 1000
    assert proposal["candidate"] is None


def test_clarification_revision_preserves_explicit_acceptance_and_blocks_old_candidate(team, tmp_path):
    requirement = approved_requirement(team)
    attach(team, tmp_path)
    proposal = generate(team, requirement)
    with session(team, team.bob) as access:
        revised, revision = team.requirements.save_revision(access, project_id=requirement.project_id,
            requirement_id=requirement.requirement_id, content="# 根据澄清补充原文",
            expected_version=requirement.version, acceptance_items=["保留并明确第一条", "保留第二条"])
    assert revised.status == "draft"
    assert len(team.store.list_acceptance_items(requirement.project_id, requirement.requirement_id, revision.revision_id)) == 2
    with pytest.raises(Conflict):
        decide(team, requirement, proposal, decision="approve")
