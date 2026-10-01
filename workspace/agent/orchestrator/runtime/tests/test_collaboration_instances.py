"""Execution-node, instance lifecycle and admission tests on a real SQLite database."""
from __future__ import annotations

import hashlib
import sqlite3
import sys
import time
from contextlib import closing, contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from collaboration import database as database_module
from collaboration.__main__ import main as cli_main
from collaboration.auth import Access, Unauthorized
from collaboration.database import Database, SCHEMA_VERSION
from collaboration.instances import AdmissionDenied, InstanceService
from collaboration.models import Conflict, InvalidState, NotFound, SchemaVersionError, ValidationError
from collaboration.store import CollaborationStore


@pytest.fixture
def team(tmp_path):
    database = Database(tmp_path / "collab.sqlite3")
    database.initialize()
    store = CollaborationStore(database)
    service = InstanceService(database)
    owner = store.create_user("甲")
    member = store.create_user("乙")
    project_a = store.create_project("项目 A", owner.user_id)
    project_b = store.create_project("项目 B", owner.user_id)
    store.add_member(project_a.project_id, member.user_id)
    store.add_member(project_b.project_id, member.user_id, access_role="reviewer")
    agent = store.create_agent(member.user_id, "乙的 Agent")
    binding_a = store.create_binding(
        project_a.project_id, agent.agent_id, member.user_id, duty="前端",
        persona_version="frontend-v1", allowed_tools=("Read", "Edit"),
        workspace_ref="workspace-a", provider_ref="provider-a")
    binding_b = store.create_binding(
        project_b.project_id, agent.agent_id, member.user_id, duty="后端",
        persona_version="backend-v1", allowed_tools=("Read",),
        workspace_ref="workspace-a", provider_ref="provider-a")
    node, node_token = service.enroll_node("node-1")
    return SimpleNamespace(database=database, store=store, service=service, owner=owner,
                           member=member, project_a=project_a, project_b=project_b,
                           agent=agent, binding_a=binding_a, binding_b=binding_b,
                           node=node, node_token=node_token)


@contextmanager
def access_for(team, user):
    with team.database.transaction() as conn:
        store = CollaborationStore(team.database, connection=conn)
        yield Access(store.get_user(user.user_id), False, "", 0.0, store, conn)


def register(team, *, binding=None, node=None, user=None):
    binding = binding or team.binding_a
    with access_for(team, user or team.member) as access:
        return team.service.register_slot(
            access, project_id=binding.project_id, binding_id=binding.binding_id,
            node_id=(node or team.node).node_id)


def claim(team, slot, claim_token, *, node_token=None, **overrides):
    fields = dict(protocol_version="1", capacity=4, isolation_mode="none",
                  isolation_attestation="", workspace_ref="workspace-a",
                  provider_ref="provider-a", allowed_tools=["Read"],
                  env_allowlist=["PYTHONPATH"])
    fields.update(overrides)
    return team.service.claim(node_token if node_token is not None else team.node_token,
                              claim_token, **fields)


def active_instance(team, *, binding=None, **overrides):
    slot, claim_token = register(team, binding=binding)
    return claim(team, slot, claim_token, **overrides)


def disabled_reason(team, *args, **kwargs):
    with pytest.raises(AdmissionDenied) as denial:
        team.service.admit(*args, **kwargs)
    assert isinstance(denial.value, InvalidState)
    return denial.value.reason


def test_enroll_node_stores_only_digest_and_audits_without_actor(team):
    node, token = team.service.enroll_node("node-2")
    assert node.status == "active" and node.version == 1
    assert len(token) == 43 and token not in node.__repr__()
    with team.database.read() as conn:
        row = conn.execute("SELECT * FROM nodes WHERE node_id=?", (node.node_id,)).fetchone()
        assert row["credential_hash"] == hashlib.sha256(token.encode()).hexdigest()
        events = conn.execute(
            "SELECT actor_id FROM collaboration_audit WHERE action='node.enrolled'").fetchall()
    assert len(events) == 2 and all(event["actor_id"] is None for event in events)
    assert token.encode() not in Path(team.database.path).read_bytes()


def test_service_configuration_bounds(tmp_path):
    database = Database(tmp_path / "bounds.sqlite3")
    database.initialize()
    for kwargs in ({"claim_ttl": 59}, {"claim_ttl": 604801}, {"claim_ttl": True},
                   {"heartbeat_timeout": 29}, {"heartbeat_timeout": 3601}):
        with pytest.raises(ValidationError):
            InstanceService(database, **kwargs)


def test_register_slot_requires_active_project_scoped_binding_and_node(team):
    with access_for(team, team.member) as access:
        with pytest.raises(NotFound):
            team.service.register_slot(access, project_id=team.project_a.project_id,
                                       binding_id="missing", node_id=team.node.node_id)
        with pytest.raises(NotFound):
            team.service.register_slot(access, project_id=team.project_a.project_id,
                                       binding_id=team.binding_a.binding_id, node_id="missing")
        with pytest.raises(NotFound):
            team.service.register_slot(access, project_id=team.project_b.project_id,
                                       binding_id=team.binding_a.binding_id,
                                       node_id=team.node.node_id)
    revoked = team.store.revoke_binding(team.project_a.project_id, team.binding_a.binding_id,
                                        expected_version=team.binding_a.version)
    with access_for(team, team.member) as access:
        with pytest.raises(InvalidState):
            team.service.register_slot(access, project_id=team.project_a.project_id,
                                       binding_id=revoked.binding_id, node_id=team.node.node_id)
    team.service.disable_node(team.node.node_id)
    with access_for(team, team.member) as access:
        with pytest.raises(InvalidState):
            team.service.register_slot(access, project_id=team.project_b.project_id,
                                       binding_id=team.binding_b.binding_id,
                                       node_id=team.node.node_id)
    with pytest.raises(InvalidState):
        team.service.disable_node(team.node.node_id)


def test_duplicate_live_slot_conflicts_until_disabled(team):
    slot, _ = register(team)
    with pytest.raises(Conflict):
        register(team)
    with access_for(team, team.member) as access:
        team.service.disable(access, project_id=team.project_a.project_id,
                             binding_id=team.binding_a.binding_id,
                             instance_id=slot.instance_id, expected_version=slot.version)
    replacement, _ = register(team)
    assert replacement.status == "pending" and replacement.version == 1


def test_claim_rotates_tokens_single_use_and_bound_to_node(team):
    slot, claim_token = register(team)
    node2, node2_token = team.service.enroll_node("node-2")
    with pytest.raises(Conflict):
        claim(team, slot, claim_token, node_token=node2_token)
    with pytest.raises(Conflict):
        claim(team, slot, "malformed")
    with pytest.raises(Unauthorized):
        claim(team, slot, claim_token, node_token="malformed")
    instance, instance_token, binding = claim(team, slot, claim_token)
    assert instance.status == "active" and instance.version == 2
    assert binding.binding_id == team.binding_a.binding_id
    assert len(instance_token) == 43 and instance_token != claim_token
    with team.database.read() as conn:
        row = conn.execute("SELECT * FROM execution_instances WHERE instance_id=?",
                           (instance.instance_id,)).fetchone()
        assert row["claim_token_hash"] == hashlib.sha256(claim_token.encode()).hexdigest()
        assert row["instance_token_hash"] == hashlib.sha256(instance_token.encode()).hexdigest()
    with pytest.raises(Conflict):
        claim(team, slot, claim_token)
    data = Path(team.database.path).read_bytes()
    for secret in (claim_token, instance_token, team.node_token, node2_token):
        assert secret.encode() not in data


@pytest.mark.parametrize("overrides", [
    {"protocol_version": "2"},
    {"capacity": 0},
    {"capacity": 65},
    {"isolation_mode": "docker"},
    {"isolation_mode": "container"},
    {"isolation_mode": "none", "isolation_attestation": "claimed anyway"},
    {"isolation_attestation": "x" * 1025},
    {"allowed_tools": ["Read", "Write"]},
    {"allowed_tools": ["Read", "Read"]},
    {"allowed_tools": [""]},
    {"allowed_tools": ["x" * 129]},
    {"allowed_tools": ["Read"] * 257},
    {"env_allowlist": ["lowercase"]},
    {"env_allowlist": ["MY_TOKEN"]},
    {"env_allowlist": ["OPENAI_API_KEY"]},
    {"env_allowlist": ["AWS_REGION"]},
    {"env_allowlist": ["A"] * 65},
    {"workspace_ref": "../escape"},
    {"workspace_ref": "workspace-b"},
    {"provider_ref": "provider-b"},
])
def test_claim_rejects_invalid_or_mismatched_machine_inputs(team, overrides):
    slot, claim_token = register(team)
    with pytest.raises(ValidationError):
        claim(team, slot, claim_token, **overrides)
    current = team.store.get_instance(team.project_a.project_id, slot.instance_id)
    assert current.status == "pending" and current.version == 1
    instance, _, _ = claim(team, slot, claim_token)
    assert instance.status == "active"


def test_claim_rejects_revoked_binding_and_expired_slot(team):
    slot, claim_token = register(team)
    team.store.revoke_binding(team.project_a.project_id, team.binding_a.binding_id,
                              expected_version=team.binding_a.version)
    with pytest.raises(Conflict):
        claim(team, slot, claim_token)
    late_slot, late_token = register(team, binding=team.binding_b)
    late = InstanceService(team.database, clock=lambda: time.time() + 2 * 86400)
    with pytest.raises(Conflict):
        late.claim(team.node_token, late_token, protocol_version="1", capacity=1,
                   isolation_mode="none", isolation_attestation="",
                   workspace_ref="workspace-a", provider_ref="provider-a",
                   allowed_tools=["Read"], env_allowlist=[])
    instance, _, _ = claim(team, late_slot, late_token)
    assert instance.status == "active"


def test_heartbeat_and_describe_require_live_identity(team):
    instance, instance_token, _ = active_instance(team, isolation_mode="container",
                                                  isolation_attestation="bubblewrap")
    beat = team.service.heartbeat(instance_token)
    assert beat.status == "active" and beat.heartbeat_at is not None
    described, binding = team.service.describe(instance_token)
    assert described.instance_id == instance.instance_id
    assert binding.binding_id == team.binding_a.binding_id
    for token in (None, "not-a-token", "x" * 43):
        with pytest.raises(Unauthorized):
            team.service.heartbeat(token)
        with pytest.raises(Unauthorized):
            team.service.describe(token)
    with access_for(team, team.owner) as access:
        team.service.disable(access, project_id=team.project_a.project_id,
                             binding_id=team.binding_a.binding_id,
                             instance_id=instance.instance_id, expected_version=instance.version)
    with pytest.raises(Unauthorized):
        team.service.heartbeat(instance_token)
    with pytest.raises(Unauthorized):
        team.service.describe(instance_token)


def test_disable_node_cascades_to_live_instances_with_project_audit(team):
    claimed, claimed_token, _ = active_instance(team)
    pending, _ = register(team, binding=team.binding_b)
    node, disabled = team.service.disable_node(team.node.node_id)
    assert node.status == "disabled"
    assert {item.instance_id for item in disabled} == {claimed.instance_id, pending.instance_id}
    with pytest.raises(Unauthorized):
        team.service.heartbeat(claimed_token)
    with team.database.read() as conn:
        rows = conn.execute(
            "SELECT project_id FROM collaboration_audit WHERE action='instance.disabled' "
            "ORDER BY project_id").fetchall()
    assert [row["project_id"] for row in rows] == sorted(
        [team.project_a.project_id, team.project_b.project_id])


def test_verify_requires_claimed_instance_with_declared_isolation(team):
    slot, claim_token = register(team)
    with access_for(team, team.owner) as access:
        with pytest.raises(InvalidState):
            team.service.verify(access, project_id=team.project_a.project_id,
                                binding_id=team.binding_a.binding_id,
                                instance_id=slot.instance_id, expected_version=1)
    instance, _, _ = claim(team, slot, claim_token, isolation_mode="os_account",
                           isolation_attestation="dedicated Windows account")
    with access_for(team, team.owner) as access:
        with pytest.raises(NotFound):
            team.service.verify(access, project_id=team.project_a.project_id,
                                binding_id=team.binding_b.binding_id,
                                instance_id=instance.instance_id,
                                expected_version=instance.version)
        with pytest.raises(Conflict):
            team.service.verify(access, project_id=team.project_a.project_id,
                                binding_id=team.binding_a.binding_id,
                                instance_id=instance.instance_id,
                                expected_version=instance.version + 1)
        verified = team.service.verify(access, project_id=team.project_a.project_id,
                                       binding_id=team.binding_a.binding_id,
                                       instance_id=instance.instance_id,
                                       expected_version=instance.version)
        assert verified.isolation_verified == 1 and verified.verified_at is not None
        with pytest.raises(InvalidState):
            team.service.verify(access, project_id=team.project_a.project_id,
                                binding_id=team.binding_a.binding_id,
                                instance_id=instance.instance_id,
                                expected_version=verified.version)
    none_slot, none_token = register(team, binding=team.binding_b)
    none_instance, _, _ = claim(team, none_slot, none_token)
    with access_for(team, team.owner) as access:
        with pytest.raises(InvalidState):
            team.service.verify(access, project_id=team.project_b.project_id,
                                binding_id=team.binding_b.binding_id,
                                instance_id=none_instance.instance_id,
                                expected_version=none_instance.version)


def test_instance_lifecycle_is_project_scoped(team):
    instance, _, _ = active_instance(team)
    with access_for(team, team.owner) as access:
        for call in (
            lambda: team.service.verify(access, project_id=team.project_b.project_id,
                                        binding_id=team.binding_b.binding_id,
                                        instance_id=instance.instance_id,
                                        expected_version=instance.version),
            lambda: team.service.disable(access, project_id=team.project_b.project_id,
                                         binding_id=team.binding_b.binding_id,
                                         instance_id=instance.instance_id),
            lambda: team.service.verify(access, project_id=team.project_a.project_id,
                                        binding_id=team.binding_b.binding_id,
                                        instance_id=instance.instance_id,
                                        expected_version=instance.version),
        ):
            with pytest.raises(NotFound):
                call()


def test_disable_for_binding_touches_only_that_binding(team):
    instance, token, _ = active_instance(team)
    pending, _ = register(team, binding=team.binding_b)
    with access_for(team, team.owner) as access:
        disabled = team.service.disable_for_binding(
            access, project_id=team.project_a.project_id,
            binding_id=team.binding_a.binding_id)
    assert [item.instance_id for item in disabled] == [instance.instance_id]
    with pytest.raises(Unauthorized):
        team.service.heartbeat(token)
    assert team.store.get_instance(team.project_b.project_id, pending.instance_id).status == "pending"


def test_admit_requires_verified_real_isolation_and_matching_policy(team):
    slot, claim_token = register(team)
    assert disabled_reason(team, team.project_a.project_id, team.binding_a.binding_id,
                           slot.instance_id, requires_real_data=False) == "instance_inactive"
    instance, _, _ = claim(team, slot, claim_token, isolation_mode="container",
                           isolation_attestation="podman sandbox", allowed_tools=["Read", "Edit"])
    assert disabled_reason(team, team.project_a.project_id, team.binding_a.binding_id,
                           instance.instance_id, requires_real_data=True) == "isolation_unverified"
    test_only = team.service.admit(team.project_a.project_id, team.binding_a.binding_id,
                                   instance.instance_id, requires_real_data=False)
    assert test_only.real_data is False
    with access_for(team, team.owner) as access:
        team.service.verify(access, project_id=team.project_a.project_id,
                            binding_id=team.binding_a.binding_id,
                            instance_id=instance.instance_id,
                            expected_version=instance.version)
    admission = team.service.admit(team.project_a.project_id, team.binding_a.binding_id,
                                   instance.instance_id, requires_real_data=True,
                                   expected_binding_version=team.binding_a.version)
    assert admission.instance.instance_id == instance.instance_id and admission.real_data is True
    with pytest.raises(Conflict):
        team.service.admit(team.project_a.project_id, team.binding_a.binding_id,
                           instance.instance_id, requires_real_data=True,
                           expected_binding_version=99)
    with pytest.raises(NotFound):
        team.service.admit(team.project_b.project_id, team.binding_b.binding_id,
                           instance.instance_id, requires_real_data=False)
    with pytest.raises(NotFound):
        team.service.admit(team.project_a.project_id, team.binding_b.binding_id,
                           instance.instance_id, requires_real_data=False)


def test_admit_denies_when_binding_or_identity_or_node_changes(team):
    instance, _, _ = active_instance(team, isolation_mode="container",
                                     isolation_attestation="podman sandbox")
    updated = team.store.update_binding(
        team.project_a.project_id, team.binding_a.binding_id,
        expected_version=team.binding_a.version, duty="前端", persona_version="frontend-v2",
        allowed_tools=("Read", "Edit"), workspace_ref="workspace-c", provider_ref="provider-a")
    assert disabled_reason(team, team.project_a.project_id, team.binding_a.binding_id,
                           instance.instance_id, requires_real_data=False) == "policy_mismatch"
    team.store.revoke_binding(team.project_a.project_id, team.binding_a.binding_id,
                              expected_version=updated.version)
    assert disabled_reason(team, team.project_a.project_id, team.binding_a.binding_id,
                           instance.instance_id, requires_real_data=False) == "binding_inactive"
    fresh = active_instance(team, binding=team.binding_b, isolation_mode="container",
                            isolation_attestation="podman sandbox")
    team.store.disable_node(team.node.node_id)
    assert disabled_reason(team, team.project_b.project_id, team.binding_b.binding_id,
                           fresh[0].instance_id, requires_real_data=False) == "node_disabled"


def test_admit_enforces_heartbeat_boundary_and_renews_on_heartbeat(team):
    instance, instance_token, _ = active_instance(team, isolation_mode="os_account",
                                                  isolation_attestation="runas profile")
    at_boundary = InstanceService(team.database, heartbeat_timeout=120,
                                  clock=lambda: instance.heartbeat_at + 120)
    admitted = at_boundary.admit(team.project_a.project_id, team.binding_a.binding_id,
                                 instance.instance_id, requires_real_data=False)
    assert admitted.instance.instance_id == instance.instance_id
    stale = InstanceService(team.database, heartbeat_timeout=120,
                            clock=lambda: instance.heartbeat_at + 121)
    with pytest.raises(AdmissionDenied) as denial:
        stale.admit(team.project_a.project_id, team.binding_a.binding_id,
                    instance.instance_id, requires_real_data=False)
    assert denial.value.reason == "heartbeat_stale"
    team.service.heartbeat(instance_token)
    renewed = team.store.get_instance(team.project_a.project_id, instance.instance_id)
    near = InstanceService(team.database, heartbeat_timeout=120,
                           clock=lambda: renewed.heartbeat_at + 120)
    assert near.admit(team.project_a.project_id, team.binding_a.binding_id,
                      instance.instance_id, requires_real_data=False).instance.instance_id \
        == instance.instance_id


def test_audit_records_lifecycle_without_credentials_or_heartbeats(team):
    slot, claim_token = register(team)
    instance, instance_token, _ = claim(team, slot, claim_token,
                                        isolation_mode="container",
                                        isolation_attestation="podman sandbox")
    with team.database.read() as conn:
        before = conn.execute("SELECT count(*) FROM collaboration_audit").fetchone()[0]
    team.service.heartbeat(instance_token)
    with team.database.read() as conn:
        assert conn.execute("SELECT count(*) FROM collaboration_audit").fetchone()[0] == before
        events = [dict(row) for row in conn.execute("SELECT * FROM collaboration_audit")]
    actions = {event["action"] for event in events}
    assert {"node.enrolled", "instance.registered", "instance.claimed"} <= actions
    blob = str(events)
    for secret in (team.node_token, claim_token, instance_token):
        assert secret not in blob
        assert secret.encode() not in Path(team.database.path).read_bytes()


def make_v2_database(path):
    with closing(sqlite3.connect(path, isolation_level=None)) as conn:
        conn.execute("BEGIN IMMEDIATE")
        for statement in database_module._SCHEMA_STATEMENTS:
            conn.execute(statement)
        for statement in database_module._AUTH_SCHEMA:
            conn.execute(statement)
        conn.execute("INSERT INTO users(user_id,display_name) VALUES('old-user','preserved')")
        conn.execute("PRAGMA user_version=2")
        conn.commit()


def test_v2_database_upgrades_to_v3_without_touching_identity(tmp_path):
    path = tmp_path / "v2.sqlite3"
    make_v2_database(path)
    database = Database(path)
    with pytest.raises(SchemaVersionError):
        with database.read():
            pytest.fail("Migration must be explicit")
    database.initialize()
    assert CollaborationStore(database).get_user("old-user").display_name == "preserved"
    with database.read() as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    node, _ = InstanceService(database).enroll_node("node")
    assert node.status == "active"


def test_v2_migration_failure_rolls_back(tmp_path, monkeypatch):
    path = tmp_path / "v2.sqlite3"
    make_v2_database(path)
    before = path.read_bytes()
    with monkeypatch.context() as patch:
        patch.setattr(database_module, "_INSTANCE_SCHEMA",
                      database_module._INSTANCE_SCHEMA[:1] + ("INVALID SQL",))
        with pytest.raises(sqlite3.OperationalError):
            Database(path).initialize()
    assert path.read_bytes() == before
    Database(path).initialize()
    assert CollaborationStore(Database(path)).get_user("old-user").version == 1


def test_cli_enrolls_and_disables_nodes_without_echoing_secrets(tmp_path, capsys):
    data_dir = tmp_path / "service-data"
    assert cli_main(["node-enroll", "--data-dir", str(data_dir), "--name", "node-1"]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert len(lines) == 2
    node_id, token = lines
    assert len(node_id) == 32 and len(token) == 43
    with Database(data_dir / "metadata.sqlite3").read() as conn:
        row = conn.execute("SELECT * FROM nodes WHERE node_id=?", (node_id,)).fetchone()
    assert row["credential_hash"] == hashlib.sha256(token.encode()).hexdigest()
    assert cli_main(["node-disable", "--data-dir", str(data_dir), "--node-id", node_id]) == 0
    assert capsys.readouterr().out.strip() == node_id
    assert capsys.readouterr().err == ""
    with pytest.raises(SystemExit):
        cli_main(["node-disable", "--data-dir", str(data_dir), "--node-id", "missing"])
    assert "credential" not in capsys.readouterr().err
