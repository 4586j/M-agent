from __future__ import annotations

import hashlib
import sqlite3
import sys
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from threading import Barrier

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from collaboration import database as database_module
from collaboration.auth import (
    AuthService, Forbidden, LOGIN_LIMIT, LOGIN_WINDOW, RateLimited,
    Unauthorized, hash_password, normalize_username, verify_password,
)
from collaboration.database import Database, SCHEMA_VERSION
from collaboration.models import Conflict, NotFound, SchemaVersionError, ValidationError
from collaboration.store import CollaborationStore

PASSWORD = "correct-horse-battery-42"


@pytest.fixture
def auth(tmp_path):
    database = Database(tmp_path / "collab.sqlite3")
    database.initialize()
    return AuthService(database)


@pytest.fixture
def admin(auth):
    auth.bootstrap("admin", "管理员", PASSWORD)
    return auth.login("admin", PASSWORD, "127.0.0.1")


def invited_member(auth, admin, username="member"):
    invitation = auth.create_invitation(admin.session_token, admin.csrf_token, username, "成员")
    user = auth.register(invitation.token, PASSWORD)
    session = auth.login(username, PASSWORD, "127.0.0.2")
    return user, session


def test_password_hash_is_salted_and_verifiable():
    first, second = hash_password(PASSWORD), hash_password(PASSWORD)
    assert first != second
    assert PASSWORD not in first
    assert first.startswith("pbkdf2_sha256$600000$")
    assert verify_password(PASSWORD, first)
    assert not verify_password("different-password", first)
    assert not verify_password(PASSWORD, "malformed")
    assert not verify_password(PASSWORD, first.replace("600000", "999999999"))


@pytest.mark.parametrize("password", ["short", "a" * 1025, chr(0xD800) * 12, None])
def test_password_policy(password):
    with pytest.raises(ValidationError):
        hash_password(password)


@pytest.mark.parametrize("name", ["a", "a b", "../name", "x" * 65, "账号"])
def test_username_validation(name):
    with pytest.raises(ValidationError):
        normalize_username(name)


def test_bootstrap_only_once_and_case_insensitive_login(auth, admin):
    with pytest.raises(Conflict):
        auth.bootstrap("another", "另一个", PASSWORD)
    session = auth.login("ADMIN", PASSWORD, "127.0.0.1")
    assert session.user == admin.user
    assert session.is_admin
    with auth.database.read() as conn:
        assert conn.execute("SELECT count(*) FROM users").fetchone()[0] == 1
        stored = conn.execute("SELECT * FROM auth_sessions WHERE token_hash=?",
                              (hashlib.sha256(session.session_token.encode()).hexdigest(),)).fetchone()
        assert stored is not None
        assert session.session_token not in str(dict(stored))
        assert conn.execute("SELECT count(*) FROM collaboration_audit WHERE action='account.bootstrapped'").fetchone()[0] == 1


def test_bootstrap_concurrency_has_one_winner(auth):
    barrier = Barrier(2)
    def bootstrap(name):
        barrier.wait()
        try:
            return auth.bootstrap(name, name, PASSWORD)
        except Conflict:
            return None
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(bootstrap, ("admin-one", "admin-two")))
    assert sum(item is not None for item in results) == 1


def test_invitation_acceptance_is_atomic_and_one_time(auth, admin):
    invitation = auth.create_invitation(admin.session_token, admin.csrf_token, "Member", "成员")
    with auth.database.read() as conn:
        row = conn.execute("SELECT * FROM auth_invitations").fetchone()
        assert invitation.token not in str(dict(row))
    user = auth.register(invitation.token, PASSWORD)
    assert auth.login("member", PASSWORD, "127.0.0.1").user == user
    with pytest.raises(Conflict):
        auth.register(invitation.token, PASSWORD)
    with auth.database.read() as conn:
        assert conn.execute("SELECT count(*) FROM auth_accounts").fetchone()[0] == 2
        assert conn.execute("SELECT count(*) FROM collaboration_audit WHERE action='invitation.accepted'").fetchone()[0] == 1


def test_concurrent_invitation_acceptance_has_one_winner(auth, admin):
    invitation = auth.create_invitation(admin.session_token, admin.csrf_token, "member", "成员")
    barrier = Barrier(2)
    def register():
        barrier.wait()
        try:
            return auth.register(invitation.token, PASSWORD)
        except Conflict:
            return None
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: register(), range(2)))
    assert sum(item is not None for item in results) == 1


def test_expired_and_revoked_invitations(auth, admin):
    invitation = auth.create_invitation(admin.session_token, admin.csrf_token, "member", "成员")
    auth.revoke_invitation(admin.session_token, admin.csrf_token, invitation.invitation_id, 1)
    with pytest.raises(Conflict):
        auth.register(invitation.token, PASSWORD)
    with pytest.raises(Conflict):
        auth.revoke_invitation(admin.session_token, admin.csrf_token, invitation.invitation_id, 1)
    second = auth.create_invitation(admin.session_token, admin.csrf_token, "other", "成员")
    auth.clock = lambda: second.expires_at
    with pytest.raises(Conflict):
        auth.register(second.token, PASSWORD)


def test_pending_invitation_and_registered_username_cannot_be_reused(auth, admin):
    invitation = auth.create_invitation(admin.session_token, admin.csrf_token, "member", "成员")
    with pytest.raises(Conflict):
        auth.create_invitation(admin.session_token, admin.csrf_token, "MEMBER", "成员")
    auth.register(invitation.token, PASSWORD)
    with pytest.raises(Conflict):
        auth.create_invitation(admin.session_token, admin.csrf_token, "member", "成员")


def test_nonadmin_cannot_invite_or_read_admin_scope(auth, admin):
    _, session = invited_member(auth, admin)
    assert not session.is_admin
    with pytest.raises(Forbidden):
        auth.create_invitation(session.session_token, session.csrf_token, "outsider", "外部")
    with pytest.raises(Forbidden):
        with auth.access(session.session_token, admin=True):
            pytest.fail("Should not obtain admin access")


@pytest.mark.parametrize("csrf", [None, "wrong", "非ASCII", chr(0xD800), "x" * 129])
def test_csrf_protects_mutations(auth, admin, csrf):
    with pytest.raises(Forbidden):
        auth.logout(admin.session_token, csrf)
    with auth.access(admin.session_token) as access:
        assert access.user == admin.user


def test_logout_login_rotation_and_expiration(auth, admin):
    second = auth.login("admin", PASSWORD, "127.0.0.1", previous_token=admin.session_token)
    assert second.session_token != admin.session_token
    assert second.csrf_token != admin.csrf_token
    with pytest.raises(Unauthorized):
        with auth.access(admin.session_token):
            pytest.fail("Old session must be revoked")
    auth.logout(second.session_token, second.csrf_token)
    with pytest.raises(Unauthorized):
        with auth.access(second.session_token):
            pytest.fail("Logged out session must be revoked")
    third = auth.login("admin", PASSWORD, "127.0.0.1")
    auth.clock = lambda: third.expires_at
    with pytest.raises(Unauthorized):
        with auth.access(third.session_token):
            pytest.fail("Expired session must be rejected")


def test_disabled_then_reenabled_user_does_not_recover_session_or_invitation(auth, admin):
    invitation = auth.create_invitation(admin.session_token, admin.csrf_token, "member", "成员")
    store = CollaborationStore(auth.database)
    store.update_user(admin.user.user_id, display_name=admin.user.display_name, status="disabled", expected_version=1)
    store.update_user(admin.user.user_id, display_name=admin.user.display_name, status="active", expected_version=2)
    with pytest.raises(Unauthorized):
        with auth.access(admin.session_token):
            pytest.fail("Session should not revive")
    with pytest.raises(Conflict):
        auth.register(invitation.token, PASSWORD)
    fresh = auth.login("admin", PASSWORD, "127.0.0.1")
    assert fresh.user.version == 3


def test_project_authorization_is_not_implied_by_platform_admin(auth, admin):
    user, session = invited_member(auth, admin)
    store = CollaborationStore(auth.database)
    project = store.create_project("private", user.user_id)
    with pytest.raises(NotFound):
        with auth.access(admin.session_token, project_id=project.project_id):
            pytest.fail("Platform administrator must not read private project")
    with auth.access(session.session_token, project_id=project.project_id, roles=("owner",)) as access:
        assert access.store.get_project(project.project_id) == project


def test_membership_removed_or_demoted_is_rechecked_per_operation(auth, admin):
    user, session = invited_member(auth, admin)
    store = CollaborationStore(auth.database)
    project = store.create_project("project", admin.user.user_id)
    store.add_member(project.project_id, user.user_id, access_role="manager")
    with auth.access(session.session_token, project_id=project.project_id, roles=("owner", "manager")):
        pass
    store.update_member(project.project_id, user.user_id, access_role="member", status="active", expected_version=1)
    with pytest.raises(Forbidden):
        with auth.access(session.session_token, project_id=project.project_id, roles=("owner", "manager")):
            pytest.fail("Role downgrade ignored")
    store.update_member(project.project_id, user.user_id, access_role="member", status="removed", expected_version=2)
    with pytest.raises(NotFound):
        with auth.access(session.session_token, project_id=project.project_id):
            pytest.fail("Membership removal ignored")


def test_authorized_write_and_audit_share_transaction(auth, admin):
    with auth.access(admin.session_token, admin.csrf_token, write=True) as access:
        project = access.store.create_project("with audit", access.user.user_id)
        auth.audit(access.conn, access.user.user_id, "project.created", project.project_id,
                   project_id=project.project_id, target_version=1)
    with auth.access(admin.session_token, project_id=project.project_id) as access:
        assert access.store.get_project(project.project_id) == project
        assert access.conn.execute("SELECT count(*) FROM collaboration_audit WHERE project_id=?",
                                   (project.project_id,)).fetchone()[0] == 1


def test_audit_failure_rolls_back_mutation(auth, admin, monkeypatch):
    def fail(*args, **kwargs):
        raise RuntimeError("audit failure")
    monkeypatch.setattr(auth, "audit", fail)
    with pytest.raises(RuntimeError):
        with auth.access(admin.session_token, admin.csrf_token, write=True) as access:
            project = access.store.create_project("rollback", access.user.user_id)
            auth.audit(access.conn, access.user.user_id, "project.created", project.project_id)
    assert CollaborationStore(auth.database).list_projects(admin.user.user_id) == []


def test_read_access_cannot_mutate(auth, admin):
    with auth.access(admin.session_token) as access:
        with pytest.raises(sqlite3.OperationalError):
            access.store.create_project("blocked", access.user.user_id)
    assert CollaborationStore(auth.database).list_projects(admin.user.user_id) == []


def test_invitation_registration_audit_failure_rolls_back_account(auth, admin, monkeypatch):
    invitation = auth.create_invitation(admin.session_token, admin.csrf_token, "member", "成员")
    def fail(*args, **kwargs):
        raise RuntimeError("audit failure")
    with monkeypatch.context() as patch:
        patch.setattr(auth, "audit", fail)
        with pytest.raises(RuntimeError):
            auth.register(invitation.token, PASSWORD)
    with auth.database.read() as conn:
        assert conn.execute("SELECT count(*) FROM auth_accounts").fetchone()[0] == 1
        assert conn.execute("SELECT status FROM auth_invitations").fetchone()[0] == "pending"
    assert auth.register(invitation.token, PASSWORD).display_name == "成员"


def test_login_limit_persists_after_restart_and_window_expires(auth, admin):
    now = 1000.0
    auth.clock = lambda: now
    for _ in range(LOGIN_LIMIT):
        with pytest.raises(Unauthorized):
            auth.login("unknown", "wrong-password-long", "hostile-peer")
    restarted = AuthService(auth.database, clock=lambda: now)
    with pytest.raises(RateLimited):
        restarted.login("admin", PASSWORD, "hostile-peer")
    assert restarted.login("admin", PASSWORD, "other-peer").user == admin.user
    now += LOGIN_WINDOW
    assert restarted.login("admin", PASSWORD, "hostile-peer").user == admin.user


def test_login_rechecks_user_after_password_verification(auth, admin, monkeypatch):
    import collaboration.auth as module
    original = module.verify_password
    def disable_then_verify(password, encoded):
        store = CollaborationStore(auth.database)
        store.update_user(admin.user.user_id, display_name=admin.user.display_name,
                          status="disabled", expected_version=1)
        return original(password, encoded)
    monkeypatch.setattr(module, "verify_password", disable_then_verify)
    with pytest.raises(Unauthorized):
        auth.login("admin", PASSWORD, "127.0.0.1")


@pytest.mark.parametrize("statement", [
    "UPDATE collaboration_audit SET action='tampered'",
    "DELETE FROM collaboration_audit",
    "INSERT OR REPLACE INTO collaboration_audit SELECT event_id,project_id,actor_id,'tampered',target_id,target_version,created_at FROM collaboration_audit",
])
def test_audit_is_immutable(auth, admin, statement):
    with pytest.raises(sqlite3.IntegrityError):
        with auth.database.transaction() as conn:
            conn.execute(statement)


def test_no_secrets_in_audit_or_password_storage(auth, admin):
    invitation = auth.create_invitation(admin.session_token, admin.csrf_token, "member", "成员")
    with auth.database.read() as conn:
        values = str([dict(row) for row in conn.execute("SELECT * FROM collaboration_audit")])
        values += str([dict(row) for row in conn.execute("SELECT * FROM auth_accounts")])
    for secret in (PASSWORD, admin.session_token, admin.csrf_token, invitation.token):
        assert secret not in values


def make_v1_database(path):
    with closing(sqlite3.connect(path, isolation_level=None)) as conn:
        conn.execute("BEGIN IMMEDIATE")
        for statement in database_module._SCHEMA_STATEMENTS:
            conn.execute(statement)
        conn.execute("INSERT INTO users(user_id,display_name) VALUES('old-user','preserved')")
        conn.execute("PRAGMA user_version=1")
        conn.commit()


def test_v1_migration_preserves_existing_identity(tmp_path):
    path = tmp_path / "v1.sqlite3"
    make_v1_database(path)
    database = Database(path)
    with pytest.raises(SchemaVersionError):
        with database.read():
            pytest.fail("Migration must be explicit")
    database.initialize()
    assert CollaborationStore(database).get_user("old-user").display_name == "preserved"
    with database.read() as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    assert AuthService(database).bootstrap("admin", "管理员", PASSWORD).status == "active"


def test_v1_migration_failure_rolls_back(tmp_path, monkeypatch):
    path = tmp_path / "v1.sqlite3"
    make_v1_database(path)
    before = path.read_bytes()
    with monkeypatch.context() as patch:
        patch.setattr(database_module, "_AUTH_SCHEMA", database_module._AUTH_SCHEMA[:1] + ("INVALID SQL",))
        with pytest.raises(sqlite3.OperationalError):
            Database(path).initialize()
    assert path.read_bytes() == before
    Database(path).initialize()
    assert CollaborationStore(Database(path)).get_user("old-user").version == 1


@pytest.mark.parametrize("state", ["valid", "revoked", "expired", "user_changed"])
def test_session_state_survives_service_restart(auth, admin, state):
    session = auth.login("admin", PASSWORD, "127.0.0.1")
    if state == "revoked":
        auth.logout(session.session_token, session.csrf_token)
    elif state == "expired":
        auth.clock = lambda: session.expires_at
    elif state == "user_changed":
        CollaborationStore(auth.database).update_user(
            session.user.user_id, display_name=session.user.display_name, status="disabled", expected_version=1)
    restarted = AuthService(Database(auth.database.path), clock=auth.clock)
    if state == "valid":
        with restarted.access(session.session_token) as access:
            assert access.user == session.user
            assert access.csrf_token == session.csrf_token
    else:
        with pytest.raises(Unauthorized):
            with restarted.access(session.session_token):
                pytest.fail("Invalid session revived after restart")


@pytest.mark.parametrize("target", ["invitation", "inviter"])
def test_registration_rechecks_revocation_after_password_hash(auth, admin, monkeypatch, target):
    import collaboration.auth as module
    invitation = auth.create_invitation(admin.session_token, admin.csrf_token, "member", "成员")
    original = module.hash_password

    def hash_then_revoke(password):
        encoded = original(password)
        if target == "invitation":
            auth.revoke_invitation(admin.session_token, admin.csrf_token, invitation.invitation_id, 1)
        else:
            CollaborationStore(auth.database).update_user(
                admin.user.user_id, display_name=admin.user.display_name, status="disabled", expected_version=1)
        return encoded

    monkeypatch.setattr(module, "hash_password", hash_then_revoke)
    with pytest.raises(Conflict):
        auth.register(invitation.token, PASSWORD)
    with auth.database.read() as conn:
        assert conn.execute("SELECT count(*) FROM users").fetchone()[0] == 1
        assert conn.execute("SELECT count(*) FROM auth_accounts").fetchone()[0] == 1
        assert conn.execute("SELECT count(*) FROM collaboration_audit WHERE action='invitation.accepted'").fetchone()[0] == 0


def test_invalid_unicode_is_validation_error_not_encoding_crash(auth):
    assert not verify_password(chr(0xD800) * 12, hash_password(PASSWORD))
    with pytest.raises(ValidationError):
        CollaborationStore(auth.database).create_user(chr(0xD800))
