"""Opt-in paid Ark smoke; no images and no automatic retry of unknown calls."""
# ruff: noqa: F811
import json
import os
import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[5] / "worker"))

from collaboration.sdk_planning import SdkProposalService
from collaboration_contracts import RunRequest
from collaboration_runner import SdkRunner

from agent_core.team_sdk import TeamConfig

from .test_collaboration_planning import (  # noqa: F401
    approved_requirement,
    session,
    team,
)


@pytest.mark.sdk_smoke
@pytest.mark.skipif(os.environ.get("TEAM_SDK_SMOKE") != "1", reason="Paid team SDK smoke is opt-in")
def test_ark_real_candidate_confirmation_and_file_delivery(team, tmp_path):
    credential = "TEAM_ARK_AUTH_TOKEN" if os.environ.get("TEAM_ARK_AUTH_TOKEN") else "ANTHROPIC_AUTH_TOKEN"
    if not os.environ.get(credential):
        pytest.skip("Explicit Ark credential unavailable")
    cfg_path = tmp_path / "sdk.json"
    cfg_path.write_text(json.dumps({"version": 1, "providers": {"provider-a": {
        "model": "ark-code-latest", "base_url": "https://ark.cn-beijing.volces.com/api/plan",
        "auth_token_env": credential}}, "workspaces": {"workspace-a": {"root": str(tmp_path / "project")}},
        "personas": {"persona-v1": {"system_prompt": "按冻结职责完成小型纯文本交付。不要声称运行了测试。",
                                   "allowed_tools": ["Read", "Write"]}},
        "planner": {"provider_ref": "provider-a", "max_cost_usd": 0.05, "max_turns": 1, "timeout_seconds": 120},
        "execution": {"max_cost_usd": 0.05, "max_turns": 6, "timeout_seconds": 120}}), encoding="utf-8")
    config = TeamConfig(cfg_path)
    requirement = approved_requirement(team,
        title="纯文本最小交付",
        content="只需一个工作项：让现有唯一的实现职责 Agent 在 outputs/hello.txt 写入 hello team。"
                "不需要其他职责或接口，不运行 shell、不访问外网、不使用图片。"
                "两条验收均映射到这一个工作项，预计执行预算填写 50000 单位。",
        acceptance=("交付 hello.txt 文件", "hello.txt 的内容是 hello team"))
    proposal_service = SdkProposalService(team.database, team.budgets, config, clock=team.clock)
    team.planning.proposals = proposal_service
    proposal = team.planning.plan(project_id=requirement.project_id, requirement_id=requirement.requirement_id,
        actor_id=team.carol.user_id, require_approved_only=False)
    # Local receipt is retained even when a smoke assertion fails; no secrets are in it.
    receipt = tmp_path / "team-sdk-smoke.json"
    receipt.write_text(json.dumps({"proposal": proposal}, ensure_ascii=False, indent=2), encoding="utf-8")
    assert proposal["status"] == "pending", proposal["report"]
    with session(team, team.carol) as access:
        published = proposal_service.decide(access, project_id=requirement.project_id,
            requirement_id=requirement.requirement_id, proposal_id=proposal["proposal_id"],
            expected_version=proposal["version"], decision="approve", reason="开发联调数据确认，非用户业务验收")
    items = team.store.list_work_items(requirement.project_id, published["plan_id"])
    assert len(items) == 1
    # The fixture binding permits Edit; explicitly narrow this synthetic attempt to Read/Write
    # for the smoke rather than widening a live project's binding or isolation claims.
    work = items[0]
    package = {"project_id": requirement.project_id,
        "subtask": {"key": work.key, "objective": work.objective, "scope": work.scope,
                    "acceptance": ["输出 outputs/hello.txt，内容 hello team"]},
        "dependencies": [], "policy": {"binding_id": team.binding.binding_id, "binding_version": 1,
            "persona_version": "persona-v1", "provider_ref": "provider-a", "workspace_ref": "workspace-a",
            "allowed_tools": ["Read", "Write"], "budget_units": 50000}}
    request = RunRequest("a" * 32, work.work_item_id, "b" * 64, requirement.approved_revision_hash,
        package, "在 outputs/hello.txt 写入 hello team；只交付这个文件，不使用图片，不运行命令。",
        tmp_path / "protocol-work", ("Read", "Write"), threading.Event())
    result = SdkRunner(config)(request)
    receipt.write_text(json.dumps({"proposal": proposal, "execution": {"status": result.status,
        "summary": result.summary, "usage_units": result.usage_units,
        "artifacts": [p.name for p in result.produced]}}, ensure_ascii=False, indent=2), encoding="utf-8")
    assert result.status == "submitted", result.summary
    files = json.loads(next(p.content for p in result.produced if p.name == "team-files.json"))
    artifact = next(p for p in files if p["path"] == "hello.txt")
    content = next(p.content for p in result.produced if p.name == artifact["name"])
    assert content.decode("utf-8").strip() == "hello team"
