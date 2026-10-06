"""Plan generation, outbox delivery, queue readiness and budget tests on real SQLite."""
from __future__ import annotations

import json
import sqlite3
import sys
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, contextmanager
from pathlib import Path
from threading import Barrier
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from collaboration import database as database_module
from collaboration.auth import Access
from collaboration.budgets import BudgetService
from collaboration.database import Database, SCHEMA_VERSION
from collaboration.models import (
    Conflict, InvalidState, NotFound, Unavailable, ValidationError,
)
from collaboration.planning import MAX_ITEMS, PlanService, validate_candidate
from collaboration.requirements import RequirementService
from collaboration.scheduler import OutboxDispatcher, QueueService, Scheduler
from collaboration.store import CollaborationStore

PROJECT_A = "项目 A"
PROJECT_B = "项目 B"


class Clock:
    def __init__(self, value=1000.0):
        self.value = value

    def __call__(self):
        return self.value

    def advance(self, seconds):
        self.value += seconds
        return self.value


@pytest.fixture
def team(tmp_path):
    database = Database(tmp_path / "collab.sqlite3")
    database.initialize()
    store = CollaborationStore(database)
    alice = store.create_user("甲")
    bob = store.create_user("乙")
    carol = store.create_user("丙")
    dave = store.create_user("丁")
    erin = store.create_user("戊")
    project_a = store.create_project(PROJECT_A, alice.user_id)
    project_b = store.create_project(PROJECT_B, alice.user_id)
    store.add_member(project_a.project_id, bob.user_id)
    store.add_member(project_a.project_id, carol.user_id, access_role="manager")
    store.add_member(project_a.project_id, dave.user_id)
    agent = store.create_agent(dave.user_id, "丁的 Agent")
    binding = store.create_binding(project_a.project_id, agent.agent_id, dave.user_id,
                                   duty="实现", persona_version="persona-v1",
                                   allowed_tools=("Read", "Edit"),
                                   workspace_ref="workspace-a", provider_ref="provider-a")
    clock = Clock()
    budgets = BudgetService(clock=clock)
    planning = PlanService(database, budgets=budgets, clock=clock)
    queue = QueueService(database, heartbeat_timeout=120, clock=clock)
    scheduler = Scheduler(database, planning=planning, queue=queue, clock=clock)
    return SimpleNamespace(database=database, store=store,
                           requirements=RequirementService(clock=clock), budgets=budgets,
                           planning=planning, queue=queue, scheduler=scheduler, clock=clock,
                           alice=alice, bob=bob, carol=carol, dave=dave, erin=erin,
                           project_a=project_a, project_b=project_b, agent=agent, binding=binding)


@contextmanager
def session(team, user, *, write=True):
    context = team.database.transaction() if write else team.database.read()
    with context as conn:
        store = CollaborationStore(team.database, connection=conn)
        yield Access(store.get_user(user.user_id), False, "", 0.0, store, conn)


def approved_requirement(team, *, title="需求文档", content="# 需求原文",
                         acceptance=("第一条验收", "第二条验收"), actor=None):
    actor = actor or team.bob
    with session(team, actor) as access:
        requirement = team.requirements.create_requirement(
            access, project_id=team.project_a.project_id, title=title, content=content,
            acceptance_items=list(acceptance) if acceptance is not None else None)
    with session(team, actor) as access:
        requirement, _ = team.requirements.request_review(
            access, project_id=team.project_a.project_id,
            requirement_id=requirement.requirement_id,
            revision_id=requirement.current_revision_id,
            reviewer_user_id=team.carol.user_id, expected_version=requirement.version)
    with session(team, team.carol) as access:
        requirement, _ = team.requirements.decide_review(
            access, project_id=team.project_a.project_id,
            requirement_id=requirement.requirement_id,
            revision_id=requirement.review_revision_id, decision="approved",
            expected_version=requirement.version)
    return requirement


def item(team, key, *, acceptance=("AC-001", "AC-002"), estimate=1, depends_on=(),
         assignee=None, binding=None, duty="实现", **overrides):
    record = {
        "key": key, "title": f"{key} 任务", "objective": "完成目标", "scope": "范围内",
        "non_goals": "不做的事", "duty": duty,
        "assignee_user_id": (assignee or team.dave).user_id,
        "binding_id": (binding or team.binding).binding_id,
        "acceptance_ids": list(acceptance), "estimated_units": estimate,
        "depends_on": list(depends_on),
    }
    record.update(overrides)
    return record


def manual_plan(team, requirement, planner, *, actor=None):
    team.planning.planner = planner if (planner is None or callable(planner)) \
        else (lambda context: planner)
    return team.planning.plan(project_id=requirement.project_id,
                              requirement_id=requirement.requirement_id,
                              actor_id=(actor or team.carol).user_id,
                              require_approved_only=False)


def verify(team, requirement, *, revision_id=None):
    return team.planning.plan(project_id=requirement.project_id,
                              requirement_id=requirement.requirement_id, actor_id=None,
                              require_approved_only=True,
                              expected_revision_id=revision_id
                              or requirement.approved_revision_id)


def current(team, requirement):
    return team.store.get_requirement(requirement.project_id, requirement.requirement_id)


def audit_actions(team, project_id):
    with team.database.read() as conn:
        return [row["action"] for row in conn.execute(
            "SELECT action FROM collaboration_audit WHERE project_id=? ORDER BY created_at,rowid",
            (project_id,))]


def scopes(team, requirement):
    return (("global", ""), ("project", requirement.project_id),
            ("requirement", requirement.requirement_id))


def live_instance(team, *, node_name="节点", binding=None):
    binding = binding or team.binding
    node = team.store.create_node(node_name, f"{node_name:0<64}"[:64])
    instance = team.store.create_instance(
        binding.binding_id, node.node_id, claim_token_hash=f"{node_name}-claim".ljust(64, "0"),
        claim_expires_at=team.clock() + 3600, created_by=team.alice.user_id,
        created_at=team.clock())
    return team.store.mark_claimed(instance.instance_id, protocol_version="v1", capacity=1,
                                   isolation_mode="os_account", isolation_attestation=None,
                                   workspace_ref=binding.workspace_ref,
                                   provider_ref=binding.provider_ref, allowed_tools="[]",
                                   env_allowlist="[]",
                                   instance_token_hash=f"{node_name}-token".ljust(64, "0"),
                                   claimed_at=team.clock())


def facts(*, member_status="active", binding_status="active", binding_member="u1",
          agent_status="active", duty="实现"):
    members = {"u1": SimpleNamespace(user_id="u1", status=member_status, access_role="member")}
    bindings = {"b1": SimpleNamespace(binding_id="b1", agent_id="a1", member_user_id=binding_member,
                                      duty="实现", status=binding_status, persona_version="p1")}
    agents = {"a1": SimpleNamespace(agent_id="a1", status=agent_status)}
    acceptance = [SimpleNamespace(item_id="AC-001", text="甲"),
                  SimpleNamespace(item_id="AC-002", text="乙")]
    return members, bindings, agents, acceptance


def candidate_for(key, **overrides):
    base = {"key": key, "title": "标题", "objective": "目标", "scope": "范围", "non_goals": "非目标",
            "duty": "实现", "assignee_user_id": "u1", "binding_id": "b1",
            "acceptance_ids": ["AC-001", "AC-002"], "estimated_units": 1}
    base.update(overrides)
    return base


# --- schema migration ---------------------------------------------------------

def make_v4_database(path):
    with closing(sqlite3.connect(path, isolation_level=None)) as conn:
        conn.execute("BEGIN IMMEDIATE")
        for statement in (database_module._SCHEMA_STATEMENTS + database_module._AUTH_SCHEMA
                          + database_module._INSTANCE_SCHEMA + database_module._REQUIREMENT_SCHEMA):
            conn.execute(statement)
        conn.execute("INSERT INTO users(user_id,display_name) VALUES('old-user','旧用户')")
        conn.execute("INSERT INTO projects(project_id,name,owner_id) "
                     "VALUES('old-project','旧项目','old-user')")
        conn.execute("INSERT INTO project_members(project_id,user_id,access_role) "
                     "VALUES('old-project','old-user','owner')")
        conn.execute("INSERT INTO requirements(requirement_id,project_id,title,requester_id,"
                     "status,version,created_at,updated_at) "
                     "VALUES('old-requirement','old-project','旧需求','old-user','draft',1,1.0,1.0)")
        conn.execute("INSERT INTO requirement_revisions(revision_id,project_id,requirement_id,number,"
                     "content,content_hash,content_size,author_id,created_at) "
                     "VALUES('old-revision','old-project','old-requirement',1,'原文','" + "a" * 64
                     + "',6,'old-user',1.0)")
        conn.execute("UPDATE requirements SET status='approved',approved_revision_id='old-revision',"
                     "approved_revision_hash=?,current_revision_id='old-revision',version=2 "
                     "WHERE requirement_id='old-requirement'", ("a" * 64,))
        conn.execute("PRAGMA user_version=4")
        conn.commit()


def test_v4_database_upgrades_to_v5_preserving_requirements(tmp_path):
    path = tmp_path / "v4.sqlite3"
    make_v4_database(path)
    database = Database(path)
    with pytest.raises(database_module.SchemaVersionError):
        with database.read():
            pytest.fail("Migration must be explicit")
    database.initialize()
    store = CollaborationStore(database)
    requirement = store.get_requirement("old-project", "old-requirement")
    assert (requirement.status, requirement.version) == ("approved", 2)
    assert requirement.approved_revision_id == "old-revision"
    assert store.get_revision_content("old-project", "old-requirement", "old-revision") == "原文"
    assert store.get_user("old-user").display_name == "旧用户"
    with database.read() as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    plan = store.create_plan("old-project", "old-requirement", revision_id="old-revision",
                             revision_hash="a" * 64, status="blocked",
                             report=json.dumps({"problems": ["empty_plan"], "item_count": 0}),
                             total_units=0, created_by=None, created_at=2.0)
    assert plan.status == "blocked" and plan.published_at is None


def test_v4_migration_failure_leaves_the_file_untouched(tmp_path, monkeypatch):
    path = tmp_path / "v4.sqlite3"
    make_v4_database(path)
    before = path.read_bytes()
    with monkeypatch.context() as patch:
        patch.setattr(database_module, "_PLANNING_SCHEMA",
                      database_module._PLANNING_SCHEMA[:1] + ("INVALID SQL",))
        with pytest.raises(sqlite3.OperationalError):
            Database(path).initialize()
    assert path.read_bytes() == before
    Database(path).initialize()
    assert CollaborationStore(Database(path)).get_user("old-user").version == 1


# --- schema invariants --------------------------------------------------------

def test_acceptance_items_are_immutable_and_scoped_to_their_revision(team):
    requirement = approved_requirement(team)
    revision_id = requirement.approved_revision_id
    items = team.store.list_acceptance_items(requirement.project_id, requirement.requirement_id,
                                             revision_id)
    assert [(entry.item_id, entry.position) for entry in items] == [("AC-001", 0), ("AC-002", 1)]
    with pytest.raises(sqlite3.IntegrityError):
        with team.database.transaction() as conn:
            conn.execute("UPDATE revision_acceptance_items SET text='changed' WHERE item_id='AC-001'")
    with pytest.raises(sqlite3.IntegrityError):
        with team.database.transaction() as conn:
            conn.execute("DELETE FROM revision_acceptance_items WHERE item_id='AC-001'")
    with pytest.raises(sqlite3.IntegrityError):
        with team.database.transaction() as conn:
            conn.execute("INSERT INTO revision_acceptance_items(revision_id,requirement_id,item_id,"
                         "text,position) VALUES(?,?,'AC-001','duplicate',2)",
                         (revision_id, requirement.requirement_id))
    with session(team, team.bob) as access:
        updated, _ = team.requirements.save_revision(
            access, project_id=requirement.project_id, requirement_id=requirement.requirement_id,
            content="# 第二版原文", expected_version=requirement.version,
            acceptance_items=["第三版第一条", "第三版第二条"])
    assert updated.current_revision_id != revision_id
    assert [(entry.item_id, entry.text) for entry in team.store.list_acceptance_items(
        updated.project_id, updated.requirement_id, updated.current_revision_id)] == \
        [("AC-001", "第三版第一条"), ("AC-002", "第三版第二条")]
    assert [(entry.item_id, entry.text) for entry in team.store.list_acceptance_items(
        updated.project_id, updated.requirement_id, revision_id)] == \
        [("AC-001", "第一条验收"), ("AC-002", "第二条验收")]


def test_work_item_links_require_the_same_plan_and_one_published_plan(team):
    first = approved_requirement(team)
    plan = manual_plan(team, first, {"items": [item(team, "A"), item(team, "B", depends_on=("A",))]})
    second = approved_requirement(team, title="另一个需求")
    other = manual_plan(team, second, {"items": [item(team, "C")]})
    items = {entry.key: entry for entry in team.store.list_work_items(first.project_id, plan.plan_id)}
    other_item = team.store.list_work_items(second.project_id, other.plan_id)[0]
    assert [entry.status for entry in items.values()] == ["ready", "pending_dependency"]
    with pytest.raises(sqlite3.IntegrityError):
        with team.database.transaction() as conn:
            conn.execute("INSERT INTO work_item_dependencies(work_item_id,depends_on_id,plan_id,"
                         "project_id) VALUES(?,?,?,?)",
                         (items["A"].work_item_id, other_item.work_item_id, plan.plan_id,
                          first.project_id))
    with pytest.raises(Conflict):
        team.store.create_plan(first.project_id, first.requirement_id, revision_id="x" * 32,
                               revision_hash="a" * 64, status="published", report="{}",
                               total_units=0, created_by=None, created_at=1.0)
    with pytest.raises(sqlite3.IntegrityError):
        with team.database.transaction() as conn:
            conn.execute("INSERT INTO plans(plan_id,project_id,requirement_id,revision_id,"
                         "revision_hash,status,report,total_units,created_at) "
                         "VALUES('manual','p','r','v',?,'published','{}',0,1.0)", ("a" * 64,))
    with pytest.raises(ValidationError):
        team.store.create_plan(first.project_id, first.requirement_id, revision_id="x" * 32,
                               revision_hash="a" * 64, status="blocked", report="{}",
                               total_units=0, created_by=None, created_at=1.0,
                               items=[item(team, "D")])


def test_outbox_identity_is_immutable_while_lease_fields_move(team):
    event = team.store.enqueue_event(team.project_a.project_id, "mystery.topic", {"n": 1},
                                     created_at=team.clock())
    with pytest.raises(sqlite3.IntegrityError):
        with team.database.transaction() as conn:
            conn.execute("UPDATE outbox_events SET topic='changed' WHERE event_id=?",
                         (event.event_id,))
    with pytest.raises(sqlite3.IntegrityError):
        with team.database.transaction() as conn:
            conn.execute("UPDATE outbox_events SET payload='{}' WHERE event_id=?",
                         (event.event_id,))
    with team.database.transaction() as conn:
        cursor = conn.execute("UPDATE outbox_events SET attempts=attempts+1,claim_expires_at=42 "
                             "WHERE event_id=?", (event.event_id,))
        assert cursor.rowcount == 1
    stored = team.store.get_event(event.event_id)
    assert (stored.attempts, stored.claim_expires_at) == (1, 42.0)


# --- plan validation ----------------------------------------------------------

def test_validate_candidate_reports_structural_problems():
    members, bindings, agents, acceptance = facts()
    assert validate_candidate(None, members=members, bindings=bindings, agents=agents,
                              acceptance=acceptance) == (["invalid_candidate"], [])
    assert validate_candidate({"items": "nope"}, members=members, bindings=bindings,
                              agents=agents, acceptance=acceptance) == (["invalid_candidate"], [])
    problems, _ = validate_candidate({"items": []}, members=members, bindings=bindings,
                                     agents=agents, acceptance=acceptance)
    assert problems == ["empty_plan", "acceptance_uncovered:AC-001",
                        "acceptance_uncovered:AC-002"]
    problems, _ = validate_candidate(
        {"items": [candidate_for(f"K{index}") for index in range(MAX_ITEMS + 1)]},
        members=members, bindings=bindings, agents=agents, acceptance=acceptance)
    assert problems == ["too_many_items"]
    problems, _ = validate_candidate({"items": [candidate_for("A")]}, members=members,
                                     bindings=bindings, agents=agents, acceptance=[])
    assert problems == ["acceptance_items_missing", "acceptance_unknown:AC-001",
                        "acceptance_unknown:AC-002"]
    problems, _ = validate_candidate({"items": [candidate_for("bad key")]}, members=members,
                                     bindings=bindings, agents=agents, acceptance=acceptance)
    assert problems == ["invalid_key:item_0", "acceptance_uncovered:AC-001",
                        "acceptance_uncovered:AC-002"]
    problems, _ = validate_candidate({"items": [candidate_for("A"), candidate_for("A")]},
                                     members=members, bindings=bindings, agents=agents,
                                     acceptance=acceptance)
    assert problems == ["duplicate_key:A"]
    problems, _ = validate_candidate({"items": [candidate_for("A", title="  ")]}, members=members,
                                     bindings=bindings, agents=agents, acceptance=acceptance)
    assert problems == ["invalid_field:A:title"]
    problems, items = validate_candidate({"items": [candidate_for("A", acceptance_ids=["AC-999"])]},
                                         members=members, bindings=bindings, agents=agents,
                                         acceptance=acceptance)
    assert "acceptance_unknown:AC-999" in problems
    assert "acceptance_uncovered:AC-001" in problems and "acceptance_uncovered:AC-002" in problems
    problems, items = validate_candidate({"items": [candidate_for("A")]}, members=members,
                                         bindings=bindings, agents=agents, acceptance=acceptance)
    assert problems == [] and items[0]["key"] == "A" and items[0]["depends_on"] == []


def test_validate_candidate_reports_identity_and_estimate_problems():
    def problems_for(candidate, **changes):
        members, bindings, agents, acceptance = facts(**changes)
        return validate_candidate(candidate, members=members, bindings=bindings, agents=agents,
                                  acceptance=acceptance)[0]

    assert problems_for({"items": [candidate_for("A", assignee_user_id="u9")]}) == \
        ["assignee_not_member:A", "binding_member_mismatch:A"]
    assert problems_for({"items": [candidate_for("A", binding_id="b9")]}) == ["binding_not_found:A"]
    assert problems_for({"items": [candidate_for("A")]}, binding_status="revoked") == \
        ["binding_not_active:A"]
    assert problems_for({"items": [candidate_for("A")]}, binding_member="u2") == \
        ["binding_member_mismatch:A"]
    assert problems_for({"items": [candidate_for("A", duty="别的职责")]}) == ["duty_mismatch:A"]
    assert problems_for({"items": [candidate_for("A")]}, agent_status="disabled") == \
        ["agent_not_active:A"]
    assert problems_for({"items": [candidate_for("A")]}, member_status="removed") == \
        ["assignee_not_member:A"]
    for estimate in (True, -1, 1.0, "3", None):
        assert problems_for({"items": [candidate_for("A", estimated_units=estimate)]}) == \
            ["invalid_estimate:A"]


def test_validate_candidate_reports_dependency_problems():
    members, bindings, agents, acceptance = facts()
    problems, _ = validate_candidate({"items": [candidate_for("A", depends_on=["A"])]},
                                     members=members, bindings=bindings, agents=agents,
                                     acceptance=acceptance)
    assert problems == ["dependency_self:A", "dependency_cycle"]
    problems, _ = validate_candidate({"items": [candidate_for("B", depends_on=["A"])]},
                                     members=members, bindings=bindings, agents=agents,
                                     acceptance=acceptance)
    assert problems == ["dependency_unknown:B"]
    problems, _ = validate_candidate(
        {"items": [candidate_for("A", depends_on=["B"]), candidate_for("B", depends_on=["A"])]},
        members=members, bindings=bindings, agents=agents, acceptance=acceptance)
    assert problems == ["dependency_cycle"]
    problems, items = validate_candidate(
        {"items": [candidate_for("A"), candidate_for("B", depends_on=["A", "A"])]},
        members=members, bindings=bindings, agents=agents, acceptance=acceptance)
    assert problems == [] and items[1]["depends_on"] == ["A"]


# --- plan publication ---------------------------------------------------------

def test_generation_requires_an_approved_revision_and_a_member_actor(team):
    called = []
    team.planning.planner = lambda context: called.append(context) or {"items": []}
    with session(team, team.bob) as access:
        draft = team.requirements.create_requirement(
            access, project_id=team.project_a.project_id, title="草稿", content="# 草稿")
    with pytest.raises(InvalidState):
        team.planning.plan(project_id=draft.project_id, requirement_id=draft.requirement_id,
                           actor_id=team.carol.user_id, require_approved_only=False)
    assert called == []
    with pytest.raises(NotFound):
        manual_plan(team, draft, {"items": []}, actor=team.erin)
    assert called == []
    assert team.store.list_plans(draft.project_id, draft.requirement_id) == []


def test_unconfigured_or_failing_planners_are_unavailable(team):
    requirement = approved_requirement(team)
    with pytest.raises(Unavailable):
        manual_plan(team, requirement, None)
    def broken(context):
        raise RuntimeError("model backend exploded")
    with pytest.raises(Unavailable):
        manual_plan(team, requirement, broken)
    assert team.store.list_plans(requirement.project_id, requirement.requirement_id) == []
    assert current(team, requirement).status == "approved"


def test_planner_sees_the_frozen_revision_and_runs_outside_write_transactions(team):
    requirement = approved_requirement(team, content="# 已批准原文")
    captured = {}

    def planner(context):
        captured.update(context)
        # Planning must not hold a write lock: this write would time out otherwise.
        team.store.enqueue_event(team.project_a.project_id, "probe.event", {},
                                 created_at=team.clock())
        return {"items": [item(team, "A")]}

    plan = manual_plan(team, requirement, planner)
    assert plan.status == "published"
    assert captured["content"] == "# 已批准原文"
    assert captured["revision_id"] == requirement.approved_revision_id
    assert captured["revision_hash"] == requirement.approved_revision_hash
    assert [entry["item_id"] for entry in captured["acceptance_items"]] == ["AC-001", "AC-002"]
    assert captured["project_id"] == team.project_a.project_id
    assert any(event.topic == "probe.event"
               for event in team.store.list_events(team.project_a.project_id))


def test_candidates_are_revalidated_against_fresh_facts(team):
    requirement = approved_requirement(team)

    def planner(context):
        with team.database.transaction() as conn:
            conn.execute("UPDATE agent_bindings SET status='revoked',version=version+1 "
                         "WHERE binding_id=?", (team.binding.binding_id,))
        return {"items": [item(team, "A")]}

    plan = manual_plan(team, requirement, planner)
    assert plan.status == "blocked"
    assert "binding_not_active:A" in json.loads(plan.report)["problems"]
    assert current(team, requirement).status == "planning_blocked"


def test_publication_reserves_budget_and_starts_execution(team):
    requirement = approved_requirement(team)
    plan = manual_plan(team, requirement, {"items": [
        item(team, "A", estimate=3), item(team, "B", estimate=4, depends_on=("A",))]})
    assert (plan.status, plan.total_units) == ("published", 7)
    assert plan.created_at == plan.published_at == team.clock()
    assert plan.created_by == team.carol.user_id
    assert current(team, requirement).status == "executing"
    items = {entry.key: entry for entry in team.store.list_work_items(requirement.project_id,
                                                                      plan.plan_id)}
    assert items["A"].status == "ready" and items["B"].status == "pending_dependency"
    dependencies = team.store.list_work_item_dependencies(requirement.project_id, plan.plan_id)
    assert [(d.work_item_id, d.depends_on_id) for d in dependencies] == \
        [(items["B"].work_item_id, items["A"].work_item_id)]
    for scope, ref_id in scopes(team, requirement):
        entries = team.store.list_budget_entries(scope, ref_id)
        assert [(entry.ref_type, entry.kind, entry.delta_units) for entry in entries] == \
            [("plan", "reserve", 7)]
    events = {event.topic: event for event in team.store.list_events(requirement.project_id)}
    assert set(events) == {"requirement.approved", "plan.published"}
    assert events["plan.published"].delivered_at is None
    assert json.loads(events["plan.published"].payload) == {
        "plan_id": plan.plan_id, "requirement_id": requirement.requirement_id,
        "revision_id": requirement.approved_revision_id}
    actions = audit_actions(team, requirement.project_id)
    assert actions.count("plan.published") == 1
    assert actions.count("requirement.executing") == 1
    assert actions.count("requirement.review_approved") == 1


def test_blocked_plan_records_problems_and_a_repaired_retry_publishes(team):
    requirement = approved_requirement(team)
    plan = manual_plan(team, requirement,
                       {"items": [item(team, "A", assignee=team.erin)]})
    assert (plan.status, plan.total_units, plan.published_at) == ("blocked", 0, None)
    report = json.loads(plan.report)
    assert set(report["problems"]) >= {"assignee_not_member:A", "binding_member_mismatch:A"}
    assert report["item_count"] == 1
    assert current(team, requirement).status == "planning_blocked"
    assert team.store.list_work_items(requirement.project_id, plan.plan_id) == []
    actions = audit_actions(team, requirement.project_id)
    assert actions.count("plan.blocked") == 1 and actions.count("requirement.planning_blocked") == 1
    team.store.add_member(requirement.project_id, team.erin.user_id)
    erin_agent = team.store.create_agent(team.erin.user_id, "戊的 Agent")
    erin_binding = team.store.create_binding(
        requirement.project_id, erin_agent.agent_id, team.erin.user_id, duty="实现",
        persona_version="persona-v1", allowed_tools=("Read",),
        workspace_ref="workspace-e", provider_ref="provider-e")
    repaired = manual_plan(team, requirement,
                           {"items": [item(team, "A", assignee=team.erin, binding=erin_binding)]})
    assert repaired.status == "published"
    assert current(team, requirement).status == "executing"
    assert [entry.status for entry in
            team.store.list_plans(requirement.project_id, requirement.requirement_id)] == \
        ["blocked", "published"]


def test_second_manual_generation_conflicts_and_replay_is_a_noop(team):
    requirement = approved_requirement(team)
    plan = manual_plan(team, requirement, {"items": [item(team, "A")]})
    with pytest.raises(Conflict):
        manual_plan(team, requirement, {"items": [item(team, "B")]})
    assert verify(team, requirement) is None
    assert verify(team, requirement, revision_id="0" * 32) is None
    assert len(team.store.list_plans(requirement.project_id, requirement.requirement_id)) == 1
    assert audit_actions(team, requirement.project_id).count("plan.published") == 1
    assert len(team.store.list_work_items(requirement.project_id, plan.plan_id)) == 1


def test_digest_drift_in_the_approved_revision_blocks_generation(team):
    requirement = approved_requirement(team)
    with team.database.transaction() as conn:
        conn.execute("UPDATE requirements SET approved_revision_hash=? WHERE requirement_id=?",
                     ("f" * 64, requirement.requirement_id))
    with pytest.raises(Conflict):
        manual_plan(team, requirement, {"items": [item(team, "A")]})
    assert team.store.list_plans(requirement.project_id, requirement.requirement_id) == []
    assert current(team, requirement).status == "approved"


def test_new_draft_revisions_need_a_fresh_approval(team):
    requirement = approved_requirement(team)
    with session(team, team.bob) as access:
        requirement, revision = team.requirements.save_revision(
            access, project_id=requirement.project_id, requirement_id=requirement.requirement_id,
            content="# 新草稿", expected_version=requirement.version)
    assert requirement.status == "draft"
    assert requirement.approved_revision_id is not None
    with pytest.raises(InvalidState):
        manual_plan(team, requirement, {"items": [item(team, "A")]})
    assert verify(team, requirement) is None
    assert team.store.list_plans(requirement.project_id, requirement.requirement_id) == []


def test_missing_acceptance_items_block_a_plan(team):
    requirement = approved_requirement(team, acceptance=None)
    plan = manual_plan(team, requirement, {"items": [item(team, "A", acceptance=())]})
    assert plan.status == "blocked"
    assert json.loads(plan.report)["problems"] == ["acceptance_items_missing"]
    assert current(team, requirement).status == "planning_blocked"


def test_concurrent_generation_publishes_exactly_once(team):
    requirement = approved_requirement(team)
    barrier = Barrier(2)

    def planner(context):
        barrier.wait(timeout=10)
        return {"items": [item(team, "A", estimate=7)]}

    team.planning.planner = planner
    outcomes = []

    def run():
        try:
            outcomes.append((True, team.planning.plan(
                project_id=requirement.project_id, requirement_id=requirement.requirement_id,
                actor_id=team.carol.user_id, require_approved_only=False)))
        except Conflict:
            outcomes.append((False, None))

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(lambda _: run(), range(2)))
    published = [plan for ok, plan in outcomes if ok]
    assert len(published) == 1 and published[0].status == "published"
    assert sum(1 for ok, _ in outcomes if not ok) == 1
    plans = team.store.list_plans(requirement.project_id, requirement.requirement_id)
    assert [plan.status for plan in plans] == ["published"]
    for scope, ref_id in scopes(team, requirement):
        assert len(team.store.list_budget_entries(scope, ref_id)) == 1


# --- queue --------------------------------------------------------------------

def test_queue_advances_items_through_dependencies_and_heartbeats(team):
    requirement = approved_requirement(team)
    plan = manual_plan(team, requirement, {"items": [
        item(team, "A"), item(team, "B", depends_on=("A",))]})
    items = {entry.key: entry for entry in team.store.list_work_items(requirement.project_id,
                                                                      plan.plan_id)}
    result = team.scheduler.tick(project_id=requirement.project_id)
    assert result["work_items_changed"] == 1
    assert team.store.get_work_item(requirement.project_id, items["A"].work_item_id).status == \
        "waiting_agent"
    assert team.store.get_work_item(requirement.project_id, items["B"].work_item_id).status == \
        "pending_dependency"
    with team.database.read() as conn:
        actor = conn.execute("SELECT actor_id FROM collaboration_audit "
                             "WHERE action='work_item.waiting_agent'").fetchone()
    assert actor["actor_id"] is None
    instance = live_instance(team)
    team.scheduler.tick(project_id=requirement.project_id)
    assert team.store.get_work_item(requirement.project_id, items["A"].work_item_id).status == \
        "ready"
    assert team.store.get_work_item(requirement.project_id, items["B"].work_item_id).status == \
        "pending_dependency"
    team.clock.advance(121)
    team.scheduler.tick(project_id=requirement.project_id)
    assert team.store.get_work_item(requirement.project_id, items["A"].work_item_id).status == \
        "waiting_agent"
    live_instance(team, node_name="节点二")
    team.scheduler.tick(project_id=requirement.project_id)
    assert team.store.get_work_item(requirement.project_id, items["A"].work_item_id).status == \
        "ready"
    team.store.set_disabled(instance.instance_id)
    with team.database.transaction() as conn:
        conn.execute("UPDATE work_items SET status='accepted',version=version+1 "
                     "WHERE work_item_id=?", (items["A"].work_item_id,))
    team.scheduler.tick(project_id=requirement.project_id)
    assert team.store.get_work_item(requirement.project_id, items["B"].work_item_id).status == \
        "ready"
    for entry in team.store.list_instances(requirement.project_id, team.binding.binding_id):
        if entry.status == "active":
            team.store.set_disabled(entry.instance_id)
    team.scheduler.tick(project_id=requirement.project_id)
    assert team.store.get_work_item(requirement.project_id, items["B"].work_item_id).status == \
        "waiting_agent"


def test_queue_blocks_a_revoked_binding_and_stays_sticky(team):
    requirement = approved_requirement(team)
    plan = manual_plan(team, requirement, {"items": [item(team, "A")]})
    team.store.revoke_binding(requirement.project_id, team.binding.binding_id, expected_version=1)
    result = team.scheduler.tick(project_id=requirement.project_id)
    assert result["work_items_changed"] == 1
    work_item = team.store.list_work_items(requirement.project_id, plan.plan_id)[0]
    assert work_item.status == "blocked" and work_item.version == 2
    again = team.scheduler.tick(project_id=requirement.project_id)
    assert again["work_items_changed"] == 0
    assert team.store.get_work_item(requirement.project_id, work_item.work_item_id) == work_item
    assert audit_actions(team, requirement.project_id).count("work_item.blocked") == 1


def test_queue_blocks_on_disabled_agents(team):
    requirement = approved_requirement(team)
    manual_plan(team, requirement, {"items": [item(team, "A")]})
    team.store.update_agent(team.agent.agent_id, team.dave.user_id, name="丁的 Agent",
                            status="disabled", expected_version=1)
    team.scheduler.tick(project_id=requirement.project_id)
    plan = team.store.published_plan(requirement.project_id, requirement.requirement_id)
    assert team.store.list_work_items(requirement.project_id, plan.plan_id)[0].status == "blocked"


def test_queue_blocks_on_removed_members(team):
    requirement = approved_requirement(team)
    manual_plan(team, requirement, {"items": [item(team, "A")]})
    member = team.store.get_member(requirement.project_id, team.dave.user_id)
    team.store.update_member(requirement.project_id, team.dave.user_id, access_role="member",
                             status="removed", expected_version=member.version)
    team.scheduler.tick(project_id=requirement.project_id)
    plan = team.store.published_plan(requirement.project_id, requirement.requirement_id)
    assert team.store.list_work_items(requirement.project_id, plan.plan_id)[0].status == "blocked"


def test_queue_and_dispatch_are_project_scoped(team):
    requirement = approved_requirement(team)
    plan = manual_plan(team, requirement, {"items": [item(team, "A")]})
    result = team.scheduler.tick(project_id=team.project_b.project_id)
    assert result == {"claimed": 0, "delivered": 0, "deferred": 0, "voided": 0,
                      "work_items_changed": 0, "attempts_reclaimed": 0}
    assert team.store.list_work_items(requirement.project_id, plan.plan_id)[0].status == "ready"
    assert team.store.list_events(team.project_b.project_id) == []


# --- outbox dispatcher --------------------------------------------------------

def test_dispatch_defers_unknown_topics_with_backoff_and_a_cap(team):
    dispatcher = OutboxDispatcher(team.database, clock=team.clock)
    event = team.store.enqueue_event(team.project_a.project_id, "mystery.topic", {"n": 1},
                                     created_at=team.clock())
    assert dispatcher.dispatch_once() == {"claimed": 1, "delivered": 0, "deferred": 1,
                                         "voided": 0}
    stored = team.store.get_event(event.event_id)
    assert (stored.attempts, stored.last_error) == (1, "_NoHandler")
    assert stored.next_attempt_at == team.clock() + 5.0
    assert dispatcher.dispatch_once() == {"claimed": 0, "delivered": 0, "deferred": 0,
                                         "voided": 0}
    team.clock.advance(5)
    assert dispatcher.dispatch_once()["claimed"] == 1
    stored = team.store.get_event(event.event_id)
    assert (stored.attempts, stored.next_attempt_at) == (2, team.clock() + 10.0)
    with team.database.transaction() as conn:
        conn.execute("UPDATE outbox_events SET attempts=20 WHERE event_id=?", (event.event_id,))
    team.clock.advance(10)
    dispatcher.dispatch_once()
    stored = team.store.get_event(event.event_id)
    assert stored.next_attempt_at == team.clock() + 3600.0
    assert stored.delivered_at is None


def test_a_voided_event_is_closed_but_never_called_delivered(team):
    store = team.store
    event = store.enqueue_event(team.project_a.project_id, "closed.topic", {}, team.clock())
    claimed = store.claim_due_events(now=team.clock(), lease_ttl=300, batch_size=10)
    assert [entry.event_id for entry in claimed] == [event.event_id]
    # 只有手里那次认领可以盖章：过期的认领属于别人，作废它等于替别人撒谎。
    assert store.void_event(event.event_id, claim_expires_at=team.clock() - 1,
                            voided_at=team.clock(), last_error="DeliveryVoided") is False
    assert store.void_event(event.event_id, claim_expires_at=team.clock() + 300,
                            voided_at=team.clock(), last_error="DeliveryVoided") is True
    closed = store.get_event(event.event_id)
    assert (closed.delivered_at, closed.voided_at, closed.attempts, closed.last_error) == \
        (None, team.clock(), 0, "DeliveryVoided")
    # 作废是终态：第二遍只报告没有变化，时间戳不动，认领也再也看不见它。
    assert store.void_event(event.event_id, claim_expires_at=team.clock() + 300,
                            voided_at=team.clock() + 5, last_error="DeliveryVoided") is False
    assert store.get_event(event.event_id).voided_at == team.clock()
    assert store.claim_due_events(now=team.clock() + 100, lease_ttl=300, batch_size=10) == []


def test_dispatch_records_the_failure_type_and_marks_success(team):
    dispatcher = OutboxDispatcher(team.database, clock=team.clock)
    event = team.store.enqueue_event(team.project_a.project_id, "work.topic", {},
                                     created_at=team.clock())

    def failing(event):
        raise ValueError("boom")

    dispatcher.register("work.topic", failing)
    assert dispatcher.dispatch_once()["deferred"] == 1
    stored = team.store.get_event(event.event_id)
    assert (stored.last_error, stored.attempts, stored.claim_expires_at, stored.delivered_at) == \
        ("ValueError", 1, None, None)
    handled = []
    dispatcher.register("work.topic", lambda event: handled.append(event.event_id))
    team.clock.advance(5)
    assert dispatcher.dispatch_once() == {"claimed": 1, "delivered": 1, "deferred": 0,
                                          "voided": 0}
    assert handled == [event.event_id]
    stored = team.store.get_event(event.event_id)
    assert (stored.delivered_at, stored.claim_expires_at) == (team.clock(), None)
    assert dispatcher.dispatch_once()["claimed"] == 0


def test_expired_claim_leases_allow_exactly_one_redelivery(team):
    dispatcher = OutboxDispatcher(team.database, clock=team.clock, lease_ttl=300)
    event = team.store.enqueue_event(team.project_a.project_id, "leased.topic", {},
                                     created_at=team.clock())
    delivered = []
    dispatcher.register("leased.topic", lambda event: delivered.append(event.event_id))
    claimed = team.store.claim_due_events(now=team.clock(), lease_ttl=300, batch_size=10)
    assert [entry.event_id for entry in claimed] == [event.event_id]
    assert team.store.mark_event_delivered(event.event_id, claim_expires_at=team.clock(),
                                           delivered_at=team.clock()) is False
    assert dispatcher.dispatch_once()["claimed"] == 0
    team.clock.advance(301)
    assert dispatcher.dispatch_once() == {"claimed": 1, "delivered": 1, "deferred": 0,
                                          "voided": 0}
    assert delivered == [event.event_id]
    stored = team.store.get_event(event.event_id)
    assert (stored.attempts, stored.delivered_at) == (0, team.clock())


def test_crash_after_the_handler_does_not_duplicate_the_plan(team):
    requirement = approved_requirement(team)
    team.planning.planner = lambda context: {"items": [item(team, "A")]}
    events = team.store.claim_due_events(now=team.clock(), lease_ttl=300, batch_size=10)
    assert [event.topic for event in events] == ["requirement.approved"]
    handler = team.scheduler.dispatcher.handlers["requirement.approved"]
    handler(events[0])
    assert team.store.get_event(events[0].event_id).delivered_at is None
    assert len(team.store.list_plans(requirement.project_id, requirement.requirement_id)) == 1
    team.clock.advance(301)
    result = team.scheduler.tick(project_id=requirement.project_id)
    assert (result["claimed"], result["delivered"], result["deferred"]) == (2, 2, 0)
    plans = team.store.list_plans(requirement.project_id, requirement.requirement_id)
    assert [plan.status for plan in plans] == ["published"]
    assert len(team.store.list_work_items(requirement.project_id, plans[0].plan_id)) == 1
    assert current(team, requirement).status == "executing"


def test_scheduler_tick_plans_after_approval_and_queues_work_items(team):
    requirement = approved_requirement(team)
    team.planning.planner = lambda context: {"items": [item(team, "A", estimate=2)]}
    first = team.scheduler.tick(project_id=requirement.project_id)
    assert first == {"claimed": 1, "delivered": 1, "deferred": 0, "voided": 0,
                     "work_items_changed": 1, "attempts_reclaimed": 0}
    assert current(team, requirement).status == "executing"
    plan = team.store.published_plan(requirement.project_id, requirement.requirement_id)
    assert plan is not None and plan.total_units == 2
    assert team.store.list_work_items(requirement.project_id, plan.plan_id)[0].status == \
        "waiting_agent"
    second = team.scheduler.tick(project_id=requirement.project_id)
    assert (second["claimed"], second["delivered"]) == (1, 1)
    assert second["work_items_changed"] == 0


def test_concurrent_ticks_deliver_each_event_once(team):
    requirement = approved_requirement(team)
    team.planning.planner = lambda context: {"items": [item(team, "A")]}
    dispatcher = team.scheduler.dispatcher
    dispatcher.dispatch_once(project_id=requirement.project_id)  # approval -> one plan
    dispatcher.dispatch_once(project_id=requirement.project_id)  # plan.published
    delivered = []
    dispatcher.register("probe.event", lambda event: delivered.append(event.event_id))
    probe = team.store.enqueue_event(requirement.project_id, "probe.event", {},
                                     created_at=team.clock())
    results = []
    barrier = Barrier(2)

    def run():
        barrier.wait(timeout=10)
        results.append(team.scheduler.tick(project_id=requirement.project_id))

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(lambda _: run(), range(2)))
    assert sum(result["claimed"] for result in results) == 1
    assert sum(result["delivered"] for result in results) == 1
    assert delivered == [probe.event_id]
    assert team.store.get_event(probe.event_id).delivered_at is not None
    assert len(team.store.list_plans(requirement.project_id, requirement.requirement_id)) == 1


# --- budgets ------------------------------------------------------------------

@pytest.mark.parametrize("scope", ["global", "project", "requirement"])
def test_budget_limits_block_then_allow_publication(team, scope):
    requirement = approved_requirement(team)
    ref_id = {"global": "", "project": requirement.project_id,
              "requirement": requirement.requirement_id}[scope]
    with session(team, team.alice) as access:
        team.budgets.set_limit(access, scope=scope, ref_id=ref_id, limit_units=5,
                               expected_version=None,
                               project_id=None if scope == "global" else requirement.project_id)
    plan = manual_plan(team, requirement, {"items": [item(team, "A", estimate=7)]})
    assert plan.status == "blocked"
    assert json.loads(plan.report)["problems"] == [f"budget_exceeded:{scope}"]
    assert current(team, requirement).status == "planning_blocked"
    assert all(team.store.list_budget_entries(scope_name, ref) == []
               for scope_name, ref in scopes(team, requirement))
    with session(team, team.alice) as access:
        view = team.budgets.set_limit(access, scope=scope, ref_id=ref_id, limit_units=10,
                                      expected_version=1,
                                      project_id=None if scope == "global"
                                      else requirement.project_id)
    assert (view["limit_units"], view["version"]) == (10, 2)
    plan = manual_plan(team, requirement, {"items": [item(team, "A", estimate=7)]})
    assert plan.status == "published" and plan.total_units == 7
    assert current(team, requirement).status == "executing"


def test_reservations_are_idempotent_and_usage_stays_visible(team):
    requirement = approved_requirement(team)
    plan = manual_plan(team, requirement, {"items": [item(team, "A", estimate=4)]})
    with session(team, team.alice) as access:
        team.budgets.reserve(access.store, project_id=requirement.project_id,
                             requirement_id=requirement.requirement_id, plan_id=plan.plan_id,
                             amount=4, created_at=team.clock())
    for scope, ref_id in scopes(team, requirement):
        entries = team.store.list_budget_entries(scope, ref_id)
        assert [(entry.ref_key, entry.delta_units) for entry in entries] == [(plan.plan_id, 4)]
    with session(team, team.alice) as access:
        view = team.budgets.set_limit(access, scope="project", ref_id=requirement.project_id,
                                      limit_units=10, expected_version=None,
                                      project_id=requirement.project_id)
    assert (view["used_units"], view["remaining_units"]) == (4, 6)
    with session(team, team.alice) as access:
        listed = team.budgets.budget_view(access, project_id=requirement.project_id)
    assert len(listed["accounts"]) == 1 and listed["accounts"][0] == view


def test_budget_accounts_enforce_expected_versions(team):
    with session(team, team.alice) as access:
        created = team.budgets.set_limit(access, scope="global", ref_id="", limit_units=100,
                                         expected_version=None)
    assert (created["limit_units"], created["version"]) == (100, 1)
    with session(team, team.alice) as access:
        with pytest.raises(Conflict):
            team.budgets.set_limit(access, scope="global", ref_id="", limit_units=5,
                                   expected_version=None)
    with session(team, team.alice) as access:
        with pytest.raises(Conflict):
            team.budgets.set_limit(access, scope="global", ref_id="", limit_units=5,
                                   expected_version=7)
    with session(team, team.alice) as access:
        updated = team.budgets.set_limit(access, scope="global", ref_id="", limit_units=200,
                                         expected_version=1)
    assert (updated["limit_units"], updated["version"]) == (200, 2)
    with session(team, team.alice) as access:
        with pytest.raises(ValidationError):
            team.budgets.set_limit(access, scope="team", ref_id="x", limit_units=1,
                                   expected_version=None)
    with session(team, team.alice) as access:
        with pytest.raises(ValidationError):
            team.budgets.set_limit(access, scope="global", ref_id="", limit_units=-1,
                                   expected_version=2)


def test_executing_requirements_are_visible_to_members_only(team):
    requirement = approved_requirement(team)
    manual_plan(team, requirement, {"items": [item(team, "A")]})
    with session(team, team.carol, write=False) as access:
        listed = team.requirements.list_requirements(
            access, project_id=requirement.project_id)
        assert [entry.requirement_id for entry in listed] == [requirement.requirement_id]
        plans = team.planning.list_plans(access, project_id=requirement.project_id,
                                         requirement_id=requirement.requirement_id)
        assert [plan.status for plan in plans] == ["published"]
    with session(team, team.erin, write=False) as access:
        with pytest.raises(NotFound):
            team.requirements.list_requirements(access, project_id=requirement.project_id)
        with pytest.raises(NotFound):
            team.requirements.get_requirement(
                access, project_id=requirement.project_id,
                requirement_id=requirement.requirement_id)
        with pytest.raises(NotFound):
            team.planning.list_plans(access, project_id=requirement.project_id,
                                     requirement_id=requirement.requirement_id)
        with pytest.raises(NotFound):
            team.planning.plan_detail(access, project_id=requirement.project_id,
                                      requirement_id=requirement.requirement_id,
                                      plan_id=team.store.published_plan(
                                          requirement.project_id,
                                          requirement.requirement_id).plan_id)
        with pytest.raises(NotFound):
            team.store.get_requirement(team.project_b.project_id, requirement.requirement_id)
    team.store.add_member(team.project_b.project_id, team.dave.user_id)
    with session(team, team.dave, write=False) as access:
        with pytest.raises(NotFound):
            team.requirements.get_requirement(
                access, project_id=team.project_b.project_id,
                requirement_id=requirement.requirement_id)
