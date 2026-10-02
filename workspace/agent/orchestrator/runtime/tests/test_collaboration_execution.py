"""Execution attempts, task packages, artifacts, reviews and budget settlement on real SQLite."""
from __future__ import annotations

import base64
import hashlib
import json
import secrets
import sqlite3
import sys
from contextlib import closing, contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from collaboration import database as database_module
from collaboration.auth import Access, Unauthorized
from collaboration.budgets import BudgetService
from collaboration.database import Database, SCHEMA_VERSION
from collaboration.dispatch import DispatchService, TOPIC_ATTEMPT_ASSIGNED
from collaboration.execution import ExecutionService, decode_artifact
from collaboration.instances import AdmissionDenied, InstanceService
from collaboration.scheduler import OutboxDispatcher, QueueService, Scheduler
from collaboration.models import (
    Conflict, InvalidState, NotFound, SchemaVersionError, ValidationError,
)
from collaboration.requirements import RequirementService
from collaboration.store import CollaborationStore


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
    clock = Clock()
    owner = store.create_user("甲")
    requester = store.create_user("乙")
    reviewer = store.create_user("丙")
    worker = store.create_user("丁")
    project_a = store.create_project("项目 A", owner.user_id)
    project_b = store.create_project("项目 B", owner.user_id)
    store.add_member(project_a.project_id, requester.user_id)
    store.add_member(project_a.project_id, reviewer.user_id, access_role="manager")
    store.add_member(project_a.project_id, worker.user_id)
    store.add_member(project_b.project_id, worker.user_id)
    agent = store.create_agent(worker.user_id, "丁的 Agent")
    binding = store.create_binding(project_a.project_id, agent.agent_id, worker.user_id,
                                   duty="实现", persona_version="persona-v1",
                                   allowed_tools=("Read", "Edit"),
                                   workspace_ref="workspace-a", provider_ref="provider-a")
    other_binding = store.create_binding(project_b.project_id, agent.agent_id, worker.user_id,
                                         duty="实现", persona_version="persona-v1",
                                         allowed_tools=("Read",),
                                         workspace_ref="workspace-b", provider_ref="provider-b")
    instances = InstanceService(database, clock=clock)
    return SimpleNamespace(
        database=database, store=store, clock=clock, budgets=BudgetService(clock=clock),
        requirements=RequirementService(clock=clock), instances=instances,
        make_dispatch=lambda **kwargs: DispatchService(database, instances=instances,
                                                       clock=clock, **kwargs),
        make_execution=lambda **kwargs: ExecutionService(database, instances=instances,
                                                         clock=clock, **kwargs),
        owner=owner, requester=requester, reviewer=reviewer, worker=worker,
        project_a=project_a, project_b=project_b, agent=agent, binding=binding,
        other_binding=other_binding)


@contextmanager
def session(team, user):
    with team.database.transaction() as conn:
        store = CollaborationStore(team.database, connection=conn)
        yield Access(store.get_user(user.user_id), False, "", 0.0, store, conn)


def approved_requirement(team):
    with session(team, team.requester) as access:
        requirement = team.requirements.create_requirement(
            access, project_id=team.project_a.project_id, title="协作需求",
            content="# 需求原文", acceptance_items=["第一条验收", "第二条验收"])
    with session(team, team.requester) as access:
        requirement, _ = team.requirements.request_review(
            access, project_id=team.project_a.project_id,
            requirement_id=requirement.requirement_id,
            revision_id=requirement.current_revision_id,
            reviewer_user_id=team.reviewer.user_id, expected_version=requirement.version)
    with session(team, team.reviewer) as access:
        requirement, _ = team.requirements.decide_review(
            access, project_id=team.project_a.project_id,
            requirement_id=requirement.requirement_id,
            revision_id=requirement.review_revision_id, decision="approved",
            expected_version=requirement.version)
    return requirement


def plan_item(team, key="A", *, acceptance=("AC-001", "AC-002"), estimate=4, depends_on=()):
    return {
        "key": key, "title": f"{key} 任务", "objective": "完成目标", "scope": "范围内",
        "non_goals": "不做的事", "duty": "实现", "assignee_user_id": team.worker.user_id,
        "binding_id": team.binding.binding_id, "acceptance_ids": list(acceptance),
        "estimated_units": estimate, "depends_on": list(depends_on),
    }


def published_plan(team, requirement, *items):
    return team.store.create_plan(
        requirement.project_id, requirement.requirement_id,
        revision_id=requirement.approved_revision_id,
        revision_hash=requirement.approved_revision_hash, status="published",
        report=json.dumps({"problems": [], "item_count": len(items)}),
        total_units=sum(item["estimated_units"] for item in items),
        created_by=team.reviewer.user_id, created_at=team.clock(),
        items=list(items) or [plan_item(team)])


def work_item(team, plan, key="A"):
    return next(item for item in team.store.list_work_items(plan.project_id, plan.plan_id)
                if item.key == key)


def live_instance(team, *, binding=None, worker_url=None):
    binding = binding or team.binding
    node = team.store.create_node("node-1", "f" * 64)
    instance = team.store.create_instance(
        binding.binding_id, node.node_id, claim_token_hash="c" * 64,
        claim_expires_at=team.clock() + 3600, created_by=team.owner.user_id,
        created_at=team.clock(), worker_url=worker_url)
    return team.store.mark_claimed(
        instance.instance_id, protocol_version="v1", capacity=1, isolation_mode="os_account",
        isolation_attestation=None, workspace_ref=binding.workspace_ref,
        provider_ref=binding.provider_ref, allowed_tools="[]", env_allowlist="[]",
        instance_token_hash="t" * 64, claimed_at=team.clock())


def dispatch(team, requirement, item, instance, *, number=1, control_epoch=1,
             input_hash="a" * 64, package_json=None):
    return team.store.create_attempt(
        requirement.project_id, item.work_item_id, plan_id=item.plan_id,
        requirement_id=requirement.requirement_id, revision_id=requirement.approved_revision_id,
        revision_hash=requirement.approved_revision_hash, instance_id=instance.instance_id,
        binding_id=item.binding_id, binding_version=team.binding.version,
        assignee_user_id=item.assignee_user_id, number=number, schema_version="package-v1",
        input_hash=input_hash,
        package_json=json.dumps({"work_item_id": item.work_item_id}) if package_json is None
        else package_json,
        control_epoch=control_epoch, lease_expires_at=team.clock() + 120, created_at=team.clock())


def ready_attempt(team):
    requirement = approved_requirement(team)
    plan = published_plan(team, requirement)
    item = work_item(team, plan)
    instance = live_instance(team)
    return requirement, item, instance, dispatch(team, requirement, item, instance)


# --- migration ----------------------------------------------------------------

def make_v5_database(path):
    with closing(sqlite3.connect(path, isolation_level=None)) as conn:
        conn.execute("BEGIN IMMEDIATE")
        for statement in (database_module._SCHEMA_STATEMENTS + database_module._AUTH_SCHEMA
                          + database_module._INSTANCE_SCHEMA
                          + database_module._REQUIREMENT_SCHEMA
                          + database_module._PLANNING_SCHEMA):
            conn.execute(statement)
        conn.execute("INSERT INTO users(user_id,display_name) VALUES('old-user','旧用户')")
        conn.execute("INSERT INTO projects(project_id,name,owner_id) "
                     "VALUES('old-project','旧项目','old-user')")
        conn.execute("INSERT INTO project_members(project_id,user_id,access_role) "
                     "VALUES('old-project','old-user','owner')")
        conn.execute("INSERT INTO agents(agent_id,owner_user_id,name) "
                     "VALUES('old-agent','old-user','旧 Agent')")
        conn.execute("INSERT INTO agent_bindings(binding_id,project_id,agent_id,member_user_id,"
                     "duty,persona_version,allowed_tools,workspace_ref,provider_ref) "
                     "VALUES('old-binding','old-project','old-agent','old-user','实现','p1',"
                     "'[\"Read\"]','ws','pv')")
        conn.execute("INSERT INTO nodes(node_id,name,credential_hash) "
                     "VALUES('old-node','旧节点',?)", ("n" * 64,))
        conn.execute("INSERT INTO execution_instances(instance_id,binding_id,node_id,"
                     "claim_token_hash,claim_expires_at,created_by,created_at,status) "
                     "VALUES('old-instance','old-binding','old-node',?,2000.0,'old-user',1.0,'active')",
                     ("c" * 64,))
        conn.execute("INSERT INTO requirements(requirement_id,project_id,title,requester_id,"
                     "status,version,created_at,updated_at) "
                     "VALUES('old-requirement','old-project','旧需求','old-user','executing',2,1.0,1.0)")
        conn.execute("INSERT INTO requirement_revisions(revision_id,project_id,requirement_id,number,"
                     "content,content_hash,content_size,author_id,created_at) "
                     "VALUES('old-revision','old-project','old-requirement',1,'原文',?,6,'old-user',1.0)",
                     ("a" * 64,))
        conn.execute("UPDATE requirements SET approved_revision_id='old-revision',"
                     "approved_revision_hash=?,current_revision_id='old-revision' "
                     "WHERE requirement_id='old-requirement'", ("a" * 64,))
        conn.execute("INSERT INTO plans(plan_id,project_id,requirement_id,revision_id,"
                     "revision_hash,status,report,total_units,created_at,published_at) "
                     "VALUES('old-plan','old-project','old-requirement','old-revision',?,"
                     "'published','{}',4,1.0,1.0)", ("a" * 64,))
        conn.execute("INSERT INTO work_items(work_item_id,plan_id,project_id,key,title,objective,"
                     "scope,non_goals,duty,assignee_user_id,binding_id,status,estimated_units,"
                     "created_at,updated_at) VALUES('old-item','old-plan','old-project','A','标题',"
                     "'目标','范围','非目标','实现','old-user','old-binding','ready',4,1.0,1.0)")
        conn.execute("PRAGMA user_version=5")
        conn.commit()


def test_v5_database_upgrades_to_v6_preserving_execution_rows(tmp_path):
    path = tmp_path / "v5.sqlite3"
    make_v5_database(path)
    database = Database(path)
    with pytest.raises(database_module.SchemaVersionError):
        with database.read():
            pytest.fail("Migration must be explicit")
    database.initialize()
    store = CollaborationStore(database)
    instance = store.get_instance("old-project", "old-instance")
    assert (instance.worker_url, instance.control_epoch) == (None, 1)
    assert store.get_work_item("old-project", "old-item").status == "ready"
    with database.read() as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master "
                                                 "WHERE type='table'")}
    assert {"attempts", "attempt_reports", "attempt_artifacts", "owner_reviews",
            "security_reviews", "security_grants", "work_item_acceptance_items"} <= tables


def test_v6_migration_failure_leaves_the_file_untouched(tmp_path, monkeypatch):
    path = tmp_path / "v5.sqlite3"
    make_v5_database(path)
    before = path.read_bytes()
    with monkeypatch.context() as patch:
        patch.setattr(database_module, "_EXECUTION_SCHEMA",
                      database_module._EXECUTION_SCHEMA[:1] + ("INVALID SQL",))
        with pytest.raises(sqlite3.OperationalError):
            Database(path).initialize()
    assert path.read_bytes() == before
    Database(path).initialize()
    assert CollaborationStore(Database(path)).get_user("old-user").version == 1


# --- instance registration ----------------------------------------------------

def test_worker_url_is_registered_and_rejects_extra_callback_targets(team):
    node = team.store.create_node("node-url", "u" * 64)
    with session(team, team.owner) as access:
        instance, _ = team.instances.register_slot(
            access, project_id=team.binding.project_id, binding_id=team.binding.binding_id,
            node_id=node.node_id, worker_url="https://worker.internal:8443/tasks")
    assert instance.worker_url == "https://worker.internal:8443/tasks"
    assert instance.control_epoch == 1
    for bad in ("ftp://worker.internal", "http://", "http://a b/", "http://user@host/",
                "http://host/?next=evil", "http://host#frag", "http://host/a\\b",
                "http://host:0", "http://host:99999", "http://host:" + "x" * 600,
                "http://" + "h" * 300, "http://-bad.host", "http://bad.host-", "javascript:alert(1)"):
        node = team.store.create_node(f"node-{hashlib.sha256(bad.encode()).hexdigest()[:8]}",
                                      hashlib.sha256(bad.encode()).hexdigest())
        with session(team, team.owner) as access, pytest.raises(ValidationError):
            team.instances.register_slot(
                access, project_id=team.binding.project_id, binding_id=team.binding.binding_id,
                node_id=node.node_id, worker_url=bad)


def test_disabling_an_instance_advances_its_control_epoch(team):
    instance = live_instance(team)
    assert instance.control_epoch == 1
    disabled = team.store.set_disabled(instance.instance_id, expected_version=instance.version)
    assert (disabled.status, disabled.control_epoch) == ("disabled", 2)
    assert team.store.get_instance(team.project_a.project_id,
                                   instance.instance_id).control_epoch == 2


# --- attempt identity ---------------------------------------------------------

def test_only_one_open_attempt_per_work_item(team):
    requirement, item, instance, attempt = ready_attempt(team)
    assert (attempt.status, attempt.lease_epoch, attempt.usage_units) == ("assigned", 1, 0)
    with pytest.raises(Conflict):
        dispatch(team, requirement, item, instance)
    team.store.advance_attempt(attempt.attempt_id, status="failed",
                               expected_version=attempt.version, updated_at=team.clock())
    retry = dispatch(team, requirement, item, instance,
                     number=team.store.next_attempt_number(item.work_item_id))
    assert retry.number == 2
    assert team.store.next_attempt_number(item.work_item_id) == 3
    assert [entry.attempt_id for entry in team.store.list_attempts(
        requirement.project_id, item.work_item_id)] == [attempt.attempt_id, retry.attempt_id]
    assert team.store.open_attempt(item.work_item_id).attempt_id == retry.attempt_id


def test_attempt_identity_columns_are_immutable(team):
    requirement, item, instance, attempt = ready_attempt(team)
    for column, value in (("work_item_id", "'other'"), ("project_id", "'other'"),
                          ("input_hash", "?"), ("number", "9"), ("instance_id", "'other'")):
        sql = f"UPDATE attempts SET {column}={value} WHERE attempt_id='{attempt.attempt_id}'"
        with pytest.raises(sqlite3.IntegrityError):
            with team.database.transaction() as conn:
                conn.execute(sql, ("b" * 64,) if column == "input_hash" else ())
    updated = team.store.advance_attempt(attempt.attempt_id, status="running",
                                         expected_version=attempt.version, updated_at=team.clock())
    assert (updated.status, updated.version) == ("running", 2)


def test_attempt_rows_are_bound_to_the_same_project_and_revision(team):
    requirement, item, instance, attempt = ready_attempt(team)
    other = approved_requirement_other_project(team)
    with pytest.raises(Conflict):
        team.store.create_attempt(other.project_id, item.work_item_id, plan_id=item.plan_id,
                                  requirement_id=other.requirement_id,
                                  revision_id=other.approved_revision_id,
                                  revision_hash=other.approved_revision_hash,
                                  instance_id=instance.instance_id, binding_id=item.binding_id,
                                  binding_version=1, assignee_user_id=item.assignee_user_id,
                                  number=1, schema_version="package-v1", input_hash="a" * 64,
                                  package_json="{}", control_epoch=1,
                                  lease_expires_at=team.clock(), created_at=team.clock())
    with pytest.raises(ValidationError):
        dispatch(team, requirement, item, instance, input_hash="short")
    with pytest.raises(ValidationError):
        dispatch(team, requirement, item, instance, number=0)
    team.store.advance_attempt(attempt.attempt_id, status="cancelled",
                               expected_version=attempt.version, updated_at=team.clock())
    with pytest.raises(ValidationError):
        dispatch(team, requirement, item, instance, number=2, package_json="   ")
    with pytest.raises(ValidationError):
        dispatch(team, requirement, item, instance, number=2, control_epoch=0)


def approved_requirement_other_project(team):
    with session(team, team.owner) as access:
        requirement = team.requirements.create_requirement(
            access, project_id=team.project_b.project_id, title="别的项目需求", content="# 原文",
            acceptance_items=["第一条"])
    with session(team, team.owner) as access:
        requirement, _ = team.requirements.request_review(
            access, project_id=team.project_b.project_id,
            requirement_id=requirement.requirement_id,
            revision_id=requirement.current_revision_id,
            reviewer_user_id=team.owner.user_id, expected_version=requirement.version)
    with session(team, team.owner) as access:
        requirement, _ = team.requirements.decide_review(
            access, project_id=team.project_b.project_id,
            requirement_id=requirement.requirement_id,
            revision_id=requirement.review_revision_id, decision="approved",
            expected_version=requirement.version)
    return requirement


def test_attempt_lookup_is_scoped_to_its_project(team):
    requirement, item, instance, attempt = ready_attempt(team)
    assert team.store.get_attempt(requirement.project_id, attempt.attempt_id) == attempt
    with pytest.raises(NotFound):
        team.store.get_attempt(team.project_b.project_id, attempt.attempt_id)
    with pytest.raises(NotFound):
        team.store.list_attempts(team.project_b.project_id, item.work_item_id)


# --- leases and control epochs ------------------------------------------------

def test_lease_takeover_and_stale_reports(team):
    requirement, item, instance, attempt = ready_attempt(team)
    bumped = team.store.bump_attempt_lease(attempt.attempt_id, control_epoch=1,
                                           lease_expires_at=team.clock() + 300,
                                           updated_at=team.clock())
    assert (bumped.lease_epoch, bumped.version) == (2, 2)
    with pytest.raises(Conflict):
        team.store.advance_attempt(attempt.attempt_id, status="running",
                                   expected_version=attempt.version, updated_at=team.clock(),
                                   lease_epoch=1)
    running = team.store.advance_attempt(bumped.attempt_id, status="running",
                                         expected_version=bumped.version, updated_at=team.clock(),
                                         lease_epoch=2)
    assert running.lease_epoch == 2
    with pytest.raises(Conflict):
        team.store.bump_attempt_lease(attempt.attempt_id, control_epoch=7,
                                      lease_expires_at=team.clock(), updated_at=team.clock())
    with pytest.raises(Conflict):
        team.store.advance_attempt(attempt.attempt_id, status="submitted",
                                   expected_version=running.version, updated_at=team.clock(),
                                   control_epoch=7)


def test_revoked_instance_epoch_fails_open_attempts_only(team):
    requirement, item, instance, attempt = ready_attempt(team)
    stale = team.store.advance_attempt(attempt.attempt_id, status="failed",
                                       expected_version=attempt.version, updated_at=team.clock())
    reopened = dispatch(team, requirement, item, instance, number=2, control_epoch=1)
    disabled = team.store.set_disabled(instance.instance_id)
    changed = team.store.set_attempt_control_epoch(instance.instance_id, disabled.control_epoch)
    assert changed == 1
    frozen = team.store.get_attempt(requirement.project_id, reopened.attempt_id)
    assert (frozen.control_epoch, frozen.status) == (2, "assigned")
    assert team.store.get_attempt(requirement.project_id, stale.attempt_id).control_epoch == 1
    with pytest.raises(Conflict):
        team.store.bump_attempt_lease(reopened.attempt_id, control_epoch=1,
                                      lease_expires_at=team.clock(), updated_at=team.clock())


def test_closing_an_attempt_freezes_its_result_fields(team):
    requirement, item, instance, attempt = ready_attempt(team)
    result = json.dumps({"summary": "完成"}, ensure_ascii=False)
    submitted = team.store.advance_attempt(
        attempt.attempt_id, status="submitted", expected_version=attempt.version,
        updated_at=team.clock(), result_json=result,
        result_hash=hashlib.sha256(result.encode("utf-8")).hexdigest(), usage_units=3,
        trace_id="trace-1")
    assert (submitted.status, submitted.usage_units, submitted.trace_id) == ("submitted", 3, "trace-1")
    with pytest.raises(ValidationError):
        team.store.advance_attempt(attempt.attempt_id, status="submitted",
                                   expected_version=submitted.version, updated_at=team.clock(),
                                   usage_units=-1)
    with pytest.raises(ValidationError):
        team.store.advance_attempt(attempt.attempt_id, status="submitted",
                                   expected_version=submitted.version, updated_at=team.clock(),
                                   result_hash="nope")


# --- callback reports ---------------------------------------------------------

def test_reports_record_rejected_callbacks_without_changing_state(team):
    requirement, item, instance, attempt = ready_attempt(team)
    accepted = team.store.record_attempt_report(
        attempt.attempt_id, stage="ack", lease_epoch=1, control_epoch=1, input_hash="a" * 64,
        payload=json.dumps({"ok": True}), accepted=True, reason=None, created_at=team.clock())
    late = team.store.record_attempt_report(
        attempt.attempt_id, stage="result", lease_epoch=1, control_epoch=1, input_hash="b" * 64,
        payload="{}", accepted=False, reason="stale lease epoch", created_at=team.clock() + 1)
    assert (accepted.accepted, accepted.reason) == (True, None)
    assert (late.accepted, late.reason, late.project_id) == (False, "stale lease epoch",
                                                             requirement.project_id)
    assert [(report.stage, report.accepted) for report in team.store.list_attempt_reports(
        requirement.project_id, attempt.attempt_id)] == [("ack", True), ("result", False)]
    assert team.store.get_attempt(requirement.project_id, attempt.attempt_id) == attempt
    with pytest.raises(ValidationError):
        team.store.record_attempt_report(attempt.attempt_id, stage="unknown", lease_epoch=1,
                                         control_epoch=1, input_hash="a" * 64, payload="{}",
                                         accepted=True, reason=None, created_at=team.clock())
    with pytest.raises(ValidationError):
        team.store.record_attempt_report(attempt.attempt_id, stage="ack", lease_epoch=1,
                                         control_epoch=1, input_hash="a" * 64,
                                         payload="x" * 300000, accepted=True, reason=None,
                                         created_at=team.clock())
    with pytest.raises(NotFound):
        team.store.list_attempt_reports(team.project_b.project_id, attempt.attempt_id)


# --- artifacts ----------------------------------------------------------------

def test_artifact_content_is_verified_immutable_and_project_scoped(team):
    requirement, item, instance, attempt = ready_attempt(team)
    content = "产物内容".encode("utf-8")
    digest = hashlib.sha256(content).hexdigest()
    artifact = team.store.add_attempt_artifact(attempt.attempt_id, name="result.md",
                                               media_type="text/markdown", content=content,
                                               sha256=digest, created_at=team.clock())
    assert (artifact.size_bytes, artifact.sha256, artifact.work_item_id) == (
        len(content), digest, item.work_item_id)
    assert not hasattr(artifact, "content")
    assert team.store.get_artifact_content(requirement.project_id, artifact.artifact_id) == content
    assert [entry.name for entry in team.store.list_artifacts(
        requirement.project_id, attempt.attempt_id)] == ["result.md"]
    assert team.store.get_artifact_by_name(attempt.attempt_id, "result.md").artifact_id \
        == artifact.artifact_id
    with pytest.raises(ValidationError):
        team.store.add_attempt_artifact(attempt.attempt_id, name="bad.md", media_type="text/plain",
                                        content=content, sha256="f" * 64, created_at=team.clock())
    with pytest.raises(ValidationError):
        team.store.add_attempt_artifact(attempt.attempt_id, name="bad.md", media_type="text/plain",
                                        content="str", sha256=digest, created_at=team.clock())
    with pytest.raises(Conflict):
        team.store.add_attempt_artifact(attempt.attempt_id, name="result.md",
                                        media_type="text/markdown", content=content,
                                        sha256=digest, created_at=team.clock())
    with pytest.raises(NotFound):
        team.store.get_artifact(team.project_b.project_id, artifact.artifact_id)
    with pytest.raises(NotFound):
        team.store.get_artifact_content(team.project_b.project_id, artifact.artifact_id)
    mutations = (
        ("UPDATE attempt_artifacts SET content=? WHERE artifact_id=?", (content, artifact.artifact_id)),
        ("UPDATE attempt_artifacts SET name='other' WHERE artifact_id=?", (artifact.artifact_id,)),
        ("DELETE FROM attempt_artifacts WHERE artifact_id=?", (artifact.artifact_id,)),
    )
    for statement, params in mutations:
        with pytest.raises(sqlite3.IntegrityError):
            with team.database.transaction() as conn:
                conn.execute(statement, params)


def test_artifacts_survive_after_the_attempt_closes(team):
    requirement, item, instance, attempt = ready_attempt(team)
    content = b"final"
    artifact = team.store.add_attempt_artifact(attempt.attempt_id, name="patch.diff",
                                               media_type="text/plain", content=content,
                                               sha256=hashlib.sha256(content).hexdigest(),
                                               created_at=team.clock())
    closed = team.store.advance_attempt(attempt.attempt_id, status="accepted",
                                        expected_version=attempt.version, updated_at=team.clock())
    assert closed.status == "accepted"
    assert team.store.get_artifact_content(requirement.project_id, artifact.artifact_id) == content


# --- owner review and security gate -------------------------------------------

def test_owner_review_is_written_once_per_attempt(team):
    requirement, item, instance, attempt = ready_attempt(team)
    result_hash = "d" * 64
    review = team.store.add_owner_review(attempt.attempt_id, reviewer_user_id=team.worker.user_id,
                                         decision="confirmed", result_hash=result_hash,
                                         reason="核对原文一致", created_at=team.clock())
    assert (review.work_item_id, review.decision) == (item.work_item_id, "confirmed")
    assert team.store.get_owner_review(attempt.attempt_id).review_id == review.review_id
    with pytest.raises(Conflict):
        team.store.add_owner_review(attempt.attempt_id, reviewer_user_id=team.worker.user_id,
                                    decision="changes_requested", result_hash="e" * 64,
                                    reason="重复", created_at=team.clock())
    assert team.store.get_owner_review("missing-attempt") is None
    with pytest.raises(ValidationError):
        team.store.add_owner_review(attempt.attempt_id, reviewer_user_id=team.worker.user_id,
                                    decision="approved", result_hash=result_hash, reason="",
                                    created_at=team.clock())


def test_security_reviews_have_one_decision_and_delegable_grants(team):
    requirement, item, instance, attempt = ready_attempt(team)
    policy_hash = hashlib.sha256(b"policy").hexdigest()
    review = team.store.add_security_review(
        attempt.attempt_id, rule_key="rm-rules", requirement_id=requirement.requirement_id,
        revision_id=requirement.approved_revision_id, binding_version=team.binding.version,
        tool_policy_version=policy_hash, expires_at=team.clock() + 600, created_at=team.clock())
    assert (review.decision, review.input_hash, review.project_id) == (
        "pending", "a" * 64, requirement.project_id)
    assert team.store.security_review_by_rule(attempt.attempt_id, "rm-rules").version == 1
    assert [entry.rule_key for entry in team.store.list_security_reviews(
        requirement.project_id, attempt.attempt_id)] == ["rm-rules"]
    with pytest.raises(Conflict):
        team.store.add_security_review(attempt.attempt_id, rule_key="rm-rules",
                                       requirement_id=requirement.requirement_id,
                                       revision_id=requirement.approved_revision_id,
                                       binding_version=1, tool_policy_version=policy_hash,
                                       expires_at=team.clock() + 600, created_at=team.clock())
    decided = team.store.decide_security_review(review.security_review_id, decision="approved",
                                                reviewer_user_id=team.reviewer.user_id,
                                                reason="允许执行", expected_version=review.version,
                                                updated_at=team.clock())
    assert (decided.decision, decided.reviewer_user_id) == ("approved", team.reviewer.user_id)
    with pytest.raises(InvalidState):
        team.store.decide_security_review(review.security_review_id, decision="rejected",
                                          reviewer_user_id=team.reviewer.user_id, reason="改主意",
                                          expected_version=decided.version, updated_at=team.clock())
    with pytest.raises(ValidationError):
        team.store.add_security_review(attempt.attempt_id, rule_key="expired",
                                       requirement_id=requirement.requirement_id,
                                       revision_id=requirement.approved_revision_id,
                                       binding_version=1, tool_policy_version=policy_hash,
                                       expires_at=team.clock(), created_at=team.clock())
    assert team.store.get_security_grant(requirement.project_id, team.reviewer.user_id,
                                         "security:approve") is None
    grant = team.store.upsert_security_grant(requirement.project_id, team.reviewer.user_id,
                                             capability="security:approve",
                                             granted_by=team.owner.user_id, active=True,
                                             created_at=team.clock())
    assert (grant.status, grant.version) == ("active", 1)
    revoked = team.store.upsert_security_grant(requirement.project_id, team.reviewer.user_id,
                                               capability="security:approve",
                                               granted_by=team.owner.user_id, active=False,
                                               created_at=team.clock(),
                                               expected_version=grant.version)
    assert (revoked.status, revoked.version) == ("revoked", 2)
    with pytest.raises(Conflict):
        team.store.upsert_security_grant(requirement.project_id, team.reviewer.user_id,
                                         capability="security:approve",
                                         granted_by=team.owner.user_id, active=True,
                                         created_at=team.clock(), expected_version=1)
    with pytest.raises(ValidationError):
        team.store.upsert_security_grant(requirement.project_id, team.reviewer.user_id,
                                         capability="shell:execute", granted_by=team.owner.user_id,
                                         active=True, created_at=team.clock())
    assert [grant.user_id for grant in team.store.list_security_grants(
        requirement.project_id)] == [team.reviewer.user_id]
    with pytest.raises(sqlite3.IntegrityError):
        with team.database.transaction() as conn:
            conn.execute("UPDATE security_reviews SET decision='rejected' "
                         "WHERE security_review_id=?", (review.security_review_id,))
    with pytest.raises(sqlite3.IntegrityError):
        with team.database.transaction() as conn:
            conn.execute("DELETE FROM security_reviews WHERE security_review_id=?",
                         (review.security_review_id,))


# --- work item states and acceptance links ------------------------------------

def test_published_work_items_carry_their_acceptance_items(team):
    requirement = approved_requirement(team)
    plan = published_plan(team, requirement, plan_item(team, "A", acceptance=("AC-002",)),
                          plan_item(team, "B", depends_on=("A",)))
    first = work_item(team, plan, "A")
    assert team.store.list_work_item_acceptance(first.work_item_id) == [("AC-002", "第二条验收")]
    assert team.store.list_work_item_acceptance(
        work_item(team, plan, "B").work_item_id) == [("AC-001", "第一条验收"),
                                                     ("AC-002", "第二条验收")]
    assert team.store.list_work_item_acceptance("missing-item") == []


def test_work_items_advance_through_execution_statuses(team):
    requirement = approved_requirement(team)
    plan = published_plan(team, requirement)
    item = work_item(team, plan)
    version = item.version
    for status in ("running", "awaiting_owner_review", "submitted", "accepted"):
        updated = team.store.advance_work_item(item.work_item_id, status=status,
                                               expected_version=version, updated_at=team.clock())
        version = updated.version
    assert updated.status == "accepted"
    assert team.store.dependency_gap_count(item.work_item_id) == 0
    with pytest.raises(ValidationError):
        team.store.advance_work_item(item.work_item_id, status="done", expected_version=version,
                                     updated_at=team.clock())


# --- budget settlement --------------------------------------------------------

def test_settlement_and_release_are_idempotent_per_attempt(team):
    requirement, item, instance, attempt = ready_attempt(team)
    team.store.upsert_budget_account("project", requirement.project_id, limit_units=100,
                                     expected_version=None, updated_at=team.clock())
    team.budgets.reserve(team.store, project_id=requirement.project_id,
                         requirement_id=requirement.requirement_id, plan_id="plan-1",
                         amount=item.estimated_units, created_at=team.clock())
    team.budgets.settle(team.store, project_id=requirement.project_id,
                        requirement_id=requirement.requirement_id, attempt_id=attempt.attempt_id,
                        reserved_units=item.estimated_units, actual_units=6,
                        created_at=team.clock())
    team.budgets.settle(team.store, project_id=requirement.project_id,
                        requirement_id=requirement.requirement_id, attempt_id=attempt.attempt_id,
                        reserved_units=item.estimated_units, actual_units=6,
                        created_at=team.clock())
    scope_entries = team.store.list_budget_entries("project", requirement.project_id)
    assert [(entry.ref_type, entry.kind, entry.delta_units) for entry in scope_entries] == [
        ("plan", "reserve", 4), ("attempt", "settle", 2)]
    team.budgets.release(team.store, project_id=requirement.project_id,
                         requirement_id=requirement.requirement_id,
                         attempt_id=attempt.attempt_id, reserved_units=6,
                         created_at=team.clock())
    assert sum(entry.delta_units for entry in team.store.list_budget_entries(
        "project", requirement.project_id)) == 0
    with pytest.raises(ValidationError):
        team.budgets.settle(team.store, project_id=requirement.project_id,
                            requirement_id=requirement.requirement_id,
                            attempt_id=attempt.attempt_id, reserved_units=4,
                            actual_units="6", created_at=team.clock())
    with pytest.raises(ValidationError):
        team.budgets.release(team.store, project_id=requirement.project_id,
                             requirement_id=requirement.requirement_id,
                             attempt_id=attempt.attempt_id, reserved_units=0,
                             created_at=team.clock())


# --- dispatch -----------------------------------------------------------------

def events(team, project_id, topic):
    return [event for event in team.store.list_events(project_id) if event.topic == topic]


def test_dispatch_freezes_the_package_and_moves_the_work_item(team):
    requirement = approved_requirement(team)
    plan = published_plan(team, requirement)
    item = work_item(team, plan)
    instance = live_instance(team)
    service = team.make_dispatch(lease_ttl=60)
    attempt = service.dispatch(requirement.project_id, item.work_item_id, actor_id=None,
                               requires_real_data=False)
    assert (attempt.status, attempt.control_epoch, attempt.lease_epoch) == ("assigned", 1, 1)
    assert attempt.lease_expires_at == team.clock() + 60
    assert attempt.input_hash == hashlib.sha256(attempt.package_json.encode("utf-8")).hexdigest()
    package = json.loads(attempt.package_json)
    assert package["documents"] == [{
        "kind": "requirement_text", "revision_id": plan.revision_id,
        "content_hash": requirement.approved_revision_hash, "size_bytes": len("# 需求原文".encode()),
        "media_type": "text/markdown;charset=utf-8", "href": "requirement", "complete": True}]
    assert "# 需求原文" not in attempt.package_json
    assert package["subtask"]["acceptance"] == [
        {"item_id": "AC-001", "text": "第一条验收"}, {"item_id": "AC-002", "text": "第二条验收"}]
    assert package["policy"]["allowed_tools"] == ["Read", "Edit"]
    assert package["policy"]["binding_version"] == team.binding.version
    assert package["policy"]["budget_units"] == item.estimated_units
    assert package["dependencies"] == []
    assert team.store.get_work_item(requirement.project_id,
                                    item.work_item_id).status == "running"
    listed = events(team, requirement.project_id, TOPIC_ATTEMPT_ASSIGNED)
    assert len(listed) == 1
    assert json.loads(listed[0].payload) == {
        "attempt_id": attempt.attempt_id, "work_item_id": item.work_item_id,
        "instance_id": instance.instance_id, "input_hash": attempt.input_hash,
        "control_epoch": 1}
    with team.database.read() as conn:
        assert conn.execute("SELECT action FROM collaboration_audit WHERE target_id=?",
                            (attempt.attempt_id,)).fetchone()[0] == "attempt.assigned"


def test_dispatch_is_idempotent_until_the_frozen_inputs_change(team):
    requirement = approved_requirement(team)
    plan = published_plan(team, requirement)
    item = work_item(team, plan)
    live_instance(team)
    service = team.make_dispatch()
    first = service.dispatch(requirement.project_id, item.work_item_id, requires_real_data=False)
    again = service.dispatch(requirement.project_id, item.work_item_id, requires_real_data=False)
    assert again.attempt_id == first.attempt_id
    assert len(events(team, requirement.project_id, TOPIC_ATTEMPT_ASSIGNED)) == 1
    binding = team.store.get_binding(requirement.project_id, team.binding.binding_id)
    team.store.update_binding(requirement.project_id, binding.binding_id,
                              expected_version=binding.version, duty=binding.duty,
                              persona_version="persona-v2", allowed_tools=binding.allowed_tools,
                              workspace_ref=binding.workspace_ref,
                              provider_ref=binding.provider_ref)
    with pytest.raises(Conflict):
        service.dispatch(requirement.project_id, item.work_item_id, requires_real_data=False)
    assert team.store.open_attempt(item.work_item_id).attempt_id == first.attempt_id
    team.store.advance_attempt(first.attempt_id, status="cancelled",
                               expected_version=first.version, updated_at=team.clock())
    running = team.store.get_work_item(requirement.project_id, item.work_item_id)
    team.store.advance_work_item(item.work_item_id, status="waiting_agent",
                                 expected_version=running.version, updated_at=team.clock())
    retry = service.dispatch(requirement.project_id, item.work_item_id, requires_real_data=False)
    assert (retry.number, retry.input_hash) != (first.number, first.input_hash)
    assert team.store.get_work_item(requirement.project_id,
                                    item.work_item_id).status == "running"


def test_dispatch_waits_for_a_live_verified_instance(team):
    requirement = approved_requirement(team)
    plan = published_plan(team, requirement)
    item = work_item(team, plan)
    service = team.make_dispatch()
    with pytest.raises(AdmissionDenied) as denial:
        service.dispatch(requirement.project_id, item.work_item_id, requires_real_data=True)
    assert denial.value.reason == "no_live_instance"
    assert team.store.get_work_item(requirement.project_id, item.work_item_id).status == "ready"
    instance = live_instance(team)
    with pytest.raises(AdmissionDenied) as denial:
        service.dispatch(requirement.project_id, item.work_item_id, requires_real_data=True)
    assert denial.value.reason == "isolation_unverified"
    with pytest.raises(AdmissionDenied):
        service.dispatch(requirement.project_id, item.work_item_id)
    assert service.dispatch(requirement.project_id, item.work_item_id,
                            requires_real_data=False).status == "assigned"
    team.store.set_verified(instance.instance_id, instance.version, team.clock())
    assert team.store.get_instance(requirement.project_id,
                                   instance.instance_id).isolation_verified
    open_attempt = team.store.open_attempt(item.work_item_id)
    team.store.advance_attempt(open_attempt.attempt_id, status="cancelled",
                               expected_version=open_attempt.version, updated_at=team.clock())
    current = team.store.get_work_item(requirement.project_id, item.work_item_id)
    team.store.advance_work_item(item.work_item_id, status="waiting_agent",
                                 expected_version=current.version, updated_at=team.clock())
    assert service.dispatch(requirement.project_id, item.work_item_id).number == 2


def test_dispatch_waits_for_accepted_dependencies(team):
    requirement = approved_requirement(team)
    plan = published_plan(team, requirement, plan_item(team, "A"),
                          plan_item(team, "B", depends_on=("A",)))
    first = work_item(team, plan, "A")
    second = work_item(team, plan, "B")
    service = team.make_dispatch()
    with pytest.raises(InvalidState):
        service.dispatch(requirement.project_id, second.work_item_id, requires_real_data=False)
    live_instance(team)
    # A stale queue reading must still not let a downstream item consume missing evidence.
    version = team.store.advance_work_item(second.work_item_id, status="ready",
                                           expected_version=second.version,
                                           updated_at=team.clock()).version
    with pytest.raises(AdmissionDenied) as denial:
        service.dispatch(requirement.project_id, second.work_item_id, requires_real_data=False)
    assert denial.value.reason == "dependency_not_accepted"
    assert team.store.get_work_item(requirement.project_id,
                                    second.work_item_id).version == version
    attempt = service.dispatch(requirement.project_id, first.work_item_id,
                               requires_real_data=False)
    content = b"upstream result"
    artifact = team.store.add_attempt_artifact(attempt.attempt_id, name="out.txt",
                                               media_type="text/plain", content=content,
                                               sha256=hashlib.sha256(content).hexdigest(),
                                               created_at=team.clock())
    closed = team.store.advance_attempt(attempt.attempt_id, status="accepted",
                                        expected_version=attempt.version, updated_at=team.clock())
    team.store.advance_work_item(first.work_item_id, status="accepted",
                                 expected_version=team.store.get_work_item(
                                     requirement.project_id, first.work_item_id).version,
                                 updated_at=team.clock())
    package = json.loads(service.dispatch(requirement.project_id, second.work_item_id,
                                          requires_real_data=False).package_json)
    assert package["dependencies"] == [{
        "work_item_id": first.work_item_id, "key": "A", "title": first.title,
        "attempt_id": closed.attempt_id,
        "artifacts": [{"artifact_id": artifact.artifact_id, "name": "out.txt",
                       "media_type": "text/plain", "sha256": artifact.sha256,
                       "size_bytes": artifact.size_bytes,
                       "href": f"artifacts/{artifact.artifact_id}"}]}]


def test_wake_is_advisory_and_delivery_happens_once(team):
    requirement = approved_requirement(team)
    plan = published_plan(team, requirement)
    item = work_item(team, plan)
    instance = live_instance(team, worker_url="http://127.0.0.1:9000/tasks")
    calls = []
    service = team.make_dispatch(push=lambda url, body: calls.append((url, body)))
    dispatcher = OutboxDispatcher(team.database, clock=team.clock)
    dispatcher.register(TOPIC_ATTEMPT_ASSIGNED, service.wake)
    attempt = service.dispatch(requirement.project_id, item.work_item_id,
                               requires_real_data=False)
    assert dispatcher.dispatch_once(requirement.project_id)["delivered"] == 1
    assert calls == [(instance.worker_url, {
        "attempt_id": attempt.attempt_id, "work_item_id": item.work_item_id,
        "input_hash": attempt.input_hash, "control_epoch": 1})]
    assert dispatcher.dispatch_once(requirement.project_id)["claimed"] == 0
    delivered = events(team, requirement.project_id, TOPIC_ATTEMPT_ASSIGNED)[0]
    assert delivered.delivered_at is not None and delivered.last_error is None


def test_wake_defers_when_the_dispatch_transport_fails(team):
    requirement = approved_requirement(team)
    plan = published_plan(team, requirement)
    item = work_item(team, plan)
    live_instance(team, worker_url="http://127.0.0.1:9111/tasks")
    service = team.make_dispatch(push=lambda url, body: (_ for _ in ()).throw(OSError("timeout")))
    dispatcher = OutboxDispatcher(team.database, clock=team.clock, base_delay=10)
    dispatcher.register(TOPIC_ATTEMPT_ASSIGNED, service.wake)
    service.dispatch(requirement.project_id, item.work_item_id, requires_real_data=False)
    assert dispatcher.dispatch_once(requirement.project_id)["deferred"] == 2
    pending = events(team, requirement.project_id, TOPIC_ATTEMPT_ASSIGNED)[0]
    assert (pending.attempts, pending.last_error) == (1, "OSError")


def test_wake_defers_until_the_owner_registers_a_worker_url(team):
    requirement = approved_requirement(team)
    plan = published_plan(team, requirement)
    item = work_item(team, plan)
    live_instance(team)
    service = team.make_dispatch(push=lambda url, body: None)
    dispatcher = OutboxDispatcher(team.database, clock=team.clock, base_delay=10)
    dispatcher.register(TOPIC_ATTEMPT_ASSIGNED, service.wake)
    attempt = service.dispatch(requirement.project_id, item.work_item_id,
                               requires_real_data=False)
    assert dispatcher.dispatch_once(requirement.project_id)["deferred"] == 2
    pending = events(team, requirement.project_id, TOPIC_ATTEMPT_ASSIGNED)[0]
    assert (pending.attempts, pending.last_error) == (1, "AdmissionDenied")
    team.store.set_disabled(attempt.instance_id)
    assert dispatcher.dispatch_once(requirement.project_id)["deferred"] == 0


def test_scheduler_tick_auto_dispatches_only_when_admission_allows(team):
    requirement = approved_requirement(team)
    plan = published_plan(team, requirement)
    item = work_item(team, plan)
    queue = QueueService(team.database, heartbeat_timeout=120, clock=team.clock)
    guarded = Scheduler(team.database, planning=SimpleNamespace(), queue=queue,
                        dispatch=team.make_dispatch(), auto_dispatch=True, clock=team.clock)
    assert guarded.tick(requirement.project_id)["attempts_dispatched"] == 0
    live_instance(team)
    assert guarded.tick(requirement.project_id)["attempts_dispatched"] == 0
    assert team.store.open_attempt(item.work_item_id) is None

    service = team.make_dispatch(requires_real_data=False)
    scheduler = Scheduler(team.database, planning=SimpleNamespace(), queue=queue,
                          dispatch=service, auto_dispatch=True, clock=team.clock)
    assert scheduler.tick(requirement.project_id)["attempts_dispatched"] == 1
    assert team.store.open_attempt(item.work_item_id).status == "assigned"
    assert scheduler.tick(requirement.project_id)["attempts_dispatched"] == 0
    manual = Scheduler(team.database, planning=SimpleNamespace(), queue=queue, clock=team.clock)
    assert "attempts_dispatched" not in manual.tick(requirement.project_id)


# --- machine execution protocol ------------------------------------------------

def claimed_instance(team, *, binding=None, worker_url=None):
    """A live instance plus a machine token whose digest only this test knows."""
    binding = binding or team.binding
    token = secrets.token_urlsafe(32)
    name = f"node-{secrets.token_hex(8)}"
    node = team.store.create_node(name, hashlib.sha256(name.encode("utf-8")).hexdigest())
    instance = team.store.create_instance(
        binding.binding_id, node.node_id,
        claim_token_hash=hashlib.sha256(f"claim-{name}".encode("utf-8")).hexdigest(),
        claim_expires_at=team.clock() + 3600, created_by=team.owner.user_id,
        created_at=team.clock(), worker_url=worker_url)
    return team.store.mark_claimed(
        instance.instance_id, protocol_version="v1", capacity=1, isolation_mode="os_account",
        isolation_attestation=None, workspace_ref=binding.workspace_ref,
        provider_ref=binding.provider_ref, allowed_tools="[]", env_allowlist="[]",
        instance_token_hash=hashlib.sha256(token.encode("ascii")).hexdigest(),
        claimed_at=team.clock()), token


def live_attempt(team, *, key="A", lease_ttl=120.0, items=None, requirement=None):
    requirement = requirement or approved_requirement(team)
    plan = published_plan(team, requirement, *(items or ()))
    item = work_item(team, plan, key)
    instance, token = claimed_instance(team)
    attempt = team.make_dispatch(lease_ttl=lease_ttl,
                                 requires_real_data=False).dispatch(
        requirement.project_id, item.work_item_id)
    return requirement, plan, item, instance, token, attempt


def claims(attempt):
    return {"input_hash": attempt.input_hash, "control_epoch": attempt.control_epoch,
            "lease_epoch": attempt.lease_epoch}


def stored_reports(team, project_id, attempt_id):
    return [(report.stage, bool(report.accepted), report.reason) for report in
            team.store.list_attempt_reports(project_id, attempt_id)]


def audited(team, target_id):
    with team.database.read() as conn:
        return [row[0] for row in conn.execute(
            "SELECT action FROM collaboration_audit WHERE target_id=? ORDER BY rowid",
            (target_id,))]


def test_worker_pulls_frozen_package_and_complete_original_text(team):
    requirement, _plan, _item, _instance, token, attempt = live_attempt(team)
    service = team.make_execution()
    payload, pulled = service.package(token, attempt.attempt_id)
    assert pulled.attempt_id == attempt.attempt_id
    assert hashlib.sha256(payload).hexdigest() == attempt.input_hash
    assert json.loads(payload)["work_item_id"] == attempt.work_item_id
    text, _ = service.requirement_text(token, attempt.attempt_id)
    assert text.decode("utf-8") == "# 需求原文"
    assert hashlib.sha256(text).hexdigest() == attempt.revision_hash
    assert [entry.attempt_id for entry in service.held_attempts(token)] == [attempt.attempt_id]
    assert service.artifacts(token, attempt.attempt_id) == []
    _other, other_token = claimed_instance(team, binding=team.other_binding)
    with pytest.raises(NotFound):
        service.package(other_token, attempt.attempt_id)
    with pytest.raises(NotFound):
        service.requirement_text(other_token, attempt.attempt_id)
    with pytest.raises(NotFound):
        service.package(token, "0" * 32)
    with pytest.raises(NotFound):
        service.artifacts(token, "0" * 32)
    with pytest.raises(Unauthorized):
        service.package("x" * 43, attempt.attempt_id)
    with pytest.raises(Unauthorized):
        service.held_attempts("x" * 43)
    # Reading is side-effect free: the requirement and the attempt are untouched.
    assert team.store.get_attempt(requirement.project_id,
                                  attempt.attempt_id) == attempt
    assert team.store.get_requirement(requirement.project_id,
                                      requirement.requirement_id).status == "approved"


def test_progress_callbacks_advance_once_and_repeat_without_effect(team):
    requirement, _plan, item, _instance, token, attempt = live_attempt(team, lease_ttl=60)
    service = team.make_execution(lease_ttl=60)
    acked = service.ack(token, attempt.attempt_id, **claims(attempt))
    assert (acked.outcome, acked.attempt.status, acked.attempt.version) == ("accepted", "acked", 2)
    repeated = service.ack(token, attempt.attempt_id, **claims(acked.attempt))
    assert (repeated.outcome, repeated.attempt.version) == ("duplicate", 2)
    started = service.start(token, attempt.attempt_id, trace_id="trace-1",
                            **claims(repeated.attempt))
    assert (started.attempt.status, started.attempt.trace_id) == ("running", "trace-1")
    assert service.start(token, attempt.attempt_id, **claims(started.attempt)).outcome \
        == "duplicate"
    renewed = service.renew_lease(token, attempt.attempt_id, **claims(started.attempt))
    assert (renewed.attempt.lease_epoch, renewed.attempt.lease_expires_at) == (
        2, team.clock() + 60)
    with pytest.raises(Conflict):
        service.renew_lease(token, attempt.attempt_id, **claims(started.attempt))
    second = service.renew_lease(token, attempt.attempt_id, **claims(renewed.attempt))
    team.clock.advance(45)
    result = service.report_result(token, attempt.attempt_id, status="submitted",
                                   summary="完成", usage_units=3, trace_id="trace-2",
                                   **claims(second.attempt))
    assert (result.attempt.status, result.attempt.usage_units, result.attempt.trace_id) == (
        "submitted", 3, "trace-2")
    assert result.attempt.result_hash == hashlib.sha256(
        json.dumps({"artifacts": [], "summary": "完成"}, ensure_ascii=False, sort_keys=True,
                   separators=(",", ":")).encode("utf-8")).hexdigest()
    duplicate = service.report_result(token, attempt.attempt_id, status="submitted",
                                      summary="完成", usage_units=3, trace_id="trace-2",
                                      **claims(result.attempt))
    assert (duplicate.outcome, duplicate.attempt.version) == ("duplicate", result.attempt.version)
    assert team.store.get_work_item(requirement.project_id,
                                    item.work_item_id).status == "awaiting_owner_review"
    assert stored_reports(team, requirement.project_id, attempt.attempt_id) == [
        ("ack", True, None), ("ack", True, "duplicate ack"), ("start", True, None),
        ("start", True, "duplicate start"), ("lease", True, None),
        ("lease", False, "stale lease epoch"), ("lease", True, None), ("result", True, None),
        ("result", True, "duplicate result")]
    assert audited(team, attempt.attempt_id) == [
        "attempt.assigned", "attempt.ack", "attempt.ack", "attempt.start", "attempt.start",
        "attempt.lease", "attempt.callback_rejected", "attempt.lease", "attempt.result",
        "attempt.result"]


def test_forged_stale_and_revoked_callbacks_leave_evidence_only(team):
    requirement, _plan, item, instance, token, attempt = live_attempt(team, lease_ttl=60)
    service = team.make_execution(lease_ttl=60)
    _foreign, foreign_token = claimed_instance(team, binding=team.other_binding)
    forged = dict(claims(attempt))
    forged["input_hash"] = "b" * 64
    with pytest.raises(Conflict):
        service.ack(token, attempt.attempt_id, **forged)
    with pytest.raises(Conflict):
        service.ack(token, attempt.attempt_id, control_epoch=7, input_hash=attempt.input_hash,
                    lease_epoch=1)
    with pytest.raises(Conflict):
        service.ack(token, attempt.attempt_id, control_epoch=1, input_hash=attempt.input_hash,
                    lease_epoch=9)
    with pytest.raises(NotFound):
        service.ack(foreign_token, attempt.attempt_id, **claims(attempt))
    with pytest.raises(Unauthorized):
        service.ack("y" * 43, attempt.attempt_id, **claims(attempt))
    assert team.store.get_attempt(requirement.project_id, attempt.attempt_id) == attempt
    team.clock.advance(61)
    with pytest.raises(Conflict):
        service.ack(token, attempt.attempt_id, **claims(attempt))
    renewed = service.renew_lease(token, attempt.attempt_id, **claims(attempt))
    assert service.ack(token, attempt.attempt_id, **claims(renewed.attempt)).attempt.status \
        == "acked"
    frozen = team.store.set_attempt_control_epoch(instance.instance_id,
                                                  instance.control_epoch + 1)
    assert frozen == 1
    revoked = team.store.get_attempt(requirement.project_id, attempt.attempt_id)
    with pytest.raises(Conflict):
        service.start(token, attempt.attempt_id, input_hash=revoked.input_hash,
                      control_epoch=revoked.control_epoch, lease_epoch=revoked.lease_epoch)
    team.store.set_disabled(instance.instance_id)
    with pytest.raises(Unauthorized):
        service.held_attempts(token)
    assert team.store.get_work_item(requirement.project_id,
                                    item.work_item_id).status == "running"
    assert stored_reports(team, requirement.project_id, attempt.attempt_id) == [
        ("ack", False, "input digest mismatch"), ("ack", False, "stale control epoch"),
        ("ack", False, "stale lease epoch"), ("ack", False, "lease expired"),
        ("lease", True, None), ("ack", True, None), ("start", False, "execution right revoked")]


def test_result_declares_only_artifacts_the_worker_uploaded(team):
    requirement, _plan, item, _instance, token, attempt = live_attempt(team)
    service = team.make_execution()
    content = "结果说明".encode("utf-8")
    digest = hashlib.sha256(content).hexdigest()
    call = service.put_artifact(token, attempt.attempt_id, name="result.md",
                                media_type="text/markdown", sha256=digest, content=content,
                                **claims(attempt))
    assert (call.outcome, call.artifact.size_bytes) == ("accepted", len(content))
    same = service.put_artifact(token, attempt.attempt_id, name="result.md",
                                media_type="text/markdown", sha256=digest, content=content,
                                **claims(attempt))
    assert (same.outcome, same.artifact.artifact_id) == ("duplicate", call.artifact.artifact_id)
    tampered = "篡改内容".encode("utf-8")
    with pytest.raises(Conflict):
        service.put_artifact(token, attempt.attempt_id, name="result.md",
                             media_type="text/markdown",
                             sha256=hashlib.sha256(tampered).hexdigest(), content=tampered,
                             **claims(attempt))
    with pytest.raises(ValidationError):
        service.put_artifact(token, attempt.attempt_id, name="bad.md", media_type="text/plain",
                             sha256="0" * 64, content=content, **claims(attempt))
    acknowledged = service.ack(token, attempt.attempt_id, **claims(attempt))
    with pytest.raises(Conflict):
        service.report_result(token, attempt.attempt_id, status="submitted", summary="完成",
                              artifacts=({"name": "missing.md", "sha256": digest},),
                              **claims(acknowledged.attempt))
    submitted = service.report_result(token, attempt.attempt_id, status="submitted",
                                      summary="完成", usage_units=2,
                                      artifacts=({"name": "result.md", "sha256": digest},),
                                      **claims(acknowledged.attempt))
    assert submitted.attempt.status == "submitted"
    assert json.loads(submitted.attempt.result_json)["artifacts"] == [
        {"name": "result.md", "sha256": digest}]
    assert [artifact.name for artifact in service.artifacts(token, attempt.attempt_id)] == [
        "result.md"]
    downloaded, artifact = service.artifact_content(token, attempt.attempt_id,
                                                   call.artifact.artifact_id)
    assert (downloaded, artifact.name) == (content, "result.md")
    assert stored_reports(team, requirement.project_id, attempt.attempt_id) == [
        ("artifact", True, None), ("artifact", True, "duplicate artifact"),
        ("artifact", False, "artifact name already used"), ("ack", True, None),
        ("result", False, "declared artifact is missing"), ("result", True, None)]
    assert team.store.get_work_item(requirement.project_id,
                                    item.work_item_id).status == "awaiting_owner_review"


def test_cancel_closes_the_attempt_and_a_late_result_cannot_revive_it(team):
    requirement, _plan, item, _instance, token, attempt = live_attempt(team, lease_ttl=60)
    service = team.make_execution(lease_ttl=60)
    started = service.start(token, attempt.attempt_id, **claims(attempt))
    cancelled = service.cancel(token, attempt.attempt_id, reason="节点停机",
                               **claims(started.attempt))
    assert cancelled.attempt.status == "cancelled"
    assert service.cancel(token, attempt.attempt_id, reason="节点停机",
                          **claims(cancelled.attempt)).outcome == "duplicate"
    assert team.store.get_work_item(requirement.project_id,
                                    item.work_item_id).status == "waiting_agent"
    team.clock.advance(3600)
    with pytest.raises(InvalidState):
        service.report_result(token, attempt.attempt_id, status="submitted",
                              summary="迟到的结果", **claims(cancelled.attempt))
    with pytest.raises(InvalidState):
        service.renew_lease(token, attempt.attempt_id, **claims(cancelled.attempt))
    with pytest.raises(InvalidState):
        service.put_artifact(token, attempt.attempt_id, name="late.md", media_type="text/plain",
                             sha256=hashlib.sha256(b"late").hexdigest(), content=b"late",
                             **claims(cancelled.attempt))
    assert team.store.get_work_item(requirement.project_id,
                                    item.work_item_id).status == "waiting_agent"
    assert stored_reports(team, requirement.project_id, attempt.attempt_id)[-3:] == [
        ("result", False, "attempt is closed"), ("lease", False, "attempt is closed"),
        ("artifact", False, "attempt is closed")]


def test_failed_result_moves_the_work_item_to_failed(team):
    requirement, _plan, item, _instance, token, attempt = live_attempt(team)
    service = team.make_execution()
    started = service.start(token, attempt.attempt_id, **claims(attempt))
    failed = service.report_result(token, attempt.attempt_id, status="failed",
                                   summary="依赖缺失", usage_units=1, **claims(started.attempt))
    assert (failed.attempt.status, failed.attempt.usage_units) == ("failed", 1)
    assert team.store.get_work_item(requirement.project_id,
                                    item.work_item_id).status == "failed"


def test_binding_version_change_blocks_further_callbacks(team):
    requirement, _plan, item, _instance, token, attempt = live_attempt(team)
    service = team.make_execution()
    started = service.start(token, attempt.attempt_id, **claims(attempt))
    team.store.update_binding(requirement.project_id, team.binding.binding_id,
                              duty="实现与复核", persona_version="persona-v1",
                              allowed_tools=("Read", "Edit"), workspace_ref="workspace-a",
                              provider_ref="provider-a", expected_version=team.binding.version)
    with pytest.raises(Conflict):
        service.report_result(token, attempt.attempt_id, status="submitted", summary="完成",
                              **claims(started.attempt))
    assert team.store.get_attempt(requirement.project_id,
                                  attempt.attempt_id).status == "running"
    assert team.store.get_work_item(requirement.project_id,
                                    item.work_item_id).status == "running"
    assert stored_reports(team, requirement.project_id,
                         attempt.attempt_id)[-1] == ("result", False, "binding version changed")


def test_dependency_artifacts_download_only_from_the_frozen_manifest(team):
    requirement = approved_requirement(team)
    plan = published_plan(team, requirement, plan_item(team, "A"), plan_item(team, "C"),
                          plan_item(team, "B", depends_on=("A",)))
    queue = QueueService(team.database, heartbeat_timeout=120, clock=team.clock)
    dispatcher = team.make_dispatch(requires_real_data=False)
    service = team.make_execution()
    instance, token = claimed_instance(team)
    first = work_item(team, plan, "A")
    with pytest.raises(InvalidState):
        dispatcher.dispatch(requirement.project_id, work_item(team, plan, "B").work_item_id)
    attempt = dispatcher.dispatch(requirement.project_id, first.work_item_id)
    content = "上游产物".encode("utf-8")
    digest = hashlib.sha256(content).hexdigest()
    uploaded = service.put_artifact(token, attempt.attempt_id, name="api.md",
                                    media_type="text/markdown", sha256=digest, content=content,
                                    **claims(attempt))
    acknowledged = service.ack(token, attempt.attempt_id, **claims(uploaded.attempt))
    service.report_result(token, attempt.attempt_id, status="submitted", summary="完成",
                          artifacts=({"name": "api.md", "sha256": digest},),
                          **claims(acknowledged.attempt))
    accepted = team.store.advance_attempt(
        attempt.attempt_id, status="accepted",
        expected_version=team.store.get_attempt(requirement.project_id,
                                                attempt.attempt_id).version,
        updated_at=team.clock())
    upstream = team.store.get_work_item(requirement.project_id, first.work_item_id)
    team.store.advance_work_item(first.work_item_id, status="accepted",
                                 expected_version=upstream.version, updated_at=team.clock())
    assert queue.advance(requirement.project_id)["work_items_changed"] == 1

    other_item = work_item(team, plan, "C")
    other = dispatcher.dispatch(requirement.project_id, other_item.work_item_id)
    unrelated = service.put_artifact(token, other.attempt_id, name="other.md",
                                     media_type="text/markdown", sha256=digest, content=content,
                                     **claims(other))
    assert other.instance_id == instance.instance_id
    second = dispatcher.dispatch(requirement.project_id, work_item(team, plan, "B").work_item_id)
    payload, _ = service.package(token, second.attempt_id)
    declared = json.loads(payload)["dependencies"][0]["artifacts"][0]
    assert declared["artifact_id"] == uploaded.artifact.artifact_id
    assert declared["sha256"] == digest
    assert [artifact.name for artifact in service.artifacts(token, attempt.attempt_id)] == ["api.md"]
    assert service.artifacts(token, second.attempt_id) == []
    fetched, artifact = service.artifact_content(token, second.attempt_id,
                                                 declared["artifact_id"])
    assert (fetched, artifact.name) == (content, "api.md")
    with pytest.raises(NotFound):
        service.artifact_content(token, second.attempt_id, unrelated.artifact.artifact_id)
    _foreign, foreign_token = claimed_instance(team, binding=team.other_binding)
    with pytest.raises(NotFound):
        service.artifact_content(foreign_token, second.attempt_id, declared["artifact_id"])
    assert accepted.status == "accepted"


def test_decode_artifact_bounds_the_encoded_body():
    content = "内容".encode("utf-8")
    encoded = base64.b64encode(content).decode("ascii")
    assert decode_artifact(encoded) == content
    for rejected in ("not base64!!", "AAAA\nAAAA", "A" * (4 * ((32 * 1024 * 1024 + 2) // 3) + 9)):
        with pytest.raises(ValidationError):
            decode_artifact(rejected)
