"""Real runner adapter and end-to-end team protocol, SDK calls replaced deterministically."""
# ruff: noqa: F811
import hashlib
import json
import sys
import threading
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "worker"))
sys.path.insert(0, str(ROOT / "workspace/agent/orchestrator/runtime"))

from collaboration_client import Fetched
from collaboration_contracts import RunRequest
from collaboration_runner import SdkRunner

from agent_core.kernel import KernelResult
from agent_core.team_sdk import TeamConfig
from worker.tests.test_collaboration_worker import (  # noqa: F401
    approved_requirement,
    env,
    human,
    register_instance,
)


def config(tmp_path, monkeypatch):
    monkeypatch.setenv("TEAM_TEST_TOKEN", "test-selected-provider")
    data = {"version": 1, "providers": {"provider-a": {"model": "model", "base_url": "https://example.com/api",
            "auth_token_env": "TEAM_TEST_TOKEN"}}, "workspaces": {"workspace-a": {"root": str(tmp_path / "projects")}},
            "personas": {"persona-v1": {"system_prompt": "实施这项职责", "allowed_tools": ["Read", "Write", "Edit"]}},
            "planner": {"provider_ref": "provider-a", "max_cost_usd": 0.01}, "execution": {"max_cost_usd": 0.02}}
    path = tmp_path / "sdk.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return TeamConfig(path)


def request(tmp_path, **overrides):
    package = {"project_id": "a" * 32, "subtask": {"key": "implement", "duty": "实现"},
               "dependencies": [], "policy": {"binding_id": "b" * 32, "binding_version": 1,
                   "provider_ref": "provider-a", "workspace_ref": "workspace-a", "persona_version": "persona-v1",
                   "allowed_tools": ["Read", "Write"], "budget_units": 10000}}
    value = RunRequest("c" * 32, "d" * 32, "e" * 64, "f" * 64, package, "frozen requirement",
                       tmp_path / "journal-work", ("Read", "Write"), threading.Event())
    return replace(value, **overrides)


def test_sdk_runner_delivers_bounded_files_and_usage_evidence(tmp_path, monkeypatch):
    cfg = config(tmp_path, monkeypatch)
    seen = []
    def invoke(**kwargs):
        seen.append(kwargs)
        (kwargs["cwd"] / "outputs" / "hello.py").write_text('print("hello")', encoding="utf-8")
        return KernelResult(output="已实现；未运行 Shell 测试", cost_usd=0.001, usage={"input_tokens": 5})
    runner = SdkRunner(cfg, sdk=SimpleNamespace(invoke=invoke))
    outcome = runner(request(tmp_path))
    assert outcome.status == "submitted" and outcome.usage_units == 1000
    assert seen[0]["max_cost_usd"] == 0.01 and seen[0]["tools"] == ["Read", "Write"]
    manifest = json.loads(next(p.content for p in outcome.produced if p.name == "team-files.json"))
    assert manifest[0]["path"] == "hello.py"
    usage = json.loads(next(p.content for p in outcome.produced if p.name == "team-usage.json"))
    assert usage["simulated"] is False and usage["cost_basis"] == "sdk_estimate"
    assert "TOKEN" not in json.dumps(usage)
    with pytest.raises(ValueError, match="new"):
        runner(request(tmp_path))  # Cannot replay side effects in a previously used directory.


def test_unknown_usage_keeps_full_reservation_and_does_not_offer_acceptance(tmp_path, monkeypatch):
    runner = SdkRunner(config(tmp_path, monkeypatch), sdk=SimpleNamespace(
        invoke=lambda **kwargs: KernelResult(output="response without accounting", cost_usd=None)))
    outcome = runner(request(tmp_path))
    assert outcome.status == "failed" and outcome.usage_units == 10000
    assert "费用未知" in outcome.summary
    usage = json.loads(outcome.produced[0].content)
    assert usage["cost_usd"] is None and usage["cost_basis"] == "unknown"


def test_runner_downloads_and_checks_dependency_artifact_bytes(tmp_path, monkeypatch):
    cfg = config(tmp_path, monkeypatch)
    req = request(tmp_path)
    data = b"upstream accepted source"
    req.package["dependencies"] = [{"key": "analysis", "artifacts": [{"artifact_id": "a" * 32,
        "name": "../not-a-path", "media_type": "text/plain", "size_bytes": len(data),
        "sha256": hashlib.sha256(data).hexdigest()}]}]
    def invoke(**kwargs):
        assert (kwargs["cwd"] / "inputs/dependency-001").read_bytes() == data
        assert "../not-a-path" in kwargs["prompt"]
        return KernelResult(output="done", cost_usd=0.001)
    runner = SdkRunner(cfg, sdk=SimpleNamespace(invoke=invoke),
                       artifact_loader=lambda aid, fid: Fetched(data, hashlib.sha256(data).hexdigest()))
    assert runner(req).status == "submitted"


@pytest.mark.parametrize("change", ["provider", "workspace", "persona", "shell", "zero_budget", "claimed_version"])
def test_policy_mismatches_fail_before_sdk(tmp_path, monkeypatch, change):
    cfg = config(tmp_path, monkeypatch)
    req = request(tmp_path)
    instance = {**req.package["policy"]}
    changes = {"provider": ("provider_ref", "missing"), "workspace": ("workspace_ref", "missing"),
               "persona": ("persona_version", "missing"), "shell": ("allowed_tools", ["Bash"]),
               "zero_budget": ("budget_units", 0), "claimed_version": ("binding_version", 2)}
    name, value = changes[change]
    req.package["policy"][name] = value
    def forbidden(**kwargs):
        pytest.fail("SDK must not be invoked")
    runner = SdkRunner(cfg, sdk=SimpleNamespace(invoke=forbidden), instance_policy=instance)
    with pytest.raises(ValueError):
        runner(req)


def test_three_duties_require_human_confirmation_acceptance_and_integration(env, tmp_path, monkeypatch):
    from collaboration.scheduler import QueueService
    from collaboration.sdk_planning import SdkProposalService
    from collaboration_app import FINISHED, CollaborationWorker, WorkerSettings
    from collaboration_journal import ExecutionJournal
    requirement = approved_requirement(env)
    cfg = config(tmp_path, monkeypatch)
    bindings = [env.binding]
    for duty in ("开发", "技术评审"):
        agent = env.store.create_agent(env.worker.user_id, duty + " Agent")
        bindings.append(env.store.create_binding(env.project.project_id, agent.agent_id,
            env.worker.user_id, duty=duty, persona_version="persona-v1", allowed_tools=("Read", "Edit"),
            workspace_ref="workspace-a", provider_ref="provider-a"))
    items = [{"key": f"role-{i}", "title": b.duty, "objective": "交付职责产物", "scope": "需求内",
              "non_goals": "不代替人工验收", "duty": b.duty, "assignee_user_id": b.member_user_id,
              "binding_id": b.binding_id, "acceptance_ids": ["AC-001", "AC-002"],
              "estimated_units": 10000, "depends_on": [f"role-{i-1}"] if i else []}
             for i, b in enumerate(bindings)]
    proposals = SdkProposalService(env.database, env.app.state.budgets, cfg,
        sdk=SimpleNamespace(invoke=lambda **kwargs: KernelResult(output=json.dumps({"items": items}), cost_usd=0.001)))
    env.app.state.planning.proposals = proposals
    proposal = env.app.state.planning.plan(project_id=env.project.project_id,
        requirement_id=requirement.requirement_id, actor_id=env.reviewer.user_id, require_approved_only=False)
    assert proposal["status"] == "pending"
    assert env.store.published_plan(env.project.project_id, requirement.requirement_id) is None
    with human(env, env.reviewer) as access:
        published = proposals.decide(access, project_id=env.project.project_id,
            requirement_id=requirement.requirement_id, proposal_id=proposal["proposal_id"],
            expected_version=proposal["version"], decision="approve")
    queued = env.store.list_work_items(env.project.project_id, published["plan_id"])
    for i, binding in enumerate(bindings):
        instance, client, _ = register_instance(env, project_id=env.project.project_id,
                                               binding=binding, node_name=f"sdk-{i}")
        settings = WorkerSettings(base_url=client.base_url, state_dir=tmp_path / f"worker-{i}", runner="sdk")
        def invoke(index=i, assigned=binding, **kwargs):
            assert kwargs["tools"] == ["Edit", "Read"]
            if index:
                assert any((kwargs["cwd"] / "inputs").iterdir())
            return KernelResult(output=f"职责 {assigned.duty} 的实际交接说明", cost_usd=0.001)
        runner = SdkRunner(cfg, sdk=SimpleNamespace(invoke=invoke), artifact_loader=client.artifact,
                           instance_policy=client.describe())
        worker = CollaborationWorker(client, ExecutionJournal(settings.journal_path), settings=settings, runner=runner)
        try:
            work = next(w for w in queued if w.binding_id == binding.binding_id)
            QueueService(env.database).advance(project_id=env.project.project_id)
            attempt = env.dispatch.dispatch(env.project.project_id, work.work_item_id, requires_real_data=False)
            worker.journal.discover(attempt_id=attempt.attempt_id, work_item_id=attempt.work_item_id,
                                   input_hash=attempt.input_hash, control_epoch=attempt.control_epoch)
            assert worker.run(attempt.attempt_id) == FINISHED
            submitted = env.store.get_attempt(env.project.project_id, attempt.attempt_id)
            assert submitted.status == "submitted", submitted.result_json
            with human(env, env.worker) as access:
                env.reviews.confirm(access, project_id=env.project.project_id, attempt_id=attempt.attempt_id,
                    decision="confirmed", reason="核对该职责的交付物", expected_version=submitted.version)
            work = env.store.get_work_item(env.project.project_id, work.work_item_id)
            with human(env, env.reviewer) as access:
                env.reviews.accept(access, project_id=env.project.project_id, work_item_id=work.work_item_id,
                    decision="accepted", reason="核对验收映射", expected_version=work.version)
        finally:
            worker.close()
    with human(env, env.reviewer) as access:
        updated = env.reviews.advance_requirement(access, project_id=env.project.project_id,
            requirement_id=requirement.requirement_id,
            expected_version=access.store.get_requirement(env.project.project_id, requirement.requirement_id).version)
    assert updated.status == "integrating"
    # A technical Agent report never becomes the requester's signature.
    with human(env, env.requester) as access:
        from collaboration.models import InvalidState
        with pytest.raises(InvalidState):
            env.reviews.accept_requirement(access, project_id=env.project.project_id,
                requirement_id=requirement.requirement_id, decision="accepted", expected_version=updated.version)
    with human(env, env.reviewer) as access:
        updated = env.reviews.record_integration(access, project_id=env.project.project_id,
            requirement_id=requirement.requirement_id, decision="passed", expected_version=updated.version,
            reason="核对三项职责交付的覆盖关系；本用例只验协议，不把模拟 SDK 当实际代码测试")
    assert updated.status == "awaiting_acceptance"
    with human(env, env.requester) as access:
        finished = env.reviews.accept_requirement(access, project_id=env.project.project_id,
            requirement_id=requirement.requirement_id, decision="accepted", expected_version=updated.version,
            reason="明确人工签收这一版交付")
    assert finished.status == "completed"
