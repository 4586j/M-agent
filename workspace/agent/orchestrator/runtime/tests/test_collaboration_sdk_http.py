"""Candidate confirmation uses the real HTTP auth/CSRF/project boundaries."""
# ruff: noqa: F811
import json
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from collaboration.sdk_planning import SdkProposalService

from agent_core.kernel import KernelResult
from agent_core.team_sdk import TeamConfig

from .test_collaboration_app import (  # noqa: F401
    api,
    approval_setup,
    checked,
    plan_candidate,
    plans_path,
    registered_database,
)


def test_real_planner_candidate_cannot_bypass_auth_or_plan_confirmation(api, tmp_path):
    project, member, binding, rid = approval_setup(api)
    path = plans_path(project, rid)
    config = tmp_path / "sdk.json"
    config.write_text(json.dumps({"version": 1, "providers": {"p": {
        "model": "model", "base_url": "https://example.com/api", "auth_token_env": "TEAM_TEST_TOKEN"}},
        "planner": {"provider_ref": "p"}}))
    service = api.client.app.state.planning
    service.proposals = SdkProposalService(api.database, service.budgets, TeamConfig(config),
        sdk=SimpleNamespace(invoke=lambda **kwargs: KernelResult(output=json.dumps({"items": [
            plan_candidate(binding, member["user_id"], "A", ["AC-001"], estimate=10000)]}), cost_usd=0.001)))
    assert checked(api.send("member", "GET", "/capabilities"))["real_planner"]
    generated = checked(api.send("manager", "POST", path), 201)
    proposal = generated["proposal"]
    assert generated["plan"] is None and generated["work_items"] == []
    proposal_path = path.removesuffix("/plans") + "/planning-proposals"
    assert checked(api.send("member", "GET", proposal_path))[0]["proposal_id"] == proposal["proposal_id"]
    checked(api.send("outsider", "GET", proposal_path), 404)
    decision_path = proposal_path + f"/{proposal['proposal_id']}/decision"
    body = {"decision": "approve", "expected_version": proposal["version"]}
    checked(api.send("member", "POST", decision_path, json=body), 403)
    checked(api.send("outsider", "POST", decision_path, json=body), 404)
    checked(api.send("manager", "POST", decision_path, json=body, headers={"X-CSRF-Token": None}), 403)
    published = checked(api.send("manager", "POST", decision_path, json=body))
    assert published["status"] == "published"
    checked(api.send("manager", "POST", decision_path, json=body), 409)
    plans = checked(api.send("member", "GET", path))
    assert len(plans) == 1 and plans[0]["plan_id"] == published["plan_id"]


def test_disabled_planner_has_no_candidate_and_cli_accepts_explicit_config(api):
    assert not checked(api.send("member", "GET", "/capabilities"))["real_planner"]
