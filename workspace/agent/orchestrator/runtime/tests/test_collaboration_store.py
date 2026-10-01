from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from dataclasses import FrozenInstanceError, asdict
from pathlib import Path
from threading import Barrier

import pytest

RUNTIME = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RUNTIME))

from collaboration.database import Database, SCHEMA_VERSION
from collaboration.models import Conflict, InvalidState, NotFound, SchemaVersionError, ValidationError
from collaboration.store import CollaborationStore


@pytest.fixture
def store(tmp_path):
    database = Database(tmp_path / "collaboration" / "metadata.sqlite3")
    database.initialize()
    return CollaborationStore(database)


@pytest.fixture
def identities(store):
    owner = store.create_user("甲")
    member = store.create_user("乙")
    a = store.create_project("项目 A", owner.user_id)
    b = store.create_project("项目 B", owner.user_id)
    store.add_member(a.project_id, member.user_id)
    store.add_member(b.project_id, member.user_id, access_role="reviewer")
    agent = store.create_agent(member.user_id, "乙的 Agent")
    return owner, member, a, b, agent


def bind(store, identities, *, project=None, **overrides):
    _, member, a, _, agent = identities
    fields = dict(duty="前端", persona_version="frontend-v1", allowed_tools=("Read",),
                  workspace_ref="workspace-a", provider_ref="provider-a")
    fields.update(overrides)
    return store.create_binding((project or a).project_id, agent.agent_id, member.user_id, **fields)


def edit_binding(store, binding, **overrides):
    fields = {name: getattr(binding, name) for name in (
        "duty", "persona_version", "allowed_tools", "workspace_ref", "provider_ref")}
    fields.update(overrides)
    return store.update_binding(binding.project_id, binding.binding_id,
                                expected_version=binding.version, **fields)


def test_database_explicit_initialization_and_reopen(tmp_path):
    path = tmp_path / "new" / "metadata.sqlite3"
    db = Database(path)
    assert not path.exists()
    db.initialize()
    store = CollaborationStore(db)
    user = store.create_user("甲")
    Database(path).initialize()
    assert CollaborationStore(Database(path)).get_user(user.user_id) == user
    with db.read() as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("DELETE FROM users")


def test_transaction_rolls_back_and_closes(store):
    with pytest.raises(RuntimeError):
        with store.database.transaction() as conn:
            conn.execute("INSERT INTO users(user_id,display_name) VALUES('rollback','甲')")
            raise RuntimeError("injected failure")
    with pytest.raises(NotFound):
        store.get_user("rollback")
    with pytest.raises(sqlite3.ProgrammingError):
        conn.execute("SELECT 1")
    assert store.create_user("after failure").version == 1


def test_commit_failure_rolls_back_deferred_owner_constraint(store):
    user = store.create_user("甲")
    with pytest.raises(sqlite3.IntegrityError):
        with store.database.transaction() as conn:
            conn.execute("INSERT INTO projects(project_id,name,owner_id) VALUES('orphan','P',?)", (user.user_id,))
    with pytest.raises(NotFound):
        store.get_project("orphan")
    assert store.create_project("valid", user.user_id).owner_id == user.user_id


def test_future_schema_is_rejected_without_modification(tmp_path):
    path = tmp_path / "future.sqlite3"
    with closing(sqlite3.connect(path)) as conn, conn:
        conn.execute("PRAGMA user_version=999")
        conn.execute("CREATE TABLE future_data(value TEXT)")
        conn.execute("INSERT INTO future_data VALUES('preserve')")
    before = path.read_bytes()
    with pytest.raises(SchemaVersionError):
        Database(path).initialize()
    assert path.read_bytes() == before


@pytest.mark.parametrize("statement", [
    "CREATE TABLE old_data(value TEXT)",
    "CREATE VIEW legacy_view AS SELECT 1",
])
def test_unversioned_existing_database_is_not_adopted(tmp_path, statement):
    path = tmp_path / "existing.sqlite3"
    with closing(sqlite3.connect(path)) as conn, conn:
        conn.execute(statement)
    before = path.read_bytes()
    with pytest.raises(SchemaVersionError):
        Database(path).initialize()
    assert path.read_bytes() == before


def test_live_store_rejects_newer_schema(store):
    with store.database.transaction() as conn:
        conn.execute("PRAGMA user_version=999")
    with pytest.raises(SchemaVersionError):
        store.create_user("should not write")
    with pytest.raises(SchemaVersionError):
        store.get_user("unknown")


def test_project_owner_is_created_atomically(store):
    owner = store.create_user("甲")
    project = store.create_project("项目", owner.user_id)
    member = store.get_member(project.project_id, owner.user_id)
    assert (member.access_role, member.status, member.version) == ("owner", "active", 1)
    with pytest.raises(NotFound):
        store.create_project("invalid", "unknown")
    assert store.list_projects(owner.user_id) == [project]


def test_two_projects_keep_distinct_identities(store, identities):
    _, member, a, b, agent = identities
    first = bind(store, identities)
    second = bind(store, identities, project=b, duty="测试", persona_version="tester-v2",
                  allowed_tools=("Read", "Test"), workspace_ref="workspace-b", provider_ref="provider-b")
    assert first.agent_id == second.agent_id == agent.agent_id
    assert first.binding_id != second.binding_id
    assert store.get_member(a.project_id, member.user_id).access_role == "member"
    assert store.get_member(b.project_id, member.user_id).access_role == "reviewer"
    assert store.list_bindings(a.project_id) == [first]
    assert store.list_bindings(b.project_id) == [second]
    assert store.validate_binding(a.project_id, first.binding_id, expected_version=1) == first
    with pytest.raises(FrozenInstanceError):
        first.duty = "mutated"


def test_cross_project_ids_are_not_visible(store, identities):
    _, _, _, b, _ = identities
    binding = bind(store, identities)
    with pytest.raises(NotFound):
        store.get_binding(b.project_id, binding.binding_id)
    with pytest.raises(NotFound):
        store.get_binding_revision(b.project_id, binding.binding_id, 1)
    with pytest.raises(NotFound):
        store.revoke_binding(b.project_id, binding.binding_id, expected_version=1)
    with pytest.raises(NotFound):
        store.validate_binding(b.project_id, binding.binding_id, expected_version=1)
    assert store.get_binding(binding.project_id, binding.binding_id) == binding


def test_owner_member_mismatch_is_rejected(store, identities):
    owner, _, a, _, agent = identities
    with pytest.raises(ValidationError):
        store.create_binding(a.project_id, agent.agent_id, owner.user_id, duty="test",
                             persona_version="v1", allowed_tools=(), workspace_ref="w", provider_ref="p")
    assert store.list_bindings(a.project_id) == []


def test_nonmember_binding_is_rejected(store, identities):
    owner, member, _, _, agent = identities
    project = store.create_project("private", owner.user_id)
    with pytest.raises(NotFound):
        store.create_binding(project.project_id, agent.agent_id, member.user_id, duty="test",
                             persona_version="v1", allowed_tools=(), workspace_ref="w", provider_ref="p")


@pytest.mark.parametrize("mismatch", ["owner", "project"])
def test_database_foreign_keys_reject_mismatched_binding(store, identities, mismatch):
    owner, member, a, _, agent = identities
    project_id = a.project_id
    member_id = owner.user_id
    if mismatch == "project":
        project_id = store.create_project("no member", owner.user_id).project_id
        member_id = member.user_id
    with pytest.raises(sqlite3.IntegrityError):
        with store.database.transaction() as conn:
            conn.execute(
                "INSERT INTO agent_bindings(binding_id,project_id,agent_id,member_user_id,duty,"
                "persona_version,allowed_tools,workspace_ref,provider_ref) VALUES(?,?,?,?,?,?,?,?,?)",
                ("bad", project_id, agent.agent_id, member_id, "duty", "v1", "[]", "w", "p"))


def test_single_active_binding_and_immutable_revision_history(store, identities):
    original = bind(store, identities)
    with pytest.raises(Conflict):
        bind(store, identities)
    updated = edit_binding(store, original, duty="后端", persona_version="backend-v2")
    assert updated.version == 2
    assert store.get_binding_revision(original.project_id, original.binding_id, 1) == original
    assert store.get_binding_revision(original.project_id, original.binding_id, 2) == updated
    with pytest.raises(Conflict):
        edit_binding(store, original, duty="stale")
    with pytest.raises(Conflict):
        store.validate_binding(original.project_id, original.binding_id, expected_version=1)
    revoked = store.revoke_binding(updated.project_id, updated.binding_id, expected_version=2)
    assert revoked.status == "revoked" and revoked.version == 3
    assert store.get_binding_revision(revoked.project_id, revoked.binding_id, 3) == revoked
    with pytest.raises(InvalidState):
        edit_binding(store, revoked)
    replacement = bind(store, identities)
    assert replacement.binding_id != original.binding_id
    with store.database.read() as conn:
        assert conn.execute("SELECT count(*) FROM binding_revisions").fetchone()[0] == 4


@pytest.mark.parametrize("sql", [
    "UPDATE binding_revisions SET snapshot='{}'",
    "DELETE FROM binding_revisions",
])
def test_database_revision_mutation_is_rejected(store, identities, sql):
    binding = bind(store, identities)
    with pytest.raises(sqlite3.IntegrityError):
        with store.database.transaction() as conn:
            conn.execute(sql)
    assert store.get_binding_revision(binding.project_id, binding.binding_id, 1) == binding


def test_database_revision_replace_is_rejected_without_connection_pragmas(store, identities):
    binding = bind(store, identities)
    with closing(sqlite3.connect(store.database.path)) as conn, conn:
        conn.execute("PRAGMA recursive_triggers=OFF")
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("INSERT OR REPLACE INTO binding_revisions(binding_id,version,snapshot) VALUES(?,?,?)",
                         (binding.binding_id, 1, "{}"))
    assert store.get_binding_revision(binding.project_id, binding.binding_id, 1) == binding


def test_read_context_keeps_consistent_snapshot(store):
    with closing(sqlite3.connect(store.database.path)) as conn, conn:
        conn.execute("PRAGMA journal_mode=WAL")
    user = store.create_user("before")
    with store.database.read() as reader:
        assert reader.execute("SELECT display_name FROM users WHERE user_id=?", (user.user_id,)).fetchone()[0] == "before"
        store.update_user(user.user_id, display_name="after", status="active", expected_version=1)
        assert reader.execute("SELECT display_name FROM users WHERE user_id=?", (user.user_id,)).fetchone()[0] == "before"
    assert store.get_user(user.user_id).display_name == "after"


def test_schema_creation_failure_is_atomic(tmp_path, monkeypatch):
    import collaboration.database as module
    path = tmp_path / "failed.sqlite3"
    statements = module._SCHEMA_STATEMENTS
    with monkeypatch.context() as patch:
        patch.setattr(module, "_SCHEMA_STATEMENTS", statements[:2] + ("INVALID SQL",))
        with pytest.raises(sqlite3.OperationalError):
            Database(path).initialize()
    with closing(sqlite3.connect(path)) as conn, conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 0
        assert conn.execute("SELECT count(*) FROM sqlite_master").fetchone()[0] == 0
    Database(path).initialize()
    assert CollaborationStore(Database(path)).create_user("ready").version == 1


def test_snapshot_failure_rolls_back_binding_change(store, identities, monkeypatch):
    binding = bind(store, identities)
    def fail(conn, record):
        raise RuntimeError("snapshot disk error")
    monkeypatch.setattr(store, "_snapshot", fail)
    with pytest.raises(RuntimeError):
        edit_binding(store, binding, duty="should rollback")
    assert store.get_binding(binding.project_id, binding.binding_id) == binding


def test_removal_revokes_only_matching_project_and_rejoin_does_not_restore(store, identities):
    _, member, a, b, _ = identities
    first = bind(store, identities)
    second = bind(store, identities, project=b)
    removed = store.update_member(a.project_id, member.user_id, access_role="member",
                                  status="removed", expected_version=1)
    assert removed.version == 2
    revoked = store.get_binding(a.project_id, first.binding_id)
    assert revoked.status == "revoked"
    assert revoked.version == 2
    assert store.get_binding_revision(a.project_id, first.binding_id, 1) == first
    assert store.validate_binding(b.project_id, second.binding_id, expected_version=1) == second
    assert store.list_projects(member.user_id) == [b]
    store.update_member(a.project_id, member.user_id, access_role="member", status="active", expected_version=2)
    with pytest.raises(InvalidState):
        store.validate_binding(a.project_id, first.binding_id, expected_version=2)
    assert bind(store, identities).binding_id != first.binding_id


def test_removal_and_revocation_rollback_together(store, identities, monkeypatch):
    _, member, a, _, _ = identities
    original = bind(store, identities)
    def fail(conn, record):
        raise RuntimeError("snapshot failed")
    monkeypatch.setattr(store, "_snapshot", fail)
    with pytest.raises(RuntimeError):
        store.update_member(a.project_id, member.user_id, access_role="member",
                            status="removed", expected_version=1)
    assert store.get_member(a.project_id, member.user_id).status == "active"
    assert store.get_binding(a.project_id, original.binding_id) == original


@pytest.mark.parametrize("access_role,status", [("member", "active"), ("owner", "removed")])
def test_owner_cannot_be_demoted_or_removed(store, identities, access_role, status):
    owner, _, a, _, _ = identities
    with pytest.raises(ValidationError):
        store.update_member(a.project_id, owner.user_id, access_role=access_role,
                            status=status, expected_version=1)
    assert store.get_member(a.project_id, owner.user_id).version == 1


def test_cannot_promote_second_owner(store, identities):
    _, member, a, _, _ = identities
    with pytest.raises(ValidationError):
        store.update_member(a.project_id, member.user_id, access_role="owner", status="active", expected_version=1)
    with pytest.raises(Conflict):
        store.add_member(a.project_id, member.user_id)


@pytest.mark.parametrize("disable", ["user", "agent", "project"])
def test_disabling_identity_revokes_bindings(store, identities, disable):
    _, member, a, b, agent = identities
    first = bind(store, identities)
    second = bind(store, identities, project=b)
    if disable == "user":
        store.update_user(member.user_id, display_name=member.display_name, status="disabled", expected_version=1)
    elif disable == "agent":
        store.update_agent(agent.agent_id, member.user_id, name=agent.name, status="disabled", expected_version=1)
    else:
        store.update_project(a.project_id, name=a.name, status="archived", expected_version=1)
    with pytest.raises(InvalidState):
        store.validate_binding(a.project_id, first.binding_id, expected_version=2)
    with pytest.raises(InvalidState):
        bind(store, identities)
    expected_status = "active" if disable == "project" else "revoked"
    assert store.get_binding(b.project_id, second.binding_id).status == expected_status


@pytest.mark.parametrize("target", ["user", "agent"])
def test_reenabling_identity_does_not_revive_binding(store, identities, target):
    _, member, a, _, agent = identities
    original = bind(store, identities)
    for version, status in ((1, "disabled"), (2, "active")):
        if target == "user":
            store.update_user(member.user_id, display_name=member.display_name,
                              status=status, expected_version=version)
        else:
            store.update_agent(agent.agent_id, member.user_id, name=agent.name,
                               status=status, expected_version=version)
    binding = store.get_binding(a.project_id, original.binding_id)
    assert (binding.status, binding.version) == ("revoked", 2)
    assert store.get_binding_revision(a.project_id, original.binding_id, 1) == original
    assert store.get_binding_revision(a.project_id, original.binding_id, 2) == binding
    with pytest.raises(InvalidState):
        store.validate_binding(a.project_id, original.binding_id, expected_version=2)
    assert bind(store, identities).binding_id != original.binding_id


def test_get_agent_cannot_borrow_another_owner(store, identities):
    owner, member, _, _, agent = identities
    assert store.get_agent(agent.agent_id, member.user_id) == agent
    assert store.list_agents(owner.user_id) == []
    with pytest.raises(NotFound):
        store.get_agent(agent.agent_id, owner.user_id)
    with pytest.raises(NotFound):
        store.update_agent(agent.agent_id, owner.user_id, name="hijack", status="active", expected_version=1)


@pytest.mark.parametrize("field,value", [
    ("duty", " "), ("persona_version", ""), ("allowed_tools", "Read"),
    ("allowed_tools", ("Read", "Read")), ("allowed_tools", ("",)),
    ("workspace_ref", "../project-b"), ("workspace_ref", "C:\\private"),
    ("provider_ref", "https://untrusted.invalid"),
])
def test_binding_input_is_validated(store, identities, field, value):
    with pytest.raises(ValidationError):
        bind(store, identities, **{field: value})


@pytest.mark.parametrize("expected", [0, -1, True, "1"])
def test_expected_version_must_be_positive_integer(store, identities, expected):
    _, _, a, _, _ = identities
    with pytest.raises(ValidationError):
        store.update_project(a.project_id, name="bad", status="active", expected_version=expected)


def test_concurrent_binding_creation_has_one_winner(store, identities):
    barrier = Barrier(2)
    def create():
        local = CollaborationStore(store.database)
        barrier.wait()
        try:
            return bind(local, identities)
        except Conflict:
            return None
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: create(), range(2)))
    assert sum(value is not None for value in results) == 1
    with store.database.read() as conn:
        assert conn.execute("SELECT count(*) FROM agent_bindings").fetchone()[0] == 1
        assert conn.execute("SELECT count(*) FROM binding_revisions").fetchone()[0] == 1


def test_concurrent_expected_version_updates_have_one_winner(store, identities):
    original = bind(store, identities)
    barrier = Barrier(2)
    def update(duty):
        local = CollaborationStore(store.database)
        barrier.wait()
        try:
            return edit_binding(local, original, duty=duty)
        except Conflict:
            return None
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(update, ("backend", "frontend")))
    winners = [value for value in results if value is not None]
    assert len(winners) == 1
    assert winners[0].version == 2
    assert store.get_binding(original.project_id, original.binding_id) == winners[0]


def test_concurrent_removal_never_leaves_active_binding(store, identities):
    _, member, a, _, _ = identities
    barrier = Barrier(2)

    def create():
        barrier.wait()
        try:
            return bind(CollaborationStore(store.database), identities)
        except InvalidState:
            return None

    def remove():
        barrier.wait()
        return CollaborationStore(store.database).update_member(
            a.project_id, member.user_id, access_role="member", status="removed", expected_version=1)

    with ThreadPoolExecutor(max_workers=2) as pool:
        created = pool.submit(create)
        removed = pool.submit(remove)
        created.result()
        assert removed.result().status == "removed"
    assert all(binding.status == "revoked" for binding in store.list_bindings(a.project_id))


def test_concurrent_database_initialization_is_idempotent(tmp_path):
    path = tmp_path / "parallel.sqlite3"
    barrier = Barrier(2)

    def initialize():
        barrier.wait()
        Database(path).initialize()

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(lambda _: initialize(), range(2)))
    store = CollaborationStore(Database(path))
    assert store.create_user("ready").version == 1


def test_stale_edits_do_not_overwrite_identity_changes(store, identities):
    _, member, a, _, agent = identities
    member_update = dict(access_role="reviewer", status="active", expected_version=1)
    store.update_member(a.project_id, member.user_id, **member_update)
    with pytest.raises(Conflict):
        store.update_member(a.project_id, member.user_id, **member_update)
    store.update_user(member.user_id, display_name="renamed", status="active", expected_version=1)
    with pytest.raises(Conflict):
        store.update_user(member.user_id, display_name="stale", status="active", expected_version=1)
    store.update_agent(agent.agent_id, member.user_id, name="renamed", status="active", expected_version=1)
    with pytest.raises(Conflict):
        store.update_agent(agent.agent_id, member.user_id, name="stale", status="active", expected_version=1)
    store.update_project(a.project_id, name="renamed", status="active", expected_version=1)
    with pytest.raises(Conflict):
        store.update_project(a.project_id, name="stale", status="active", expected_version=1)


def test_old_data_unchanged_and_independent(store, identities, tmp_path, monkeypatch):
    from task_store import TaskRecord, TaskStore
    old_path = tmp_path / "legacy"
    old_store = TaskStore(str(old_path))
    record = TaskRecord(task_id="legacy-1", role="executor", prompt="keep", worker_id=None,
                        status="done", created_at="t0", updated_at="t1", output="unchanged",
                        artifacts=[], error=None)
    old_store.write(record)
    monkeypatch.setenv("ORCH_TASK_STORE_DIR", str(old_path))
    before = {p.name: p.read_bytes() for p in old_path.iterdir() if p.is_file()}
    bind(store, identities)
    assert {p.name: p.read_bytes() for p in old_path.iterdir() if p.is_file()} == before
    assert asdict(TaskStore(str(old_path)).get("legacy-1")) == asdict(record)


def test_restart_in_new_process_preserves_binding(store, identities):
    binding = bind(store, identities)
    code = (
        "import json,sys; from dataclasses import asdict; "
        "from collaboration.database import Database; from collaboration.store import CollaborationStore; "
        "db=Database(sys.argv[1]); db.initialize(); "
        "print(json.dumps(asdict(CollaborationStore(db).get_binding(sys.argv[2],sys.argv[3]))))"
    )
    env = dict(os.environ, PYTHONPATH=str(RUNTIME), PYTHONIOENCODING="utf-8")
    result = subprocess.run([sys.executable, "-c", code, str(store.database.path),
                             binding.project_id, binding.binding_id], env=env,
                            capture_output=True, text=True, encoding="utf-8", timeout=20, check=True)
    expected = asdict(binding)
    expected["allowed_tools"] = list(expected["allowed_tools"])
    assert json.loads(result.stdout) == expected


def test_old_startup_does_not_import_collaboration(tmp_path):
    root = RUNTIME.parents[3]
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("AGENT_", "ORCH_", "ANTHROPIC_", "OPENAI_"))}
    env.update(AGENT_ENV_FILE=str(tmp_path / "missing.env"), PYTHONIOENCODING="utf-8",
               PYTHONPATH=os.pathsep.join((str(root), str(RUNTIME), str(root / "worker"))))
    code = (
        "import sys; import agent_core.kernel, brain_api, worker_api; "
        "assert not any('collaboration' in name.split('.') for name in sys.modules)"
    )
    subprocess.run([sys.executable, "-c", code], cwd=tmp_path, env=env,
                   capture_output=True, text=True, encoding="utf-8", timeout=30, check=True)
