"""HTTP/CLI contract tests using real authentication and temporary SQLite databases."""
from __future__ import annotations

from contextlib import closing
from dataclasses import asdict, dataclass
import base64
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import subprocess
import sys
import time
from types import SimpleNamespace
from urllib.parse import unquote

import httpx
import pytest
from fastapi.testclient import TestClient

RUNTIME = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RUNTIME))

from collaboration import __main__ as cli
from collaboration.app import (_ACTIVITY_DENIED, AppConfig, COOKIE, PREFIX, _wake, create_app)
from collaboration.database import Database, SCHEMA_VERSION
from collaboration.reviews import INTEGRATION_DECISIONS
from collaboration.store import (REQUIREMENT_TRANSITIONS, WORK_ITEM_STATUSES,
                                 CollaborationStore)

ORIGIN = "https://testserver"
PASSWORD = "test-only-password-2026"
BINDING_FIELDS = dict(duty="Project-specific duty", persona_version="persona-v1",
                      allowed_tools=["Read", "Test"], workspace_ref="workspace-a",
                      provider_ref="provider-a")


def checked(response, status=200):
    assert response.status_code == status, response.text
    assert response.headers["cache-control"] == "no-store"
    assert "session_token" not in response.text
    return response.json() if response.content else None


@pytest.fixture(scope="module")
def registered_database(tmp_path_factory):
    """Hash/register each account once, then clone the real database for isolation."""
    directory = tmp_path_factory.mktemp("collab-registered")
    app = create_app(AppConfig(directory, public_origin=ORIGIN))
    identities = {}
    with TestClient(app, base_url=ORIGIN, client=("seed-peer", 50000)) as client:
        app.state.auth.bootstrap("admin", "Administrator", PASSWORD)
        for username in ("admin", "owner", "manager", "member", "outsider"):
            if username != "admin":
                admin = identities["admin"]
                invitation = checked(client.post(PREFIX + "/auth/invitations", json={
                    "username": username, "display_name": username.title()}, headers={
                    "Origin": ORIGIN, "Cookie": f"{COOKIE}={admin['token']}",
                    "X-CSRF-Token": admin["csrf"]}), 201)
                client.cookies.clear()
                registered = client.post(PREFIX + "/auth/register", json={
                    "invitation_token": invitation["token"], "password": PASSWORD},
                    headers={"Origin": ORIGIN})
                checked(registered, 201)
                assert "set-cookie" not in registered.headers
                assert client.get(PREFIX + "/me").status_code == 401
            client.cookies.clear()
            login = client.post(PREFIX + "/auth/login", json={
                "username": username, "password": PASSWORD}, headers={"Origin": ORIGIN})
            data = checked(login)
            identities[username] = dict(user=data["user"], csrf=data["csrf_token"],
                                        token=client.cookies.get(COOKIE))
    return directory / "metadata.sqlite3", identities


@dataclass
class API:
    client: TestClient
    identities: dict
    database: Database

    def send(self, actor, method, path, *, headers=None, **kwargs):
        self.client.cookies.clear()
        actual = {"Origin": ORIGIN}
        if actor is not None:
            identity = self.identities[actor]
            actual.update({"Cookie": f"{COOKIE}={identity['token']}",
                           "X-CSRF-Token": identity["csrf"]})
        actual.update(headers or {})
        return self.client.request(method, PREFIX + path,
                                   headers={key: value for key, value in actual.items() if value is not None},
                                   **kwargs)

    def project(self, actor="owner", name="Project A"):
        return checked(self.send(actor, "POST", "/projects", json={"name": name}), 201)

    def member(self, project, actor="member", role="member"):
        return checked(self.send("owner", "POST", f"/projects/{project['project_id']}/members",
                                 json={"user_id": self.identities[actor]["user"]["user_id"],
                                       "access_role": role}), 201)

    def agent(self, actor="member"):
        return checked(self.send(actor, "POST", "/agents", json={"name": "My Agent"}), 201)

    def binding(self, project, agent, actor="member", **fields):
        return checked(self.send("owner", "POST", f"/projects/{project['project_id']}/agent-bindings",
                                 json={**BINDING_FIELDS, **fields, "agent_id": agent["agent_id"],
                                       "member_user_id": self.identities[actor]["user"]["user_id"]}), 201)


@pytest.fixture
def api(tmp_path, registered_database):
    source, identities = registered_database
    path = tmp_path / "metadata.sqlite3"
    with closing(sqlite3.connect(source)) as original, closing(sqlite3.connect(path)) as copy:
        original.backup(copy)
    app = create_app(AppConfig(tmp_path, public_origin=ORIGIN))
    with TestClient(app, base_url=ORIGIN, client=("127.0.0.1", 42000)) as client:
        yield API(client, identities, Database(path))


@pytest.mark.parametrize("origin", [
    "http://example.com", "http://0.0.0.0:7400", "http://192.168.1.2", "http://[::]",
    "https://user@testserver", "https://user:password@testserver", "https://testserver/path",
    "https://testserver?secret=1", "https://testserver#fragment", "https://testserver?",
    "https://testserver#", "https://testserver:", "https://testserver:0",
    "https://testserver:65536", "https://testserver:invalid", "https://testserver\\evil",
    "https://testserver https://other", "https://testserver\n", "https://", "file:///tmp",
    "http://localhost.evil", "https://[invalid]", "https://test%73erver", "\x00https://testserver",
])
def test_invalid_origins_fail_without_disk_side_effects(tmp_path, origin):
    directory = tmp_path / "untouched"
    with pytest.raises(ValueError):
        create_app(AppConfig(directory, public_origin=origin))
    assert not directory.exists()


@pytest.mark.parametrize("origin", [
    "http://localhost:7400", "http://127.0.0.1:7400", "http://127.0.0.2", "http://[::1]:7400",
    "https://example.com", "https://testserver/",
])
def test_allowed_origins_are_not_network_or_disk_operations(tmp_path, origin):
    directory = tmp_path / "untouched"
    app = create_app(AppConfig(directory, public_origin=origin))
    assert not directory.exists()
    assert not hasattr(app.state, "auth")


def test_lifespan_initializes_only_explicit_database(tmp_path):
    directory = tmp_path / "fresh"
    app = create_app(AppConfig(directory, public_origin=ORIGIN))
    assert not directory.exists()
    with TestClient(app, base_url=ORIGIN) as client:
        assert (directory / "metadata.sqlite3").is_file()
        checked(client.get(PREFIX + "/me"), 401)
        checked(client.get("/docs"), 404)
        checked(client.get("/openapi.json"), 404)


def test_import_has_no_database_or_dotenv_side_effects(tmp_path):
    code = f"""
import builtins, sqlite3, sys
sys.path.insert(0, {str(RUNTIME)!r})
original_open = builtins.open
def guarded_open(file, *args, **kwargs):
    if str(file).replace('\\\\', '/').rsplit('/', 1)[-1] == '.env':
        raise AssertionError('dotenv must not be read')
    return original_open(file, *args, **kwargs)
builtins.open = guarded_open
def no_database(*args, **kwargs):
    raise AssertionError('database opened at import')
sqlite3.connect = no_database
import collaboration.app
import collaboration.__main__
assert 'brain_api' not in sys.modules
assert 'dotenv' not in sys.modules
"""
    result = subprocess.run([sys.executable, "-c", code], cwd=tmp_path,
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    assert not list(tmp_path.iterdir())


def test_login_rotation_me_logout_and_cookie_flags(api):
    old = api.identities["owner"]
    response = api.send("owner", "POST", "/auth/login",
                        json={"username": "owner", "password": PASSWORD})
    login = checked(response)
    cookie = response.headers["set-cookie"]
    token = api.client.cookies.get(COOKIE)
    assert token != old["token"]
    assert token not in response.text and PASSWORD not in response.text
    for flag in ("HttpOnly", "Secure", "SameSite=strict", "Path=/collab/v1", "Max-Age=28800"):
        assert flag in cookie
    assert "Domain=" not in cookie
    checked(api.send("owner", "GET", "/me"), 401)
    headers = {"Cookie": f"{COOKIE}={token}", "X-CSRF-Token": login["csrf_token"]}
    assert checked(api.send(None, "GET", "/me", headers=headers)) == login
    checked(api.send(None, "POST", "/auth/logout", headers={**headers, "X-CSRF-Token": None}), 403)
    logout = api.send(None, "POST", "/auth/logout", headers=headers)
    checked(logout, 204)
    assert not logout.content
    for flag in ("Max-Age=0", "Path=/collab/v1", "HttpOnly", "Secure", "SameSite=strict"):
        assert flag in logout.headers["set-cookie"]
    checked(api.send(None, "GET", "/me", headers=headers), 401)


def test_loopback_http_cookie_is_not_secure(api, tmp_path):
    app = create_app(AppConfig(tmp_path, public_origin="http://127.0.0.1:7400"))
    with TestClient(app, base_url="http://127.0.0.1:7400") as client:
        login = client.post(PREFIX + "/auth/login", json={"username": "owner", "password": PASSWORD},
                            headers={"Origin": "http://127.0.0.1:7400"})
        checked(login)
        assert "Secure" not in login.headers["set-cookie"]
        checked(client.get(PREFIX + "/me"))
        checked(client.get(PREFIX + "/me", headers={"Host": "localhost:7400"}), 403)
        checked(client.get(PREFIX + "/me", headers={"Origin": "http://localhost:7400"}), 403)


def test_invitation_admin_csrf_revoke_and_single_use(api):
    body = {"username": "invited", "display_name": "Invited Person"}
    checked(api.send(None, "POST", "/auth/invitations", json=body), 401)
    checked(api.send("owner", "POST", "/auth/invitations", json=body), 403)
    checked(api.send("admin", "POST", "/auth/invitations", json=body,
                     headers={"X-CSRF-Token": None}), 403)
    invitation = checked(api.send("admin", "POST", "/auth/invitations", json=body), 201)
    path = f"/auth/invitations/{invitation['invitation_id']}/revoke"
    checked(api.send("owner", "POST", path, json={"expected_version": 1}), 403)
    checked(api.send("admin", "POST", path, json={"expected_version": 2}), 409)
    revoked = checked(api.send("admin", "POST", path, json={"expected_version": 1}))
    assert revoked["version"] == 2
    assert invitation["token"] not in json.dumps(revoked)
    checked(api.send(None, "POST", "/auth/register",
                     json={"invitation_token": invitation["token"], "password": PASSWORD}), 409)
    invitation = checked(api.send("admin", "POST", "/auth/invitations", json=body), 201)
    payload = {"invitation_token": invitation["token"], "password": PASSWORD}
    registered = api.send(None, "POST", "/auth/register", json=payload)
    assert checked(registered, 201)["display_name"] == "Invited Person"
    assert "set-cookie" not in registered.headers
    checked(api.send(None, "GET", "/me"), 401)
    checked(api.send(None, "POST", "/auth/register", json=payload), 409)
    audit = api.send("admin", "GET", "/admin/audit")
    checked(audit)
    assert invitation["token"] not in audit.text and PASSWORD not in audit.text


def test_projects_members_agents_bindings_and_audit_round_trip(api):
    project = api.project()
    pid = project["project_id"]
    assert project["owner_id"] == api.identities["owner"]["user"]["user_id"]
    assert checked(api.send("owner", "GET", "/projects")) == [project]
    assert checked(api.send("owner", "GET", f"/projects/{pid}")) == {**project,
                                                                     "access_role": "owner"}
    member = api.member(project)
    assert checked(api.send("member", "GET", f"/projects/{pid}"))["access_role"] == "member"
    agent = api.agent()
    assert checked(api.send("member", "GET", "/agents")) == [agent]
    assert checked(api.send("owner", "GET", "/agents")) == []
    binding = api.binding(project, agent)
    bid = binding["binding_id"]
    path = f"/projects/{pid}/agent-bindings/{bid}"
    assert checked(api.send("member", "GET", path)) == binding
    assert checked(api.send("member", "GET", f"/projects/{pid}/agent-bindings")) == [binding]
    updated = checked(api.send("owner", "PATCH", path,
                               json={**BINDING_FIELDS, "duty": "Revised duty", "expected_version": 1}))
    assert updated["version"] == 2
    assert checked(api.send("member", "GET", path + "/revisions/1")) == binding
    assert checked(api.send("member", "GET", path + "/revisions/2")) == updated
    checked(api.send("owner", "PATCH", path, json={**BINDING_FIELDS, "expected_version": 1}), 409)
    revoked = checked(api.send("owner", "POST", path + "/revoke", json={"expected_version": 2}))
    assert (revoked["status"], revoked["version"]) == ("revoked", 3)
    assert checked(api.send("member", "GET", path + "/revisions/3")) == revoked
    checked(api.send("member", "GET", path + "/revisions/4"), 404)
    updated_member = checked(api.send("owner", "PATCH", f"/projects/{pid}/members/{member['user_id']}",
                                      json={"access_role": "reviewer", "status": "active", "expected_version": 1}))
    assert updated_member["version"] == 2
    roster = checked(api.send("member", "GET", f"/projects/{pid}/members"))
    assert len(roster) == 2
    assert {entry["display_name"] for entry in roster} == {"Owner", "Member"}
    updated_agent = checked(api.send("member", "PATCH", f"/agents/{agent['agent_id']}",
                                     json={"name": "Renamed", "status": "active", "expected_version": 1}))
    assert updated_agent["version"] == 2
    changed_project = checked(api.send("owner", "PATCH", f"/projects/{pid}",
                                       json={"name": "New name", "status": "active", "expected_version": 1}))
    assert changed_project["version"] == 2
    audit = checked(api.send("owner", "GET", f"/projects/{pid}/audit"))
    actions = {(row["action"], row["target_id"], row["target_version"]) for row in audit}
    assert {("project.created", pid, 1), ("project.updated", pid, 2),
            ("member.added", member["user_id"], 1), ("member.updated", member["user_id"], 2),
            ("binding.created", bid, 1), ("binding.updated", bid, 2), ("binding.revoked", bid, 3)} <= actions
    assert all(row["project_id"] == pid for row in audit)
    assert "Revised duty" not in json.dumps(audit)
    assert "workspace-a" not in json.dumps(audit)
    admin_audit = checked(api.send("admin", "GET", "/admin/audit"))
    assert all(row["project_id"] is None for row in admin_audit)
    assert {"agent.created", "agent.updated"} <= {row["action"] for row in admin_audit}


def test_admin_has_no_cross_project_exemption_or_agent_ownership(api):
    project = api.project()
    pid = project["project_id"]
    api.member(project)
    agent = api.agent()
    binding = api.binding(project, agent)
    paths = [f"/projects/{pid}", f"/projects/{pid}/members", f"/projects/{pid}/agent-bindings",
             f"/projects/{pid}/agent-bindings/{binding['binding_id']}",
             f"/projects/{pid}/agent-bindings/{binding['binding_id']}/revisions/1", f"/projects/{pid}/audit"]
    for actor in (None, "outsider", "admin"):
        for path in paths:
            checked(api.send(actor, "GET", path), 401 if actor is None else 404)
    assert checked(api.send("admin", "GET", "/projects")) == []
    checked(api.send("member", "GET", f"/projects/{pid}/audit"), 403)
    checked(api.send("member", "GET", "/admin/audit"), 403)
    for actor in ("owner", "admin", "outsider"):
        checked(api.send(actor, "PATCH", f"/agents/{agent['agent_id']}",
                         json={"name": "Stolen", "status": "disabled", "expected_version": 1}), 404)
    other = api.project(name="Project B")
    other_path = f"/projects/{other['project_id']}/agent-bindings/{binding['binding_id']}"
    checked(api.send("owner", "GET", other_path), 404)
    checked(api.send("owner", "GET", other_path + "/revisions/1"), 404)
    checked(api.send("owner", "PATCH", other_path, json={**BINDING_FIELDS, "expected_version": 1}), 404)
    checked(api.send("owner", "POST", other_path + "/revoke", json={"expected_version": 1}), 404)


def test_no_query_api_key_operator_or_self_reported_identity(api):
    token = api.identities["owner"]["token"]
    uid = api.identities["owner"]["user"]["user_id"]
    for path, headers in [
        (f"/me?token={token}&session_token={token}&collab_session={token}", {}),
        ("/me?api_key=old-key", {"X-API-Key": "old-key", "X-Operator": uid}),
        ("/me", {"Authorization": f"Bearer {token}", "X-User-ID": uid}),
    ]:
        checked(api.send(None, "GET", path, headers=headers), 401)
    checked(api.send(None, "POST", "/projects", json={"name": "Fake"},
                     headers={"X-Operator": uid, "X-API-Key": "old-key"}), 401)
    for path, body in [("/projects", {"name": "Fake", "owner_id": uid}),
                       ("/agents", {"name": "Fake", "owner_user_id": uid})]:
        checked(api.send("member", "POST", path, json=body), 422)
    project = api.project("member")
    assert project["owner_id"] == api.identities["member"]["user"]["user_id"]


@pytest.mark.parametrize("headers", [
    {"Origin": None}, {"Origin": "https://evil.example"}, {"Origin": "null"},
    {"Origin": ORIGIN + "/"}, {"Origin": "http://testserver"}, {"Host": "evil.example"},
    {"Host": "testserver:443"}, {"X-CSRF-Token": None}, {"X-CSRF-Token": "wrong-csrf"},
])
def test_writes_require_exact_origin_host_and_csrf(api, headers):
    checked(api.send("owner", "POST", "/projects", json={"name": "Blocked"}, headers=headers), 403)
    assert checked(api.send("owner", "GET", "/projects")) == []


@pytest.mark.parametrize("path,body", [
    ("/auth/login", {"username": "owner", "password": PASSWORD}),
    ("/auth/register", {"invitation_token": "not-an-invitation", "password": PASSWORD}),
])
def test_login_and_register_require_origin_even_without_session(api, path, body):
    for origin in (None, "null", "https://evil.example"):
        checked(api.send(None, "POST", path, json=body, headers={"Origin": origin}), 403)


def test_read_origin_checks_duplicate_headers_and_no_cors(api):
    checked(api.send("owner", "GET", "/me", headers={"Origin": None}))
    checked(api.send("owner", "GET", "/me", headers={"Origin": "https://evil.example"}), 403)
    for key, values in (("Host", ["testserver", "evil.example"]),
                        ("Origin", [ORIGIN, "https://evil.example"]),
                        ("X-CSRF-Token", ["one", "two"])):
        headers = [(key, value) for value in values]
        checked(api.client.get(PREFIX + "/me", headers=headers), 403)
    response = api.send(None, "OPTIONS", "/projects", headers={
        "Origin": "https://evil.example", "Access-Control-Request-Method": "POST"})
    checked(response, 403)
    assert not any(key.startswith("access-control-") for key in response.headers)


def test_manager_cannot_promote_or_modify_privileged_members(api):
    project = api.project()
    pid = project["project_id"]
    manager = api.member(project, "manager", "manager")
    member = api.member(project)
    owner = api.identities["owner"]["user"]
    members_path = f"/projects/{pid}/members"
    for uid, role in ((member["user_id"], "manager"), (member["user_id"], "owner"),
                      (owner["user_id"], "member"), (manager["user_id"], "member"),
                      (manager["user_id"], "manager")):
        checked(api.send("manager", "PATCH", members_path + f"/{uid}",
                         json={"access_role": role, "status": "active", "expected_version": 1}), 403)
    new_uid = api.identities["outsider"]["user"]["user_id"]
    checked(api.send("manager", "POST", members_path,
                     json={"user_id": new_uid, "access_role": "manager"}), 403)
    checked(api.send("manager", "POST", members_path,
                     json={"user_id": new_uid, "access_role": "reviewer"}), 201)
    checked(api.send("manager", "PATCH", members_path + f"/{member['user_id']}",
                     json={"access_role": "reviewer", "status": "active", "expected_version": 1}))
    checked(api.send("manager", "PATCH", f"/projects/{pid}",
                     json={"name": "Forbidden", "status": "active", "expected_version": 1}), 403)
    checked(api.send("member", "POST", members_path,
                     json={"user_id": new_uid, "access_role": "member"}), 403)
    agent = api.agent()
    payload = {**BINDING_FIELDS, "agent_id": agent["agent_id"], "member_user_id": member["user_id"]}
    checked(api.send("member", "POST", f"/projects/{pid}/agent-bindings", json=payload), 403)
    binding = checked(api.send("manager", "POST", f"/projects/{pid}/agent-bindings", json=payload), 201)
    path = f"/projects/{pid}/agent-bindings/{binding['binding_id']}"
    checked(api.send("manager", "PATCH", path, json={**BINDING_FIELDS, "expected_version": 1}))
    checked(api.send("manager", "POST", path + "/revoke", json={"expected_version": 2}))
    checked(api.send("manager", "GET", f"/projects/{pid}/audit"))


def test_member_removal_immediately_revokes_access_and_binding(api):
    project = api.project()
    pid = project["project_id"]
    member = api.member(project)
    binding = api.binding(project, api.agent())
    member_path = f"/projects/{pid}/members/{member['user_id']}"
    checked(api.send("owner", "PATCH", member_path,
                     json={"access_role": "member", "status": "removed", "expected_version": 1}))
    for path in (f"/projects/{pid}", f"/projects/{pid}/members", f"/projects/{pid}/agent-bindings",
                 f"/projects/{pid}/agent-bindings/{binding['binding_id']}/revisions/1"):
        checked(api.send("member", "GET", path), 404)
    assert checked(api.send("member", "GET", "/projects")) == []
    checked(api.send("member", "GET", "/me"))
    path = f"/projects/{pid}/agent-bindings/{binding['binding_id']}"
    assert checked(api.send("owner", "GET", path))["status"] == "revoked"
    checked(api.send("owner", "PATCH", member_path,
                     json={"access_role": "member", "status": "active", "expected_version": 2}))
    assert checked(api.send("member", "GET", path))["status"] == "revoked"


@pytest.mark.parametrize("operation", ["user", "agent", "member", "project"])
def test_cascading_revocations_are_audited_without_content(api, operation):
    project = api.project()
    member = api.member(project)
    agent = api.agent()
    binding = api.binding(project, agent)
    pid = project["project_id"]
    actor, path, body = {
        "user": ("admin", f"/admin/users/{member['user_id']}", {"status": "disabled"}),
        "agent": ("member", f"/agents/{agent['agent_id']}", {"status": "disabled", "name": "Disabled"}),
        "member": ("owner", f"/projects/{pid}/members/{member['user_id']}",
                   {"status": "removed", "access_role": "member"}),
        "project": ("owner", f"/projects/{pid}", {"status": "archived", "name": "Archived"}),
    }[operation]
    checked(api.send(actor, "PATCH", path, json={**body, "expected_version": 1}))
    events = checked(api.send("owner", "GET", f"/projects/{pid}/audit"))
    revoked = [event for event in events if event["action"] == "binding.revoked"]
    assert len(revoked) == 1
    assert (revoked[0]["target_id"], revoked[0]["target_version"]) == (binding["binding_id"], 2)
    assert revoked[0]["actor_id"] == api.identities[actor]["user"]["user_id"]
    assert BINDING_FIELDS["duty"] not in json.dumps(events)
    binding_path = f"/projects/{pid}/agent-bindings/{binding['binding_id']}"
    assert checked(api.send("owner", "GET", binding_path + "/revisions/2"))["status"] == "revoked"


def test_cascade_audit_failure_rolls_back_user_status_and_binding_revision(api, monkeypatch):
    project = api.project()
    member = api.member(project)
    binding = api.binding(project, api.agent())
    original = api.client.app.state.auth.audit

    def broken_audit(conn, actor_id, action, target_id, **kwargs):
        original(conn, actor_id, action, target_id, **kwargs)
        if action == "binding.revoked":
            raise RuntimeError("Failed audit")

    monkeypatch.setattr(api.client.app.state.auth, "audit", broken_audit)
    checked(api.send("admin", "PATCH", f"/admin/users/{member['user_id']}",
                     json={"status": "disabled", "expected_version": 1}), 500)
    checked(api.send("member", "GET", "/me"))
    path = f"/projects/{project['project_id']}/agent-bindings/{binding['binding_id']}"
    assert checked(api.send("owner", "GET", path)) == binding
    checked(api.send("owner", "GET", path + "/revisions/2"), 404)


def test_expired_session_is_rejected_for_reads_and_writes(api, monkeypatch):
    expires = checked(api.send("member", "GET", "/me"))["expires_at"]
    monkeypatch.setattr(api.client.app.state.auth, "clock", lambda: expires + 1)
    checked(api.send("member", "GET", "/me"), 401)
    checked(api.send("member", "POST", "/projects", json={"name": "Expired"}), 401)


def test_role_revocation_is_effective_on_next_write(api):
    project = api.project()
    manager = api.member(project, "manager", "manager")
    pid = project["project_id"]
    checked(api.send("owner", "PATCH", f"/projects/{pid}/members/{manager['user_id']}",
                     json={"access_role": "member", "status": "active", "expected_version": 1}))
    checked(api.send("manager", "POST", f"/projects/{pid}/members", json={
        "user_id": api.identities["member"]["user"]["user_id"], "access_role": "member"}), 403)


def test_admin_status_change_disables_sessions_without_renaming_or_self_disable(api):
    project = api.project()
    member = api.member(project)
    binding = api.binding(project, api.agent())
    uid = member["user_id"]
    path = f"/admin/users/{uid}"
    body = {"status": "disabled", "expected_version": 1}
    checked(api.send("owner", "PATCH", path, json=body), 403)
    checked(api.send("admin", "PATCH", path, json={**body, "expected_version": 2}), 409)
    changed = checked(api.send("admin", "PATCH", path, json=body))
    assert changed["display_name"] == api.identities["member"]["user"]["display_name"]
    assert changed["version"] == 2
    checked(api.send("member", "GET", "/me"), 401)
    checked(api.send("member", "POST", "/projects", json={"name": "Blocked"}), 401)
    checked(api.send("admin", "PATCH", path, json={"status": "active", "expected_version": 2}))
    checked(api.send("member", "GET", "/me"), 401)
    binding_path = f"/projects/{project['project_id']}/agent-bindings/{binding['binding_id']}"
    assert checked(api.send("owner", "GET", binding_path))["status"] == "revoked"
    admin_uid = api.identities["admin"]["user"]["user_id"]
    checked(api.send("admin", "PATCH", f"/admin/users/{admin_uid}", json=body), 403)
    audit = checked(api.send("admin", "GET", "/admin/audit"))
    assert {(event["target_id"], event["target_version"]) for event in audit
            if event["action"] == "user.status_changed"} == {(uid, 2), (uid, 3)}


@pytest.mark.parametrize("value", [True, False, 0, -1, 1.0, "1", None])
def test_expected_version_is_strict_positive_integer(api, value):
    project = api.project()
    checked(api.send("owner", "PATCH", f"/projects/{project['project_id']}", json={
        "name": "Changed", "status": "active", "expected_version": value}), 422)
    assert checked(api.send("owner", "GET", f"/projects/{project['project_id']}")) == {
        **project, "access_role": "owner"}


@pytest.mark.parametrize("method,path,payload", [
    ("POST", "/auth/login", {"username": "ab", "password": PASSWORD}),
    ("POST", "/auth/login", {"username": "a" * 65, "password": PASSWORD}),
    ("POST", "/auth/login", {"username": "owner", "password": "short"}),
    ("POST", "/auth/login", {"username": "owner", "password": "x" * 1025}),
    ("POST", "/auth/login", {"username": "owner", "password": PASSWORD, "user_id": "forged"}),
    ("POST", "/auth/register", {"invitation_token": "private-token", "password": "short"}),
    ("POST", "/projects", {"name": ""}),
    ("POST", "/projects", {"name": "x" * 513}),
    ("POST", "/agents", {"name": "   "}),
    ("POST", "/auth/invitations", {"username": "invited", "display_name": ""}),
])
def test_validation_does_not_echo_inputs(api, method, path, payload, caplog):
    response = api.send("admin", method, path, json=payload)
    assert checked(response, 422) == {"detail": "Invalid request"}
    assert PASSWORD not in caplog.text and "private-token" not in caplog.text
    assert "input" not in response.json()


@pytest.mark.parametrize("field,value", [
    ("duty", "x" * 8193), ("allowed_tools", ["Read"] * 257),
    ("allowed_tools", ["Read", "Read"]), ("allowed_tools", ["x" * 129]),
    ("workspace_ref", "../other-project"), ("workspace_ref", "D:/private"),
    ("provider_ref", "https://user:password@provider"),
])
def test_binding_limits_and_resource_references_are_validated(api, field, value):
    project = api.project()
    member = api.member(project)
    agent = api.agent()
    payload = {**BINDING_FIELDS, "agent_id": agent["agent_id"], "member_user_id": member["user_id"], field: value}
    checked(api.send("owner", "POST", f"/projects/{project['project_id']}/agent-bindings", json=payload), 422)
    assert checked(api.send("owner", "GET", f"/projects/{project['project_id']}/agent-bindings")) == []


def test_binding_owner_must_equal_member(api):
    project = api.project()
    member = api.member(project)
    wrong_agent = api.agent("owner")
    checked(api.send("owner", "POST", f"/projects/{project['project_id']}/agent-bindings", json={
        **BINDING_FIELDS, "agent_id": wrong_agent["agent_id"], "member_user_id": member["user_id"]}), 422)


@pytest.mark.parametrize("limit", ["0", "201", "-1", "1.5", "secret-value"])
def test_audit_limit_bounds(api, limit):
    checked(api.send("admin", "GET", f"/admin/audit?limit={limit}"), 422)
    project = api.project()
    checked(api.send("owner", "GET", f"/projects/{project['project_id']}/audit?limit={limit}"), 422)


def test_audit_is_scoped_and_bounded(api):
    first, second = api.project(), api.project(name="B")
    for project in (first, second):
        pid = project["project_id"]
        rows = checked(api.send("owner", "GET", f"/projects/{pid}/audit?limit=1"))
        assert len(rows) == 1 and rows[0]["project_id"] == pid
    assert len(checked(api.send("admin", "GET", "/admin/audit?limit=1"))) == 1


def test_audit_failure_rolls_back_business_write_and_hides_sql(api, monkeypatch, caplog):
    original = api.client.app.state.auth.audit

    def broken_audit(conn, actor_id, action, target_id, **kwargs):
        original(conn, actor_id, action, target_id, **kwargs)
        if action == "project.created":
            raise sqlite3.OperationalError("SELECT secret FROM passwords; " + PASSWORD)

    monkeypatch.setattr(api.client.app.state.auth, "audit", broken_audit)
    response = api.send("owner", "POST", "/projects", json={"name": "Must roll back"})
    checked(response, 500)
    assert PASSWORD not in response.text + caplog.text
    assert "SELECT secret" not in response.text + caplog.text
    assert checked(api.send("owner", "GET", "/projects")) == []
    with api.database.read() as conn:
        assert conn.execute("SELECT count(*) FROM collaboration_audit WHERE action='project.created'").fetchone()[0] == 0


def test_mutations_authenticate_and_audit_in_same_transaction(api, monkeypatch):
    original_audit = api.client.app.state.auth.audit
    original_create = CollaborationStore.create_project
    observed = {}

    def checked_create(store, name, owner_id):
        assert store.connection is not None and store.connection.in_transaction
        observed["connection"] = store.connection
        return original_create(store, name, owner_id)

    def checked_audit(conn, actor_id, action, target_id, **kwargs):
        if action == "project.created":
            assert conn is observed["connection"] and conn.in_transaction
        return original_audit(conn, actor_id, action, target_id, **kwargs)

    monkeypatch.setattr(CollaborationStore, "create_project", checked_create)
    monkeypatch.setattr(api.client.app.state.auth, "audit", checked_audit)
    api.project()
    assert "connection" in observed


def test_unexpected_exception_and_malformed_json_never_echo_secrets(api, monkeypatch, caplog):
    malformed = api.send(None, "POST", "/auth/login", content='{"password":"secret-not-json",',
                         headers={"Content-Type": "application/json"})
    checked(malformed, 422)
    assert "secret-not-json" not in malformed.text + caplog.text

    def broken_login(*args, **kwargs):
        raise RuntimeError(PASSWORD + " SELECT password_hash")

    monkeypatch.setattr(api.client.app.state.auth, "login", broken_login)
    response = api.send(None, "POST", "/auth/login", json={"username": "owner", "password": PASSWORD})
    checked(response, 500)
    assert PASSWORD not in response.text + caplog.text
    assert "password_hash" not in response.text + caplog.text


def test_login_peer_ignores_proxy_headers_and_rate_limit_cannot_be_spoofed(api):
    for index in range(10):
        response = api.send(None, "POST", "/auth/login", json={
            "username": "owner", "password": "wrong-password-for-test"},
            headers={"X-Forwarded-For": f"198.51.100.{index}", "X-Real-IP": f"192.0.2.{index}"})
        checked(response, 401)
    checked(api.send(None, "POST", "/auth/login", json={"username": "owner", "password": PASSWORD},
                     headers={"X-Forwarded-For": "203.0.113.100"}), 429)


def test_cli_bootstrap_uses_getpass_twice_and_only_prints_id(tmp_path, monkeypatch, capsys):
    prompts = []

    def password(prompt):
        prompts.append(prompt)
        return PASSWORD

    monkeypatch.setattr(cli, "getpass", password)
    assert cli.main(["bootstrap", "--data-dir", str(tmp_path), "--username", "first-admin",
                     "--display-name", "First Admin"]) == 0
    output = capsys.readouterr()
    assert len(prompts) == 2 and not output.err
    uid = output.out.strip()
    assert len(uid) == 32 and "\n" not in uid and PASSWORD not in output.out
    user = CollaborationStore(Database(tmp_path / "metadata.sqlite3")).get_user(uid)
    assert user.display_name == "First Admin"
    with TestClient(create_app(AppConfig(tmp_path, public_origin=ORIGIN)), base_url=ORIGIN) as client:
        assert checked(client.post(PREFIX + "/auth/login", json={
            "username": "first-admin", "password": PASSWORD}, headers={"Origin": ORIGIN}))["is_admin"] is True


def test_cli_rejects_mismatched_passwords_without_initializing(tmp_path, monkeypatch, capsys):
    passwords = iter((PASSWORD, PASSWORD + "mismatch"))
    monkeypatch.setattr(cli, "getpass", lambda prompt: next(passwords))
    directory = tmp_path / "untouched"
    with pytest.raises(SystemExit) as error:
        cli.main(["bootstrap", "--data-dir", str(directory), "--username", "admin", "--display-name", "Admin"])
    assert error.value.code == 2
    output = capsys.readouterr()
    assert not output.out and PASSWORD not in output.err and not directory.exists()


@pytest.mark.parametrize("args", [
    ["serve"], ["bootstrap", "--username", "admin", "--display-name", "Admin"],
    ["bootstrap", "--data-dir", "unused", "--username", "admin", "--display-name", "Admin", "--password", PASSWORD],
])
def test_cli_requires_explicit_data_dir_and_has_no_password_argument(args, capsys):
    with pytest.raises(SystemExit) as error:
        cli.main(args)
    assert error.value.code == 2
    output = capsys.readouterr()
    assert PASSWORD not in output.out + output.err


@pytest.mark.parametrize("options,host,port,origin", [
    ([], "127.0.0.1", 7400, "http://127.0.0.1:7400"),
    (["--host", "0.0.0.0", "--port", "7443", "--origin", ORIGIN], "0.0.0.0", 7443, ORIGIN),
])
def test_cli_serve_disables_proxy_headers_without_starting_network(tmp_path, monkeypatch, options, host, port, origin):
    import uvicorn

    calls = []
    monkeypatch.setattr(uvicorn, "run", lambda app, **kwargs: calls.append((app, kwargs)))
    directory = tmp_path / "untouched"
    assert cli.main(["serve", "--data-dir", str(directory), *options]) == 0
    assert len(calls) == 1
    assert calls[0][1] == {"host": host, "port": port, "proxy_headers": False}
    assert not directory.exists()
    with TestClient(calls[0][0], base_url=origin) as client:
        checked(client.get(PREFIX + "/me"), 401)


@pytest.mark.parametrize("ttl", [True, 0, 59, 86401, "3600"])
def test_invalid_session_lifetime_has_no_disk_side_effects(tmp_path, ttl):
    directory = tmp_path / "untouched"
    with pytest.raises(ValueError):
        AppConfig(directory, public_origin=ORIGIN, session_ttl=ttl)
    assert not directory.exists()


@pytest.mark.parametrize("host", ["0.0.0.0", "192.0.2.1"])
def test_cli_plain_http_cannot_bind_external_interface(tmp_path, monkeypatch, host):
    import uvicorn
    calls = []
    monkeypatch.setattr(uvicorn, "run", lambda *args, **kwargs: calls.append(args))
    directory = tmp_path / "untouched"
    with pytest.raises(SystemExit) as error:
        cli.main(["serve", "--data-dir", str(directory), "--host", host])
    assert error.value.code == 2
    assert calls == []
    assert not directory.exists()


def test_invalid_unicode_input_returns_safe_error(api):
    response = api.send(None, "POST", "/auth/login", content=json.dumps({
        "username": "owner", "password": chr(0xD800) * 12}), headers={"Content-Type": "application/json"})
    checked(response, 401)
    response = api.send("owner", "POST", "/projects", content=json.dumps({"name": chr(0xD800)}),
                        headers={"Content-Type": "application/json"})
    checked(response, 422)


def machine_claim(api, node_token, claim_token, **overrides):
    policy = dict(workspace_ref="workspace-a", provider_ref="provider-a",
                  allowed_tools=["Read"], env_allowlist=["PYTHONPATH"])
    policy.update(overrides.pop("policy", {}))
    body = dict(protocol_version="1", capacity=2, isolation_mode="container",
                isolation_attestation="podman sandbox", policy=policy, claim_token=claim_token)
    body.update(overrides)
    return api.send(None, "POST", "/machine/instances/claim", json=body,
                    headers={"X-Node-Token": node_token})


def test_instance_registration_claim_and_machine_round_trip(api):
    project = api.project()
    pid = project["project_id"]
    api.member(project)
    binding = api.binding(project, api.agent())
    node, node_token = api.client.app.state.instances.enroll_node("node-1")
    path = f"/projects/{pid}/agent-bindings/{binding['binding_id']}/instances"
    checked(api.send("member", "POST", path, json={"node_id": node.node_id}), 403)
    checked(api.send("outsider", "POST", path, json={"node_id": node.node_id}), 404)
    created = checked(api.send("owner", "POST", path, json={"node_id": node.node_id}), 201)
    assert created["status"] == "pending" and created["version"] == 1
    assert len(created["claim_token"]) == 43
    checked(api.send("owner", "POST", path, json={"node_id": node.node_id}), 409)
    listing = checked(api.send("member", "GET", path))
    assert [item["instance_id"] for item in listing] == [created["instance_id"]]
    assert "claim_token" not in json.dumps(listing)
    checked(api.send("outsider", "GET", path), 404)
    claimed = checked(machine_claim(api, node_token, created["claim_token"]), 201)
    assert claimed["status"] == "active" and claimed["project_id"] == pid
    assert claimed["binding_version"] == 1 and "claim_token" not in claimed
    checked(machine_claim(api, node_token, created["claim_token"]), 409)
    token = claimed["instance_token"]
    beat = checked(api.send(None, "POST", "/machine/instances/heartbeat",
                            headers={"X-Instance-Token": token}))
    assert (beat["instance_id"], beat["status"]) == (created["instance_id"], "active")
    me = checked(api.send(None, "GET", "/machine/instances/me",
                          headers={"X-Instance-Token": token}))
    assert me["instance_id"] == created["instance_id"] and me["project_id"] == pid
    checked(api.send(None, "GET", "/me", headers={"X-Instance-Token": token}), 401)
    checked(api.send(None, "GET", "/projects", headers={"X-Instance-Token": token}), 401)


def test_instance_verification_is_owner_only_and_disable_is_manager_level(api):
    project = api.project()
    pid = project["project_id"]
    api.member(project)
    api.member(project, "manager", "manager")
    binding = api.binding(project, api.agent())
    node, node_token = api.client.app.state.instances.enroll_node("node-1")
    path = f"/projects/{pid}/agent-bindings/{binding['binding_id']}/instances"
    created = checked(api.send("owner", "POST", path, json={"node_id": node.node_id}), 201)
    claimed = checked(machine_claim(api, node_token, created["claim_token"]), 201)
    verify_path = f"{path}/{created['instance_id']}/verify"
    checked(api.send("member", "POST", verify_path,
                     json={"expected_version": claimed["version"]}), 403)
    checked(api.send("manager", "POST", verify_path,
                     json={"expected_version": claimed["version"]}), 403)
    checked(api.send("outsider", "POST", verify_path,
                     json={"expected_version": claimed["version"]}), 404)
    verified = checked(api.send("owner", "POST", verify_path,
                                json={"expected_version": claimed["version"]}))
    assert verified["isolation_verified"] == 1 and verified["verified_at"] is not None
    checked(api.send("owner", "POST", verify_path,
                     json={"expected_version": verified["version"]}), 409)
    disable_path = f"{path}/{created['instance_id']}/disable"
    checked(api.send("member", "POST", disable_path,
                     json={"expected_version": verified["version"]}), 403)
    disabled = checked(api.send("manager", "POST", disable_path,
                                json={"expected_version": verified["version"]}))
    assert disabled["status"] == "disabled"
    checked(api.send(None, "POST", "/machine/instances/heartbeat",
                     headers={"X-Instance-Token": claimed["instance_token"]}), 401)


@pytest.mark.parametrize("operation", ["revoke", "agent", "member", "project", "user"])
def test_instance_cascade_disable_is_audited_and_blocks_heartbeat(api, operation):
    project = api.project()
    pid = project["project_id"]
    member = api.member(project)
    agent = api.agent()
    binding = api.binding(project, agent)
    node, node_token = api.client.app.state.instances.enroll_node("node-1")
    path = f"/projects/{pid}/agent-bindings/{binding['binding_id']}/instances"
    created = checked(api.send("owner", "POST", path, json={"node_id": node.node_id}), 201)
    claimed = checked(machine_claim(api, node_token, created["claim_token"]), 201)
    actor, method, target, body = {
        "revoke": ("owner", "POST",
                   f"/projects/{pid}/agent-bindings/{binding['binding_id']}/revoke",
                   {"expected_version": 1}),
        "agent": ("member", "PATCH", f"/agents/{agent['agent_id']}",
                  {"name": "Disabled Agent", "status": "disabled", "expected_version": 1}),
        "member": ("owner", "PATCH", f"/projects/{pid}/members/{member['user_id']}",
                   {"access_role": "member", "status": "removed", "expected_version": 1}),
        "project": ("owner", "PATCH", f"/projects/{pid}",
                    {"name": "Archived", "status": "archived", "expected_version": 1}),
        "user": ("admin", "PATCH", f"/admin/users/{member['user_id']}",
                 {"status": "disabled", "expected_version": 1}),
    }[operation]
    checked(api.send(actor, method, target, json=body))
    listing = checked(api.send("owner", "GET", path))
    assert listing[0]["status"] == "disabled"
    checked(api.send(None, "POST", "/machine/instances/heartbeat",
                     headers={"X-Instance-Token": claimed["instance_token"]}), 401)
    events = checked(api.send("owner", "GET", f"/projects/{pid}/audit"))
    disabled = [event for event in events if event["action"] == "instance.disabled"]
    assert len(disabled) == 1
    assert disabled[0]["target_id"] == created["instance_id"]


@pytest.mark.parametrize("header", ["X-Node-Token", "X-Instance-Token", "X-CSRF-Token"])
def test_duplicate_machine_token_headers_are_rejected(api, header):
    headers = [("Host", "testserver"), ("Origin", ORIGIN),
               (header, "a" * 43), (header, "b" * 43)]
    response = api.client.request("POST", PREFIX + "/machine/instances/heartbeat",
                                  headers=headers)
    checked(response, 403)


def test_machine_claim_failure_modes_do_not_leak_state(api, monkeypatch):
    project = api.project()
    pid = project["project_id"]
    api.member(project)
    binding = api.binding(project, api.agent())
    node, node_token = api.client.app.state.instances.enroll_node("node-1")
    path = f"/projects/{pid}/agent-bindings/{binding['binding_id']}/instances"
    created = checked(api.send("owner", "POST", path, json={"node_id": node.node_id}), 201)
    checked(machine_claim(api, "bad", created["claim_token"]), 401)
    checked(machine_claim(api, "x" * 43, created["claim_token"]), 401)
    checked(machine_claim(api, node_token, "y" * 43), 409)
    checked(machine_claim(api, node_token, created["claim_token"],
                          policy={"workspace_ref": "workspace-b"}), 422)
    checked(machine_claim(api, node_token, created["claim_token"],
                          policy={"allowed_tools": ["Read", "Write"]}), 422)
    checked(machine_claim(api, node_token, created["claim_token"],
                          policy={"env_allowlist": ["OPENAI_API_KEY"]}), 422)
    checked(machine_claim(api, node_token, created["claim_token"],
                          isolation_mode="docker"), 422)
    checked(machine_claim(api, node_token, created["claim_token"]), 201)
    checked(machine_claim(api, node_token, created["claim_token"]), 409)
    node2, _ = api.client.app.state.instances.enroll_node("node-2")
    monkeypatch.setattr(api.client.app.state.instances, "clock", lambda: time.time() + 100)
    late = checked(api.send("owner", "POST", path, json={"node_id": node2.node_id}), 201)
    monkeypatch.setattr(api.client.app.state.instances, "clock",
                        lambda: time.time() + 100 + 86401)
    checked(machine_claim(api, node_token, late["claim_token"]), 409)


def create_requirement(api, project, actor="member", *, title="需求文档", content=None):
    return checked(api.send(actor, "POST", f"/projects/{project['project_id']}/requirements",
                            json={"title": title, "content": content}), 201)


def reviews_path(project, requirement_id):
    return f"/projects/{project['project_id']}/requirements/{requirement_id}/reviews"


def test_requirement_draft_review_and_approval_over_http(api):
    project = api.project()
    pid = project["project_id"]
    api.member(project)
    api.member(project, "manager", "manager")
    content = "# 需求文档\n第一版"
    created = create_requirement(api, project, content=content)
    assert created["status"] == "draft" and created["version"] == 2
    requirement_id, revision_id = created["requirement_id"], created["current_revision_id"]
    for actor in ("owner", "manager", "outsider"):
        checked(api.send(actor, "GET", f"/projects/{pid}/requirements/{requirement_id}"), 404)
    listing = checked(api.send("member", "GET", f"/projects/{pid}/requirements"))
    assert [item["requirement_id"] for item in listing] == [requirement_id]
    assert checked(api.send("manager", "GET", f"/projects/{pid}/requirements")) == []
    messages_path = f"/projects/{pid}/requirements/{requirement_id}/messages"
    checked(api.send("manager", "GET", messages_path), 404)
    messages = checked(api.send("member", "POST", messages_path,
                                json={"content": "请补充验收标准"}), 201)
    assert [message["role"] for message in messages] == ["user"]
    requested = checked(api.send("member", "POST", reviews_path(project, requirement_id),
                                 json={"action": "request", "revision_id": revision_id,
                                       "reviewer_user_id": api.identities["manager"]["user"]["user_id"],
                                       "expected_version": 2}))
    assert requested["requirement"]["status"] == "in_review"
    assert requested["review"]["decision"] == "requested"
    checked(api.send("member", "POST", reviews_path(project, requirement_id),
                     json={"action": "approve", "revision_id": revision_id,
                           "expected_version": 3}), 403)
    inbox = checked(api.send("manager", "GET", "/inbox"))
    assert [item["item_type"] for item in inbox] == ["requirement_review"]
    assert checked(api.send("owner", "GET", "/inbox")) == []
    revision = checked(api.send("manager", "GET",
                                f"/projects/{pid}/requirements/{requirement_id}/revisions/{revision_id}"))
    assert revision["content"] == content and revision["content_size"] == len(content.encode())
    checked(api.send("manager", "GET", messages_path))
    approved = checked(api.send("manager", "POST", reviews_path(project, requirement_id),
                                json={"action": "approve", "revision_id": revision_id,
                                      "expected_version": 3}))
    assert approved["requirement"]["status"] == "approved"
    assert approved["requirement"]["approved_revision_id"] == revision_id
    assert checked(api.send("owner", "GET",
                            f"/projects/{pid}/requirements/{requirement_id}"))["status"] == "approved"
    for actor in ("owner", "manager"):
        checked(api.send(actor, "GET", messages_path), 404)
    checked(api.send("outsider", "GET", f"/projects/{pid}/requirements/{requirement_id}"), 404)
    saved = checked(api.send("member", "POST",
                             f"/projects/{pid}/requirements/{requirement_id}/revisions",
                             json={"content": content + "\n第二版", "expected_version": 4}), 201)
    assert saved["requirement"]["status"] == "draft"
    checked(api.send("manager", "GET", f"/projects/{pid}/requirements/{requirement_id}"), 404)


def test_requirement_approval_is_idempotent_and_audited_once(api):
    project = api.project()
    pid = project["project_id"]
    api.member(project)
    api.member(project, "manager", "manager")
    created = create_requirement(api, project, content="唯一原文")
    requirement_id, revision_id = created["requirement_id"], created["current_revision_id"]
    checked(api.send("member", "POST", reviews_path(project, requirement_id),
                     json={"action": "request", "revision_id": revision_id,
                           "reviewer_user_id": api.identities["manager"]["user"]["user_id"],
                           "expected_version": 2}))
    first = checked(api.send("manager", "POST", reviews_path(project, requirement_id),
                             json={"action": "approve", "revision_id": revision_id,
                                   "expected_version": 3}))
    replay = checked(api.send("manager", "POST", reviews_path(project, requirement_id),
                              json={"action": "approve", "revision_id": revision_id,
                                    "expected_version": 3}))
    assert replay["review"]["review_id"] == first["review"]["review_id"]
    assert replay["requirement"]["version"] == first["requirement"]["version"] == 4
    events = checked(api.send("owner", "GET", f"/projects/{pid}/audit"))
    approvals = [event for event in events if event["action"] == "requirement.review_approved"]
    assert len(approvals) == 1
    reviews = checked(api.send("manager", "GET", reviews_path(project, requirement_id)))
    assert [item["decision"] for item in reviews] == ["requested", "approved"]


def test_requirement_long_content_round_trips_and_lists_omit_text(api):
    project = api.project()
    pid = project["project_id"]
    api.member(project)
    api.member(project, "manager", "manager")
    content = ("中文需求段落" * 60000) + "\n" + ("ascii-unit " * 20000) + "尾"
    raw = content.encode("utf-8")
    assert len(raw) > 1024 * 1024
    created = create_requirement(api, project, content=content)
    requirement_id, first_revision = created["requirement_id"], created["current_revision_id"]
    revision = checked(api.send(
        "member", "GET",
        f"/projects/{pid}/requirements/{requirement_id}/revisions/{first_revision}"))
    assert revision["content"].encode("utf-8") == raw
    assert revision["content_hash"] == hashlib.sha256(raw).hexdigest()
    assert revision["content_size"] == len(raw)
    listing_response = api.send(
        "member", "GET", f"/projects/{pid}/requirements/{requirement_id}/revisions")
    listing = checked(listing_response)
    assert all("content" not in item for item in listing)
    assert "中文需求段落" not in listing_response.text
    saved = checked(api.send(
        "member", "POST", f"/projects/{pid}/requirements/{requirement_id}/revisions",
        json={"content": content + "\n新增验收标准", "expected_version": 2}), 201)
    diff = checked(api.send(
        "member", "GET",
        f"/projects/{pid}/requirements/{requirement_id}/revisions/diff",
        params={"from_revision_id": first_revision,
                "to_revision_id": saved["revision"]["revision_id"]}))
    assert set(diff) == {"requirement_id", "from_revision_id", "to_revision_id", "diff"}
    assert diff["diff"].startswith("--- ") and "+新增验收标准" in diff["diff"]


def test_requirement_http_boundaries_and_validation(api):
    project = api.project()
    pid = project["project_id"]
    api.member(project)
    api.member(project, "manager", "manager")
    created = create_requirement(api, project, content="原文")
    requirement_id, revision_id = created["requirement_id"], created["current_revision_id"]
    path = f"/projects/{pid}/requirements"
    checked(api.send("member", "POST", path, json={"title": "无 CSRF"},
                     headers={"X-CSRF-Token": None}), 403)
    checked(api.send(None, "POST", path, json={"title": "无会话"}), 401)
    checked(api.send("member", "POST", reviews_path(project, requirement_id),
                     json={"action": "request", "revision_id": revision_id,
                           "expected_version": 2}), 422)
    checked(api.send("member", "POST", reviews_path(project, requirement_id),
                     json={"action": "sign", "revision_id": revision_id,
                           "expected_version": 2}), 422)
    checked(api.send("member", "POST", f"{path}/{requirement_id}/revisions",
                     json={"content": "x", "expected_version": 0}), 422)
    checked(api.send("member", "POST", f"{path}/{requirement_id}/revisions",
                     json={"content": "", "expected_version": 2}), 422)
    checked(api.send("member", "POST", f"{path}/{requirement_id}/messages",
                     json={"content": ""}), 422)
    checked(api.send("outsider", "GET", path), 404)
    checked(api.send("outsider", "POST", path, json={"title": "越权"}), 404)
    checked(api.send("outsider", "GET", path,
                     headers={"X-API-Key": "legacy-shared-secret"}), 404)


def test_requirement_flow_creates_no_execution_rows_over_http(api):
    project = api.project()
    pid = project["project_id"]
    api.member(project)
    api.member(project, "manager", "manager")

    def counts():
        with api.database.read() as conn:
            return {name: conn.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0]
                    for name in ("nodes", "execution_instances", "agent_bindings",
                                 "binding_revisions")}

    before = counts()
    created = create_requirement(api, project, content="零执行")
    requirement_id, revision_id = created["requirement_id"], created["current_revision_id"]
    assert counts() == before
    checked(api.send("member", "POST", reviews_path(project, requirement_id),
                     json={"action": "request", "revision_id": revision_id,
                           "reviewer_user_id": api.identities["manager"]["user"]["user_id"],
                           "expected_version": 2}))
    assert counts() == before
    checked(api.send("manager", "POST", reviews_path(project, requirement_id),
                     json={"action": "approve", "revision_id": revision_id,
                           "expected_version": 3}))
    assert counts() == before


# --- T06: plans, scheduler, outbox and budgets --------------------------------

def approval_setup(api, acceptance=("第一条验收",)):
    project = api.project()
    member = api.member(project)
    api.member(project, "manager", "manager")
    binding = api.binding(project, api.agent())
    created = checked(api.send("member", "POST",
                               f"/projects/{project['project_id']}/requirements",
                               json={"title": "需求文档", "content": "# 需求原文",
                                     "acceptance_items": list(acceptance)}), 201)
    requirement_id, revision_id = created["requirement_id"], created["current_revision_id"]
    checked(api.send("member", "POST", reviews_path(project, requirement_id),
                     json={"action": "request", "revision_id": revision_id,
                           "reviewer_user_id": api.identities["manager"]["user"]["user_id"],
                           "expected_version": created["version"]}))
    checked(api.send("manager", "POST", reviews_path(project, requirement_id),
                     json={"action": "approve", "revision_id": revision_id,
                           "expected_version": created["version"] + 1}))
    return project, member, binding, requirement_id


def wire_planner(api, binding, member_uid, *, estimate=2, depends_on=()):
    candidate = {"items": [{
        "key": "A", "title": "任务 A", "objective": "完成目标", "scope": "范围内",
        "non_goals": "不做的事", "duty": BINDING_FIELDS["duty"],
        "assignee_user_id": member_uid, "binding_id": binding["binding_id"],
        "acceptance_ids": ["AC-001"], "estimated_units": estimate,
        "depends_on": list(depends_on)}]}
    api.client.app.state.planning.planner = lambda context: candidate


def plans_path(project, requirement_id):
    return f"/projects/{project['project_id']}/requirements/{requirement_id}/plans"


def test_plan_generation_listing_and_replay_over_http(api):
    project, member, binding, requirement_id = approval_setup(api)
    pid = project["project_id"]
    path = plans_path(project, requirement_id)
    checked(api.send("member", "POST", path), 403)
    checked(api.send("outsider", "POST", path), 404)
    checked(api.send("manager", "POST", path, headers={"X-CSRF-Token": None}), 403)
    checked(api.send("outsider", "GET", path), 404)
    checked(api.send("manager", "POST", path), 503)  # no planner wired yet
    wire_planner(api, binding, member["user_id"])
    created = checked(api.send("manager", "POST", path), 201)
    plan = created["plan"]
    assert plan["status"] == "published" and plan["total_units"] == 2
    assert plan["report"]["item_count"] == 1 and plan["report"]["problems"] == []
    assert plan["created_by"] == api.identities["manager"]["user"]["user_id"]
    assert plan["published_at"] is not None
    assert [item["key"] for item in created["work_items"]] == ["A"]
    assert created["work_items"][0]["status"] == "ready"
    assert created["work_items"][0]["assignee_user_id"] == member["user_id"]
    assert created["dependencies"] == []
    assert checked(api.send("member", "GET",
                            f"/projects/{pid}/requirements/{requirement_id}"))["status"] == \
        "executing"
    listing = checked(api.send("member", "GET", path))
    assert [entry["plan_id"] for entry in listing] == [plan["plan_id"]]
    detail = checked(api.send("member", "GET", path + f"/{plan['plan_id']}"))
    assert detail == created
    checked(api.send("member", "GET", path + "/" + "0" * 32), 404)
    checked(api.send("manager", "POST", path), 409)


def test_scheduler_tick_and_outbox_over_http(api):
    project, member, binding, requirement_id = approval_setup(api)
    pid = project["project_id"]
    wire_planner(api, binding, member["user_id"])
    tick_path = f"/projects/{pid}/scheduler/tick"
    checked(api.send("member", "POST", tick_path), 403)
    checked(api.send("outsider", "POST", tick_path), 404)
    tick = checked(api.send("manager", "POST", tick_path))
    assert tick == {"claimed": 1, "delivered": 1, "deferred": 0, "work_items_changed": 1,
                    "attempts_reclaimed": 0}
    assert checked(api.send("member", "GET",
                            f"/projects/{pid}/requirements/{requirement_id}"))["status"] == \
        "executing"
    plans = checked(api.send("member", "GET", plans_path(project, requirement_id)))
    assert [entry["status"] for entry in plans] == ["published"]
    outbox_path = f"/projects/{pid}/outbox"
    checked(api.send("member", "GET", outbox_path), 403)
    checked(api.send("outsider", "GET", outbox_path), 404)
    events = {event["topic"]: event for event in
              checked(api.send("manager", "GET", outbox_path))}
    assert events["requirement.approved"]["delivered_at"] is not None
    assert events["plan.published"]["delivered_at"] is None
    assert events["plan.published"]["payload"] == {
        "plan_id": plans[0]["plan_id"], "requirement_id": requirement_id,
        "revision_id": plans[0]["revision_id"]}
    checked(api.send("manager", "POST", tick_path))
    events = {event["topic"]: event for event in
              checked(api.send("manager", "GET", outbox_path))}
    assert all(event["delivered_at"] is not None for event in events.values())


def test_budget_scopes_versions_and_publication_gate_over_http(api):
    project, member, binding, requirement_id = approval_setup(api)
    pid = project["project_id"]
    wire_planner(api, binding, member["user_id"])
    path = plans_path(project, requirement_id)
    budget_path = f"/projects/{pid}/budget"
    checked(api.send("member", "PUT", budget_path, json={"limit_units": 1}), 403)
    checked(api.send("outsider", "PUT", budget_path, json={"limit_units": 1}), 404)
    checked(api.send("manager", "PUT", budget_path, json={"limit_units": 1},
                     headers={"X-CSRF-Token": None}), 403)
    for body in ({"limit_units": -1}, {"limit_units": "5"}, {"limit_units": True},
                 {"limit_units": 1, "expected_version": 0}):
        checked(api.send("manager", "PUT", budget_path, json=body), 422)
    first = checked(api.send("manager", "PUT", budget_path, json={"limit_units": 1}))
    assert (first["scope"], first["ref_id"], first["limit_units"], first["used_units"],
            first["remaining_units"], first["version"]) == ("project", pid, 1, 0, 1, 1)
    blocked = checked(api.send("manager", "POST", path), 201)["plan"]
    assert blocked["status"] == "blocked"
    assert blocked["report"]["problems"] == ["budget_exceeded:project"]
    assert checked(api.send("member", "GET",
                            f"/projects/{pid}/requirements/{requirement_id}"))["status"] == \
        "planning_blocked"
    checked(api.send("manager", "PUT", budget_path,
                     json={"limit_units": 10, "expected_version": 7}), 409)
    updated = checked(api.send("manager", "PUT", budget_path,
                               json={"limit_units": 10, "expected_version": 1}))
    assert updated["version"] == 2
    published = checked(api.send("manager", "POST", path), 201)["plan"]
    assert published["status"] == "published" and published["total_units"] == 2
    accounts = checked(api.send("member", "GET", budget_path))["accounts"]
    assert [(entry["scope"], entry["limit_units"], entry["used_units"],
             entry["remaining_units"], entry["version"]) for entry in accounts] == \
        [("project", 10, 2, 8, 2)]
    requirement_budget = f"/projects/{pid}/requirements/{requirement_id}/budget"
    checked(api.send("manager", "PUT", requirement_budget, json={"limit_units": 4}), 200)
    scoped = checked(api.send("member", "GET", budget_path))["accounts"]
    assert [(entry["scope"], entry["limit_units"]) for entry in scoped] == \
        [("project", 10), ("requirement", 4)]
    checked(api.send("manager", "PUT",
                     f"/projects/{pid}/requirements/{'0' * 32}/budget",
                     json={"limit_units": 4}), 404)
    checked(api.send("owner", "PUT", "/admin/budget", json={"limit_units": 100}), 403)
    global_account = checked(api.send("admin", "PUT", "/admin/budget", json={"limit_units": 100}))
    assert (global_account["scope"], global_account["limit_units"], global_account["version"]) == \
        ("global", 100, 1)


# --- T07: machine execution protocol over HTTP ---------------------------------

def machine_claims(source):
    data = source if isinstance(source, dict) else asdict(source)
    return {key: data[key] for key in ("input_hash", "control_epoch", "lease_epoch")}


def plan_candidate(binding, member_uid, key, acceptance_ids, *, estimate=2):
    return {"key": key, "title": f"任务 {key}", "objective": "完成目标", "scope": "范围内",
            "non_goals": "不做的事", "duty": BINDING_FIELDS["duty"],
            "assignee_user_id": member_uid, "binding_id": binding["binding_id"],
            "acceptance_ids": list(acceptance_ids), "estimated_units": estimate,
            "depends_on": []}


def machine_attempt(api, *, worker_url=None, acceptance=("第一条验收",), items=None):
    """Approved requirement, published plan, verified instance and one dispatched attempt."""
    project, member, binding, requirement_id = approval_setup(api, acceptance)
    pid = project["project_id"]
    if items is None:
        wire_planner(api, binding, member["user_id"])
    else:
        candidate = {"items": items(member, binding)}
        api.client.app.state.planning.planner = lambda context: candidate
    plan = checked(api.send("manager", "POST", plans_path(project, requirement_id)), 201)
    node, node_token = api.client.app.state.instances.enroll_node("machine-node")
    path = f"/projects/{pid}/agent-bindings/{binding['binding_id']}/instances"
    created = checked(api.send("owner", "POST",
                               f"/projects/{pid}/agent-bindings/{binding['binding_id']}/instances",
                               json={"node_id": node.node_id, "worker_url": worker_url}), 201)
    claimed = checked(machine_claim(api, node_token, created["claim_token"]), 201)
    token = claimed["instance_token"]
    checked(api.send(None, "POST", "/machine/instances/heartbeat",
                     headers={"X-Instance-Token": token}))
    checked(api.send("owner", "POST", f"{path}/{created['instance_id']}/verify",
                     json={"expected_version": claimed["version"]}))
    view = checked(api.send("manager", "POST",
                            dispatch_path(pid, plan["work_items"][0]["work_item_id"])))
    assert (view["status"], view["number"]) == ("assigned", 1)
    with api.database.read() as conn:
        attempt = CollaborationStore(api.database, connection=conn).get_attempt(
            pid, view["attempt_id"])
    return project, requirement_id, plan, attempt, token


def work_item_statuses(api, project, requirement_id, plan, actor="member"):
    detail = checked(api.send(actor, "GET",
                              plans_path(project, requirement_id) + f"/{plan['plan']['plan_id']}"))
    return [entry["status"] for entry in detail["work_items"]]


def dispatch_path(project_id, work_item_id):
    return f"/projects/{project_id}/work-items/{work_item_id}/dispatch"


def test_machine_protocol_pulls_and_reports_over_http(api):
    project, requirement_id, plan, attempt, token = machine_attempt(api)
    pid = project["project_id"]
    headers = {"X-Instance-Token": token}
    base = f"/machine/attempts/{attempt.attempt_id}"

    held = checked(api.send(None, "GET", "/machine/instances/me/attempts", headers=headers))
    assert [entry["attempt_id"] for entry in held] == [attempt.attempt_id]
    assert "package_json" not in json.dumps(held) and token not in json.dumps(held)
    assert work_item_statuses(api, project, requirement_id, plan) == ["running"]

    package = api.send(None, "GET", base + "/package", headers=headers)
    payload = checked(package)
    assert package.headers["x-input-hash"] == attempt.input_hash
    assert hashlib.sha256(package.content).hexdigest() == attempt.input_hash
    assert payload["documents"] == [{
        "kind": "requirement_text", "revision_id": attempt.revision_id,
        "content_hash": attempt.revision_hash, "size_bytes": len("# 需求原文".encode("utf-8")),
        "media_type": "text/markdown;charset=utf-8", "href": "requirement", "complete": True}]
    assert payload["subtask"]["acceptance"][0]["text"] == "第一条验收"
    assert payload["policy"]["allowed_tools"] == BINDING_FIELDS["allowed_tools"]
    assert (payload["policy"]["budget_units"], payload["policy"]["documents_are_untrusted_input"]) \
        == (2, True)

    original = api.send(None, "GET", base + "/requirement", headers=headers)
    assert original.status_code == 200
    assert original.headers["cache-control"] == "no-store"
    assert (original.headers["x-content-hash"], original.headers["x-revision-id"]) == (
        attempt.revision_hash, attempt.revision_id)
    assert original.content.decode("utf-8") == "# 需求原文"

    acked = checked(api.send(None, "POST", base + "/ack", json=machine_claims(attempt),
                             headers=headers))
    assert (acked["outcome"], acked["attempt"]["status"], acked["attempt"]["version"]) == (
        "accepted", "acked", attempt.version + 1)
    replay = checked(api.send(None, "POST", base + "/ack", json=machine_claims(acked["attempt"]),
                              headers=headers))
    assert (replay["outcome"], replay["attempt"]["version"]) == (
        "duplicate", acked["attempt"]["version"])
    started = checked(api.send(None, "POST", base + "/start",
                               json={**machine_claims(replay["attempt"]), "trace_id": "trace-1"},
                               headers=headers))
    assert (started["attempt"]["status"], started["attempt"]["trace_id"]) == ("running", "trace-1")
    renewed = checked(api.send(None, "POST", base + "/lease", json=machine_claims(started["attempt"]),
                               headers=headers))
    assert renewed["attempt"]["lease_epoch"] == started["attempt"]["lease_epoch"] + 1

    content = "交付说明".encode("utf-8")
    digest = hashlib.sha256(content).hexdigest()
    uploaded = checked(api.send(None, "POST", base + "/artifacts", json={
        **machine_claims(renewed["attempt"]), "name": "result.md", "media_type": "text/markdown",
        "sha256": digest, "content": base64.b64encode(content).decode("ascii")}, headers=headers),
        201)
    assert (uploaded["outcome"], uploaded["artifact"]["size_bytes"]) == ("accepted", len(content))
    listed = checked(api.send(None, "GET", base + "/artifacts", headers=headers))
    assert [entry["href"] for entry in listed] == [
        f"artifacts/{uploaded['artifact']['artifact_id']}"]
    downloaded = api.send(None, "GET", f"{base}/{listed[0]['href']}", headers=headers)
    assert downloaded.status_code == 200 and downloaded.headers["cache-control"] == "no-store"
    assert (downloaded.content, downloaded.headers["x-content-hash"]) == (content, digest)
    assert downloaded.headers["content-type"].startswith("text/markdown")

    body = {**machine_claims(uploaded["attempt"]), "status": "submitted", "summary": "完成",
            "usage_units": 2, "artifacts": [{"name": "result.md", "sha256": digest}]}
    result = checked(api.send(None, "POST", base + "/result", json=body, headers=headers))
    assert (result["outcome"], result["attempt"]["status"]) == ("accepted", "submitted")
    assert result["attempt"]["result_hash"] == hashlib.sha256(json.dumps(
        {"artifacts": [{"name": "result.md", "sha256": digest}], "summary": "完成"},
        ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
    again = checked(api.send(None, "POST", base + "/result", json=body,
                             headers=headers))
    assert (again["outcome"], again["attempt"]["version"]) == (
        "duplicate", result["attempt"]["version"])
    assert work_item_statuses(api, project, requirement_id, plan) == ["awaiting_owner_review"]


def test_machine_protocol_rejects_unauthenticated_forged_and_foreign_calls(api):
    project, _requirement_id, _plan, attempt, token = machine_attempt(api)
    base = f"/machine/attempts/{attempt.attempt_id}"
    headers = {"X-Instance-Token": token}
    claims = machine_claims(attempt)

    checked(api.send(None, "GET", base + "/package"), 401)
    checked(api.send("member", "GET", base + "/package"), 401)
    checked(api.send("member", "POST", base + "/ack", json=claims), 401)
    checked(api.send(None, "POST", base + "/ack", json={**claims, "input_hash": "z" * 64},
                     headers=headers), 422)
    checked(api.send(None, "POST", base + "/ack", json=claims), 401)
    checked(api.send(None, "POST", base + "/ack", json={**claims, "input_hash": "b" * 64},
                     headers=headers), 409)
    checked(api.send(None, "POST", base + "/ack", json={**claims, "lease_epoch": 99},
                     headers=headers), 409)
    checked(api.send(None, "GET", f"/machine/attempts/{'0' * 32}/package", headers=headers), 404)
    assert checked(api.send(None, "GET", "/machine/instances/me/attempts",
                            headers=headers))[0]["status"] == "assigned"

    other = api.project(name="Project B")
    api.member(other)
    other_binding = api.binding(other, api.agent())
    node, node_token = api.client.app.state.instances.enroll_node("foreign-node")
    created = checked(api.send("owner", "POST",
        f"/projects/{other['project_id']}/agent-bindings/{other_binding['binding_id']}/instances",
        json={"node_id": node.node_id}), 201)
    claimed = checked(machine_claim(api, node_token, created["claim_token"]), 201)
    foreign = {"X-Instance-Token": claimed["instance_token"]}
    assert checked(api.send(None, "GET", "/machine/instances/me/attempts", headers=foreign)) == []
    checked(api.send(None, "GET", base + "/package", headers=foreign), 404)
    checked(api.send(None, "GET", base + "/requirement", headers=foreign), 404)
    checked(api.send(None, "POST", base + "/ack", json=claims, headers=foreign), 404)
    assert checked(api.send(None, "GET", "/machine/instances/me/attempts",
                            headers=headers))[0]["status"] == "assigned"


def test_machine_cancel_closes_the_attempt_and_a_late_report_is_refused(api):
    project, requirement_id, plan, attempt, token = machine_attempt(api)
    headers = {"X-Instance-Token": token}
    base = f"/machine/attempts/{attempt.attempt_id}"
    started = checked(api.send(None, "POST", base + "/start", json=machine_claims(attempt),
                               headers=headers))
    cancelled = checked(api.send(None, "POST", base + "/cancel",
                                 json={**machine_claims(started["attempt"]), "reason": "节点停机"},
                                 headers=headers))
    assert (cancelled["outcome"], cancelled["attempt"]["status"]) == ("accepted", "cancelled")
    assert checked(api.send(None, "POST", base + "/cancel",
                            json={**machine_claims(cancelled["attempt"]), "reason": "节点停机"},
                            headers=headers))["outcome"] == "duplicate"
    digest = hashlib.sha256(b"late").hexdigest()
    late = machine_claims(cancelled["attempt"])
    checked(api.send(None, "POST", base + "/result",
                     json={**late, "status": "submitted", "summary": "迟到的结果"},
                     headers=headers), 409)
    checked(api.send(None, "POST", base + "/lease", json=late, headers=headers), 409)
    checked(api.send(None, "POST", base + "/artifacts", json={
        **late, "name": "late.md", "media_type": "text/plain", "sha256": digest,
        "content": base64.b64encode(b"late").decode("ascii")}, headers=headers), 409)
    assert work_item_statuses(api, project, requirement_id, plan) == ["waiting_agent"]
    # A closed attempt still serves its frozen inputs for reconciliation.
    assert api.send(None, "GET", base + "/package", headers=headers).status_code == 200
    assert api.send(None, "GET", base + "/requirement",
                    headers=headers).content == "# 需求原文".encode("utf-8")


# --- T07d: human decisions over HTTP -------------------------------------------

def delivered(api, *, usage_units=3):
    """One machine-reported result on a dispatched attempt, ready for a human decision."""
    project, requirement_id, plan, attempt, token = machine_attempt(api)
    headers = {"X-Instance-Token": token}
    base = f"/machine/attempts/{attempt.attempt_id}"
    started = checked(api.send(None, "POST", base + "/start", json=machine_claims(attempt),
                               headers=headers))
    claims = {**machine_claims(started["attempt"]), "status": "submitted", "summary": "完成",
              "usage_units": usage_units}
    reported = checked(api.send(None, "POST", base + "/result", json=claims, headers=headers))
    return SimpleNamespace(project=project, requirement_id=requirement_id, plan=plan,
                           attempt=attempt, token=token, base=base, claims=claims,
                           reported=reported["attempt"])


def delivery_path(project_id, attempt_id):
    return f"/projects/{project_id}/attempts/{attempt_id}/reviews"


def security_review_path(project_id, attempt_id):
    return f"/projects/{project_id}/attempts/{attempt_id}/security-reviews"


def security_decide_path(project_id, security_review_id):
    return f"/projects/{project_id}/security-reviews/{security_review_id}/decide"


def test_dispatch_route_needs_a_manager_and_a_verified_live_instance(api):
    project, member, binding, requirement_id = approval_setup(api)
    pid = project["project_id"]
    wire_planner(api, binding, member["user_id"])
    plan = checked(api.send("manager", "POST", plans_path(project, requirement_id)), 201)
    work_item_id = plan["work_items"][0]["work_item_id"]
    path = dispatch_path(pid, work_item_id)
    checked(api.send("member", "POST", path), 403)
    checked(api.send("outsider", "POST", path), 404)
    checked(api.send("manager", "POST", path, headers={"X-CSRF-Token": None}), 403)
    checked(api.send("manager", "POST", dispatch_path(pid, "0" * 32)), 404)
    checked(api.send("manager", "POST", path), 409)  # AdmissionDenied: no live instance

    node, node_token = api.client.app.state.instances.enroll_node("dispatch-node")
    instances = f"/projects/{pid}/agent-bindings/{binding['binding_id']}/instances"
    for url in ("ftp://127.0.0.1:9/worker", "http://admin@127.0.0.1:9/worker",
                "http://127.0.0.1:9/worker?token=x", "http://127.0.0.1:9/worker#frag",
                "http://127.0.0.1:9/worker path", "https://127.0.0.1:9\\worker"):
        checked(api.send("owner", "POST", instances,
                         json={"node_id": node.node_id, "worker_url": url}), 422)
    created = checked(api.send("owner", "POST", instances, json={
        "node_id": node.node_id, "worker_url": "http://127.0.0.1:9/worker"}), 201)
    assert created["worker_url"] == "http://127.0.0.1:9/worker"
    claimed = checked(machine_claim(api, node_token, created["claim_token"]), 201)
    checked(api.send(None, "POST", "/machine/instances/heartbeat",
                     headers={"X-Instance-Token": claimed["instance_token"]}))
    checked(api.send("manager", "POST", path), 409)  # real data needs verified isolation
    verified = checked(api.send("owner", "POST", f"{instances}/{created['instance_id']}/verify",
                                json={"expected_version": claimed["version"]}))
    assert verified["isolation_verified"] == 1
    view = checked(api.send("manager", "POST", path))
    assert (view["status"], view["number"], view["control_epoch"], view["lease_epoch"]) == (
        "assigned", 1, 1, 1)
    assert (view["work_item_id"], view["requirement_id"]) == (work_item_id, requirement_id)
    assert len(view["input_hash"]) == 64
    assert "revision_hash" not in json.dumps(view) and "package_json" not in json.dumps(view)
    assert checked(api.send("manager", "POST", path)) == view  # replay, not a second attempt
    assert work_item_statuses(api, project, requirement_id, plan) == ["running"]
    held = checked(api.send(None, "GET", "/machine/instances/me/attempts",
                            headers={"X-Instance-Token": claimed["instance_token"]}))
    assert [entry["attempt_id"] for entry in held] == [view["attempt_id"]]


def test_owner_confirmation_security_pass_and_acceptance_round_trip(api):
    seen = delivered(api)
    pid = seen.project["project_id"]
    work_item_id = seen.plan["work_items"][0]["work_item_id"]
    delivery = delivery_path(pid, seen.attempt.attempt_id)
    checked(api.send("manager", "PUT", f"/projects/{pid}/budget", json={"limit_units": 10}))
    assert checked(api.send(None, "POST", seen.base + "/result", json=seen.claims,
                            headers={"X-Instance-Token": seen.token}))["outcome"] == "duplicate"
    assert api.send(None, "GET", f"{seen.base}/security-reviews",
                    headers={"X-Instance-Token": seen.token}).json() == []
    checked(api.send("outsider", "GET", delivery), 404)
    view = checked(api.send("member", "GET", delivery))
    assert (view["attempt"]["status"], view["owner_review"], view["security_reviews"],
            view["security_problems"]) == ("submitted", None, [], [])
    assert [entry["text"] for entry in view["acceptance_items"]] == ["第一条验收"]
    assert work_item_statuses(api, seen.project, seen.requirement_id,
                              seen.plan) == ["awaiting_owner_review"]
    version = view["attempt"]["version"]
    checked(api.send("member", "POST", delivery,
                     json={"decision": "changes_requested", "expected_version": version}), 422)
    checked(api.send("outsider", "POST", delivery,
                     json={"decision": "confirmed", "expected_version": version}), 404)
    checked(api.send("member", "POST", delivery, json={"decision": "confirmed",
                                                       "expected_version": version},
                     headers={"X-CSRF-Token": None}), 403)

    rules = security_review_path(pid, seen.attempt.attempt_id)
    checked(api.send("member", "POST", rules, json={"rule_key": "dangerous_command"}), 403)
    record = checked(api.send("manager", "POST", rules, json={"rule_key": "dangerous_command"}), 201)
    assert (record["rule_key"], record["decision"], record["reviewer_user_id"],
            record["input_hash"]) == ("dangerous_command", "pending", None,
                                      seen.attempt.input_hash)
    assert checked(api.send("manager", "POST", rules,
                            json={"rule_key": "dangerous_command"}),
                   201)["security_review_id"] == record["security_review_id"]
    assert checked(api.send("member", "GET", delivery))["security_problems"] == [
        "pending:dangerous_command"]
    checked(api.send("member", "POST", delivery, json={"decision": "confirmed",
                                                        "expected_version": version}), 409)
    decide = security_decide_path(pid, record["security_review_id"])
    checked(api.send("outsider", "POST", decide, json={"decision": "approved", "reason": "可接受",
                                                       "expected_version": 1}), 404)
    checked(api.send("manager", "POST", decide, json={"decision": "approved", "reason": "可接受",
                                                      "expected_version": 1}), 403)
    listed = checked(api.send(None, "GET", f"{seen.base}/security-reviews",
                              headers={"X-Instance-Token": seen.token}))
    assert [(entry["rule_key"], entry["decision"]) for entry in listed] == [
        ("dangerous_command", "pending")]
    checked(api.send(None, "GET", f"{seen.base}/security-reviews"), 401)
    other = api.project(name="Project B")
    api.member(other)
    other_node, other_token = api.client.app.state.instances.enroll_node("foreign-node")
    other_instance = checked(api.send("owner", "POST",
        f"/projects/{other['project_id']}/agent-bindings/"
        f"{api.binding(other, api.agent())['binding_id']}/instances",
        json={"node_id": other_node.node_id}), 201)
    foreign_claim = checked(machine_claim(api, other_token, other_instance["claim_token"]), 201)
    checked(api.send(None, "GET", f"{seen.base}/security-reviews",
                     headers={"X-Instance-Token": foreign_claim["instance_token"]}), 404)

    grants = f"/projects/{pid}/security-grants"
    manager_uid = api.identities["manager"]["user"]["user_id"]
    checked(api.send("member", "GET", grants), 403)
    assert checked(api.send("manager", "GET", grants)) == []
    checked(api.send("manager", "PUT", f"{grants}/{manager_uid}", json={"active": True}), 403)
    checked(api.send("outsider", "PUT", f"{grants}/{manager_uid}", json={"active": True}), 404)
    checked(api.send("owner", "PUT", f"{grants}/{'0' * 32}", json={"active": True}), 404)
    grant = checked(api.send("owner", "PUT", f"{grants}/{manager_uid}", json={"active": True}))
    assert (grant["capability"], grant["status"], grant["granted_by"],
            grant["version"]) == ("security:approve", "active",
                                 api.identities["owner"]["user"]["user_id"], 1)
    assert checked(api.send("manager", "GET", grants)) == [grant]
    decided = checked(api.send("manager", "POST", decide, json={"decision": "approved",
                                                                "reason": "命令只在沙箱内执行",
                                                                "expected_version": 1}))
    assert (decided["decision"], decided["reviewer_user_id"],
            decided["version"]) == ("approved", manager_uid, 2)
    checked(api.send("manager", "POST", decide, json={"decision": "rejected", "reason": "改判",
                                                      "expected_version": 1}), 409)
    assert checked(api.send("member", "GET", delivery))["security_problems"] == []

    checked(api.send("member", "POST", delivery, json={"decision": "confirmed",
                                                       "expected_version": version + 1}), 409)
    confirmed = checked(api.send("member", "POST", delivery, json={"decision": "confirmed",
                                                                   "expected_version": version}))
    assert (confirmed["attempt"]["status"],
            confirmed["review"]["decision"]) == ("accepted", "confirmed")
    assert (confirmed["review"]["result_hash"], confirmed["review"]["project_id"]) == (
        confirmed["attempt"]["result_hash"], pid)
    replay = checked(api.send("member", "POST", delivery, json={"decision": "confirmed",
                                                                "expected_version": 1}))
    assert (replay["attempt"]["status"],
            replay["review"]["review_id"]) == ("accepted", confirmed["review"]["review_id"])
    checked(api.send("member", "POST", delivery, json={"decision": "changes_requested",
                                                       "reason": "另一个决定",
                                                       "expected_version": version}), 409)

    accept = f"/projects/{pid}/work-items/{work_item_id}/accept"
    checked(api.send("outsider", "POST", accept, json={"decision": "accepted",
                                                        "expected_version": 1}), 404)
    checked(api.send("member", "POST", accept, json={"decision": "accepted",
                                                     "expected_version": 1}), 403)
    item = checked(api.send("member", "GET", plans_path(seen.project, seen.requirement_id) +
                            f"/{seen.plan['plan']['plan_id']}"))["work_items"][0]
    checked(api.send("manager", "POST", accept, json={"decision": "changes_requested",
                                                      "expected_version": item["version"]}), 422)
    checked(api.send("manager", "POST", accept, json={"decision": "accepted",
                                                      "expected_version": item["version"] + 5}), 409)
    accepted = checked(api.send("manager", "POST", accept,
                                json={"decision": "accepted",
                                      "expected_version": item["version"]}))
    assert (accepted["status"], accepted["version"]) == ("accepted", item["version"] + 1)
    assert checked(api.send("manager", "POST", accept,
                            json={"decision": "accepted", "expected_version": 1}))["status"] == \
        "accepted"
    assert work_item_statuses(api, seen.project, seen.requirement_id,
                             seen.plan) == ["accepted"]
    accounts = checked(api.send("member", "GET", f"/projects/{pid}/budget"))["accounts"]
    assert [(entry["scope"], entry["limit_units"], entry["used_units"],
             entry["remaining_units"]) for entry in accounts] == [("project", 10, 3, 7)]


def test_unliftable_and_rejected_security_rules_keep_blocking_confirmation(api):
    seen = delivered(api, usage_units=2)
    pid = seen.project["project_id"]
    delivery = delivery_path(pid, seen.attempt.attempt_id)
    version = checked(api.send("member", "GET", delivery))["attempt"]["version"]
    owner_uid = api.identities["owner"]["user"]["user_id"]
    rules = security_review_path(pid, seen.attempt.attempt_id)
    checked(api.send("manager", "POST", rules, json={"rule_key": ""}), 422)
    identity = checked(api.send("manager", "POST", rules, json={"rule_key": "identity"}), 201)
    effect = checked(api.send("manager", "POST", rules,
                              json={"rule_key": "external_side_effect"}), 201)
    checked(api.send("owner", "POST", security_decide_path(pid, identity["security_review_id"]),
                     json={"decision": "approved", "reason": "看起来安全",
                           "expected_version": 1}), 403)
    checked(api.send("owner", "POST", security_decide_path(pid, identity["security_review_id"]),
                     json={"decision": "approved", "reason": "  ", "expected_version": 1}), 422)
    rejected = checked(api.send("owner", "POST",
                                security_decide_path(pid, effect["security_review_id"]),
                                json={"decision": "rejected", "reason": "会改写宿主文件",
                                      "expected_version": 1}))
    assert (rejected["decision"], rejected["reviewer_user_id"],
            rejected["version"]) == ("rejected", owner_uid, 2)
    assert sorted(checked(api.send("member", "GET", delivery))["security_problems"]) == [
        "pending:identity", "rejected:external_side_effect"]
    checked(api.send("member", "POST", delivery, json={"decision": "confirmed",
                                                        "expected_version": version}), 409)
    assert sorted((entry["rule_key"], entry["decision"]) for entry in
                  checked(api.send(None, "GET", f"{seen.base}/security-reviews",
                                   headers={"X-Instance-Token": seen.token}))) == [
        ("external_side_effect", "rejected"), ("identity", "pending")]
    api.client.app.state.reviews.clock = lambda: time.time() + 90000
    assert sorted(checked(api.send("member", "GET", delivery))["security_problems"]) == [
        "expired:identity", "rejected:external_side_effect"]
    checked(api.send("member", "POST", delivery, json={"decision": "confirmed",
                                                       "expected_version": version}), 409)


def test_human_withdrawal_over_http_revokes_execution_rights_without_faking_a_stop(api):
    project, requirement_id, plan, attempt, token = machine_attempt(api)
    pid = project["project_id"]
    headers = {"X-Instance-Token": token}
    base = f"/machine/attempts/{attempt.attempt_id}"
    started = checked(api.send(None, "POST", base + "/start", json=machine_claims(attempt),
                               headers=headers))
    work_item_id = plan["work_items"][0]["work_item_id"]
    cancel = f"/projects/{pid}/work-items/{work_item_id}/cancel"
    item = checked(api.send("member", "GET", plans_path(project, requirement_id) +
                            f"/{plan['plan']['plan_id']}"))["work_items"][0]
    checked(api.send("outsider", "POST", cancel,
                     json={"expected_version": item["version"], "reason": "撤回"}), 404)
    checked(api.send("member", "POST", cancel, json={"expected_version": 1}),
            409)  # a stale version cannot withdraw anyone's work
    cancelled = checked(api.send("member", "POST", cancel,
                                 json={"expected_version": item["version"], "reason": "人工撤回"}))
    assert (cancelled["status"], cancelled["version"]) == ("cancelled", item["version"] + 1)
    assert work_item_statuses(api, project, requirement_id, plan) == ["cancelled"]
    held = checked(api.send(None, "GET", "/machine/instances/me/attempts", headers=headers))
    assert [(entry["status"], entry["control_epoch"]) for entry in held] == [
        ("reconciliation_required", 2)]
    late = machine_claims(started["attempt"])
    checked(api.send(None, "POST", base + "/result", json={**late, "status": "submitted",
                                                            "summary": "迟到的结果"},
                     headers=headers), 409)
    checked(api.send(None, "POST", base + "/lease", json=late, headers=headers), 409)
    assert api.send(None, "GET", base + "/package", headers=headers).status_code == 200
    events = {event["topic"]: event for event in
              checked(api.send("manager", "GET", f"/projects/{pid}/outbox"))}
    revoked = events["attempt.revoked"]
    assert revoked["delivered_at"] is None
    assert revoked["payload"] == {"attempt_id": attempt.attempt_id, "work_item_id": work_item_id,
                                  "instance_id": attempt.instance_id,
                                  "input_hash": attempt.input_hash, "control_epoch": 2}
    assert checked(api.send("member", "POST", cancel,
                            json={"expected_version": 1, "reason": "人工撤回"}))["status"] == \
        "cancelled"


def test_wake_helper_posts_advisory_payload_and_flags_failures():
    seen = []

    def handler(request):
        seen.append((str(request.url), request.headers["x-collab-wake"],
                     json.loads(request.content)))
        return httpx.Response(204 if request.url.path == "/awake" else 500)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        _wake(client, "https://worker.invalid/awake", {"attempt_id": "a"})
        with pytest.raises(OSError):
            _wake(client, "https://worker.invalid/asleep", {"attempt_id": "b"})
    assert seen == [("https://worker.invalid/awake", "1", {"attempt_id": "a"}),
                    ("https://worker.invalid/asleep", "1", {"attempt_id": "b"})]


def test_the_rework_route_returns_a_returned_item_and_informs_the_next_attempt(api):
    seen = delivered(api)
    pid = seen.project["project_id"]
    wid = seen.plan["work_items"][0]["work_item_id"]
    rework = f"/projects/{pid}/work-items/{wid}/rework"
    returned = checked(api.send("member", "POST", delivery_path(pid, seen.attempt.attempt_id),
                                json={"decision": "changes_requested", "reason": "第二条验收未满足",
                                      "expected_version": seen.reported["version"]}))
    assert returned["attempt"]["status"] == "changes_requested"
    version = checked(api.send("manager", "GET", plans_path(seen.project, seen.requirement_id)
                               + f"/{seen.plan['plan']['plan_id']}"))["work_items"][0]["version"]
    assert work_item_statuses(api, seen.project, seen.requirement_id,
                              seen.plan) == ["changes_requested"]

    checked(api.send("outsider", "POST", rework, json={"expected_version": version}), 404)
    checked(api.send("member", "POST", rework, json={"expected_version": version}), 403)
    checked(api.send("manager", "POST", f"/projects/{pid}/work-items/{'0' * 32}/rework",
                     json={"expected_version": version}), 404)
    checked(api.send("manager", "POST", rework, json={"expected_version": version + 1}), 409)
    queued = checked(api.send("manager", "POST", rework, json={"expected_version": version}))
    assert (queued["status"], queued["version"]) == ("ready", version + 1)    # 已经回到队列的工作项不再被返工，重复请求不会把版本推第二格。
    checked(api.send("manager", "POST", rework, json={"expected_version": version + 1}), 409)

    second = checked(api.send("manager", "POST", dispatch_path(pid, wid)))
    assert (second["number"], second["attempt_id"] != seen.attempt.attempt_id) == (2, True)
    payload = checked(api.send(None, "GET", f"/machine/attempts/{second['attempt_id']}/package",
                               headers={"X-Instance-Token": seen.token}))
    assert [(entry["reason"], entry["attempt_id"], entry["reviewed_by"])
            for entry in payload["rework"]] == [
        ("第二条验收未满足", seen.attempt.attempt_id,
         api.identities["member"]["user"]["user_id"])]
    assert payload["subtask"]["acceptance"] == json.loads(
        seen.attempt.package_json)["subtask"]["acceptance"]
    assert seen.attempt.input_hash != second["input_hash"]
    old = checked(api.send("manager", "GET", delivery_path(pid, seen.attempt.attempt_id)))
    assert (old["attempt"]["status"], old["owner_review"]["reason"]) == (
        "changes_requested", "第二条验收未满足")


def test_a_business_return_is_kept_and_reaches_the_next_attempt(api):
    seen = delivered(api)
    pid = seen.project["project_id"]
    wid = seen.plan["work_items"][0]["work_item_id"]
    delivery = delivery_path(pid, seen.attempt.attempt_id)
    accept = f"/projects/{pid}/work-items/{wid}/accept"
    checked(api.send("member", "POST", delivery,
                     json={"decision": "confirmed",
                           "expected_version": seen.reported["version"]}))
    assert checked(api.send("member", "GET", delivery))["acceptance_review"] is None
    version = checked(api.send("manager", "GET", plans_path(seen.project, seen.requirement_id)
                               + f"/{seen.plan['plan']['plan_id']}"))["work_items"][0]["version"]
    returned = checked(api.send("manager", "POST", accept,
                                json={"decision": "changes_requested", "reason": "验收项二缺测试",
                                      "expected_version": version}))
    assert (returned["status"], returned["version"]) == ("changes_requested", version + 1)

    verdict = checked(api.send("member", "GET", delivery))["acceptance_review"]
    assert (verdict["decision"], verdict["attempt_id"], verdict["result_hash"]) == (
        "changes_requested", seen.attempt.attempt_id, seen.reported["result_hash"])
    assert (verdict["project_id"], verdict["work_item_id"], verdict["reason"]) == (
        pid, wid, "验收项二缺测试")
    assert verdict["reviewer_user_id"] == api.identities["manager"]["user"]["user_id"]

    queued = checked(api.send("manager", "POST", f"/projects/{pid}/work-items/{wid}/rework",
                              json={"expected_version": returned["version"]}))
    assert queued["status"] == "ready"
    second = checked(api.send("manager", "POST", dispatch_path(pid, wid)))
    payload = checked(api.send(None, "GET", f"/machine/attempts/{second['attempt_id']}/package",
                               headers={"X-Instance-Token": seen.token}))
    assert [(entry["kind"], entry["attempt_number"], entry["reason"])
            for entry in payload["rework"]] == [("acceptance", 1, "验收项二缺测试")]


def test_cancelling_a_requirement_and_reconciling_its_attempt_over_http(api):
    project, requirement_id, plan, attempt, token = machine_attempt(api)
    pid = project["project_id"]
    base = f"/machine/attempts/{attempt.attempt_id}"
    headers = {"X-Instance-Token": token}
    checked(api.send("manager", "PUT", f"/projects/{pid}/requirements/{requirement_id}/budget",
                     json={"limit_units": 10}))
    started = checked(api.send(None, "POST", base + "/start", json=machine_claims(attempt),
                               headers=headers))
    actions = f"/projects/{pid}/requirements/{requirement_id}/actions"
    version = checked(api.send("member", "GET",
                               f"/projects/{pid}/requirements/{requirement_id}"))["version"]

    checked(api.send("outsider", "POST", actions,
                     json={"action": "cancel", "expected_version": version}), 404)
    checked(api.send("owner", "POST", actions,
                     json={"action": "pause", "expected_version": version}), 422)
    checked(api.send("manager", "POST", actions,
                     json={"action": "cancel", "expected_version": version + 3}), 409)
    checked(api.send("member", "POST", actions, json={"action": "cancel",
                                                      "expected_version": version},
                     headers={"X-CSRF-Token": None}), 403)
    stopping = checked(api.send("member", "POST", actions,
                                json={"action": "cancel", "expected_version": version,
                                      "reason": "需求方不再要了"}))
    assert stopping["status"] == "cancelling"
    assert work_item_statuses(api, project, requirement_id, plan) == ["cancelled"]
    live = checked(api.send("manager", "GET", delivery_path(pid, attempt.attempt_id)))["attempt"]
    assert (live["status"], live["control_epoch"]) == ("reconciliation_required",
                                                      attempt.control_epoch + 1)

    # 撤销后 Worker 的停止确认只留证据：需求停在 cancelling，直到有人核对。
    checked(api.send(None, "POST", base + "/cancel",
                     json={**machine_claims(live), "reason": "已停止"}, headers=headers), 409)
    reconcile = f"/projects/{pid}/attempts/{attempt.attempt_id}/reconcile"
    checked(api.send("outsider", "POST", reconcile,
                     json={"outcome": "cancelled", "reason": "局外人核对",
                           "expected_version": live["version"]}), 404)
    checked(api.send("member", "POST", reconcile,
                     json={"outcome": "cancelled", "reason": " ",
                           "expected_version": live["version"]}), 422)
    checked(api.send("member", "POST", f"/projects/{pid}/attempts/{'0' * 32}/reconcile",
                     json={"outcome": "cancelled", "reason": "查无此尝试",
                           "expected_version": 1}), 404)
    checked(api.send("manager", "POST", reconcile,
                     json={"outcome": "cancelled", "reason": "看日志",
                           "expected_version": live["version"] + 7}), 409)
    settled = checked(api.send("manager", "POST", reconcile,
                               json={"outcome": "cancelled",
                                     "reason": "核对 Worker 日志：进程已终止且未产生副作用",
                                     "expected_version": live["version"]}))
    assert settled["status"] == "cancelled"
    closed = checked(api.send("member", "POST", actions,
                              json={"action": "cancel", "expected_version": stopping["version"],
                                    "reason": "需求方不再要了"}))
    assert (closed["status"], closed["version"]) == ("cancelled", stopping["version"] + 1)
    accounts = checked(api.send("member", "GET", f"/projects/{pid}/budget"))["accounts"]
    assert [(entry["scope"], entry["used_units"], entry["remaining_units"])
            for entry in accounts] == [("requirement", 0, 10)]


def test_a_failed_reconciliation_puts_the_work_item_back_in_the_queue(api):
    project, requirement_id, plan, attempt, _token = machine_attempt(api)
    pid, wid = project["project_id"], plan["work_items"][0]["work_item_id"]
    path = plans_path(project, requirement_id) + f"/{plan['plan']['plan_id']}"

    def current_item():
        detail = checked(api.send("manager", "GET", path))
        return next(entry for entry in detail["work_items"] if entry["work_item_id"] == wid)

    cancelled = checked(api.send("manager", "POST", f"/projects/{pid}/work-items/{wid}/cancel",
                                 json={"expected_version": current_item()["version"],
                                       "reason": "先停下来"}))
    assert cancelled["status"] == "cancelled"
    live = checked(api.send("manager", "GET", delivery_path(pid, attempt.attempt_id)))["attempt"]
    settled = checked(api.send("manager", "POST",
                               f"/projects/{pid}/attempts/{attempt.attempt_id}/reconcile",
                               json={"outcome": "failed", "expected_version": live["version"],
                                     "reason": "执行机改过两个文件，已经落盘"}))
    assert settled["status"] == "failed"

    # 对账说「动过外面世界」，这项就不是被放弃而是失败，于是它重新出现在可重排的那一侧。
    assert current_item()["status"] == "failed"
    requeued = checked(api.send("manager", "POST", f"/projects/{pid}/work-items/{wid}/rework",
                                json={"expected_version": current_item()["version"]}))
    assert requeued["status"] == "ready"
    second = checked(api.send("manager", "POST", dispatch_path(pid, wid)))
    assert (second["number"], second["status"]) == (2, "assigned")
    # 重做已经排上，就不该再欠谁一次裁定。
    assert [entry["item_type"] for entry in checked(api.send("manager", "GET", "/inbox"))
            if not entry["item_type"].startswith("requirement_")] == []


def test_a_tick_over_http_reclaims_an_expired_lease_and_reports_it_as_news(api):
    project, requirement_id, plan, attempt, token = machine_attempt(api)
    pid, wid = project["project_id"], plan["work_items"][0]["work_item_id"]
    base = f"/machine/attempts/{attempt.attempt_id}"
    headers = {"X-Instance-Token": token}
    started = checked(api.send(None, "POST", base + "/start", json=machine_claims(attempt),
                               headers=headers))
    # 租约按服务端的真实时钟到期：这里不改任何状态，只是把那一格推到过去。
    with api.database.transaction() as conn:
        conn.execute("UPDATE attempts SET lease_expires_at=? WHERE attempt_id=?",
                     (time.time() - 1, attempt.attempt_id))

    tick_path = f"/projects/{pid}/scheduler/tick"
    checked(api.send("member", "POST", tick_path), 403)
    checked(api.send("outsider", "POST", tick_path), 404)
    assert checked(api.send("manager", "POST", tick_path))["attempts_reclaimed"] == 1

    live = checked(api.send("member", "GET", delivery_path(pid, attempt.attempt_id)))["attempt"]
    assert (live["status"], live["control_epoch"], live["version"]) == (
        "reconciliation_required", started["attempt"]["control_epoch"] + 1,
        started["attempt"]["version"] + 2)
    # 接管只收回写权：拉输入与下载证据仍然认这个实例。
    assert checked(api.send(None, "GET", base + "/package",
                            headers=headers))["work_item_id"] == wid
    for stage, body in (("ack", {}),
                        ("result", {"status": "submitted", "summary": "迟到的结果",
                                    "artifacts": [], "usage_units": 1}),
                        ("cancel", {"reason": "迟到的停止确认"})):
        checked(api.send(None, "POST", f"{base}/{stage}",
                         json={**machine_claims(live), **body}, headers=headers), 409)
    # 待办不再要求人去点撤销：它直接指向对账，版本就是尝试当前的版本。
    owed = [entry for entry in checked(api.send("member", "GET", "/inbox"))
            if entry["item_type"] in ("worker_attention", "attempt_reconciliation")]
    assert [(entry["item_type"], entry["action"], entry["expected_version"])
            for entry in owed] == [
        ("attempt_reconciliation", "reconcile_attempt", live["version"])]
    # 这一步是新闻：机器静下来这件事要出现在需求时间线与「我的动态」里，谓语读得出中文。
    activity = checked(api.send("member", "GET",
                                f"/projects/{pid}/requirements/{requirement_id}/activity?limit=200"))
    assert "attempt.lease_reclaimed" in {entry["action"] for entry in activity}
    assert [entry["label"] for entry in activity if entry["action"] == "attempt.lease_reclaimed"] \
        == ["第 1 次尝试 · A"]
    assert activity[[entry["action"] for entry in activity].index("attempt.lease_reclaimed")][
        "actor_id"] is None
    feed = checked(api.send("member", "GET", "/feed?limit=200"))
    assert "attempt.lease_reclaimed" in {entry["action"] for entry in feed}

    # 裁定「没动过外面世界」之后，这项回到队列那一侧，不是停在 running 等谁去点撤销。
    settled = checked(api.send("member", "POST",
                               f"/projects/{pid}/attempts/{attempt.attempt_id}/reconcile",
                               json={"outcome": "cancelled", "expected_version": live["version"],
                                     "reason": "看过那台机器，只读到输入没写过东西"}))
    assert settled["status"] == "cancelled"
    detail = checked(api.send("member", "GET",
                              plans_path(project, requirement_id) + f"/{plan['plan']['plan_id']}"))
    assert next(entry for entry in detail["work_items"]
                if entry["work_item_id"] == wid)["status"] == "waiting_agent"


def test_a_silent_machine_becomes_a_to_do_a_manager_can_close(api):
    project, _requirement_id, _plan, attempt, _token = machine_attempt(api)
    pid = project["project_id"]
    instances = f"/projects/{pid}/agent-bindings/{attempt.binding_id}/instances"
    disable = f"{instances}/{attempt.instance_id}/disable"
    # 只把最后一次心跳推到窗口之外：租约还是新的，所以这一格说的确实是整台机器没了消息。
    with api.database.transaction() as conn:
        conn.execute("UPDATE execution_instances SET heartbeat_at=? WHERE instance_id=?",
                     (time.time() - 121, attempt.instance_id))

    def owed(actor):
        return [entry for entry in checked(api.send(actor, "GET", "/inbox"))
                if entry["item_type"] == "machine_absent"]

    assert owed("outsider") == []
    # 负责这个工作项的成员不欠这一格：禁用执行机在路由里是 roles=MANAGERS。
    assert owed("member") == []
    manager = owed("manager")
    assert len(manager) == 1 and owed("owner") == manager
    entry = manager[0]
    live = checked(api.send("manager", "GET", f"{instances}"))
    assert [(item["instance_id"], item["version"]) for item in live] == [
        (attempt.instance_id, entry["expected_version"])]
    assert (entry["action"], entry["href"], entry["links"]) == (
        "disable_instance", disable, {"instances": instances})
    assert entry["title"] == "My Agent · Project-specific duty"

    checked(api.send("member", "POST", disable, json={"expected_version": entry["expected_version"]}),
           403)
    checked(api.send("outsider", "POST", disable, json={"expected_version": entry["expected_version"]}),
            404)
    checked(api.send("manager", "POST", disable,
                     json={"expected_version": entry["expected_version"] + 3}), 409)
    checked(api.send("manager", "POST", disable,
                     json={"expected_version": entry["expected_version"]},
                     headers={"X-CSRF-Token": None}), 403)
    disabled = checked(api.send("manager", "POST", disable,
                                json={"expected_version": entry["expected_version"]}))
    assert disabled["status"] == "disabled"
    assert owed("manager") == []
    # 照着待办给的那一步写，就写进了同一份项目审计：这条待办指的不是界面自己造的记号。
    audit = [row for row in checked(api.send("manager", "GET", f"/projects/{pid}/audit?limit=200"))
             if row["action"] == "instance.disabled"]
    assert [(row["target_id"], row["target_version"], row["actor_id"]) for row in audit] == [
        (attempt.instance_id, disabled["version"],
         api.identities["manager"]["user"]["user_id"])]


def test_the_people_judging_a_delivery_can_read_the_files_behind_it(api, monkeypatch):
    project, _requirement_id, _plan, attempt, token = machine_attempt(api)
    pid = project["project_id"]
    headers = {"X-Instance-Token": token}
    base = f"/machine/attempts/{attempt.attempt_id}"
    started = checked(api.send(None, "POST", base + "/start", json=machine_claims(attempt),
                               headers=headers))
    name = "结果 说明.md"
    content = "# 交付说明\n第二行".encode("utf-8")
    digest = hashlib.sha256(content).hexdigest()
    uploaded = checked(api.send(None, "POST", base + "/artifacts", headers=headers, json={
        **machine_claims(started["attempt"]), "name": name, "media_type": "text/markdown",
        "sha256": digest, "content": base64.b64encode(content).decode("ascii")}), 201)
    artifact_id = uploaded["artifact"]["artifact_id"]
    human = f"/projects/{pid}/attempts/{attempt.attempt_id}/artifacts"

    listed = checked(api.send("member", "GET", human))
    assert [(entry["name"], entry["media_type"], entry["sha256"], entry["size_bytes"],
             entry["href"]) for entry in listed] == [
        (name, "text/markdown", digest, len(content), f"{human}/{artifact_id}")]
    # 局外人连清单都取不到，回答与「这条尝试不在这个项目里」同一种；产物不跟着 id 跨项目跑。
    checked(api.send("outsider", "GET", human), 404)
    checked(api.send("outsider", "GET", f"{human}/{artifact_id}"), 404)
    checked(api.send("member", "GET", f"{human}/{'f' * 32}"), 404)
    checked(api.send("member", "GET",
                     f"/projects/{pid}/attempts/{'f' * 32}/artifacts/{artifact_id}"), 404)

    response = api.send("member", "GET", f"{human}/{artifact_id}")
    assert (response.status_code, response.content) == (200, content)
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-content-hash"] == digest
    assert response.headers["content-type"].startswith("text/markdown")
    # 类型是机器自报的，所以它只是关于文件的资料，不是在本源里渲染它的许可：只给下载。
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["content-security-policy"] == "default-src 'none'; sandbox"
    assert response.headers["referrer-policy"] == "no-referrer"
    disposition = response.headers["content-disposition"]
    # 文件名是自由文本：原始字符不进头部，ASCII 兜底用记录自己的 id。
    assert disposition.startswith(f'attachment; filename="{artifact_id}"; ')
    assert name not in disposition
    assert unquote(disposition.split("filename*=UTF-8''")[1]) == name

    # 证据改不动：append-only 触发器先拦下任何 SQL 改写，所以「字节与摘要不符」只能来自文件损坏。
    with api.database.transaction() as conn, pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE attempt_artifacts SET content=? WHERE artifact_id=?",
                     (b"tampered bytes", artifact_id))
    # 那一格仍然要拒：这里替换的是读出结果，模拟的是坏掉的文件，不是模拟判定逻辑本身。
    def rotted(_self, project_id, artifact):
        return b"tampered bytes"

    monkeypatch.setattr(CollaborationStore, "get_artifact_content", rotted)
    checked(api.send("member", "GET", f"{human}/{artifact_id}"), 409)
    # 清单照旧可读：它不碰字节，报的是「机器交回了什么」。
    assert [entry["sha256"] for entry in checked(api.send("member", "GET", human))] == [digest]


def test_the_requester_signs_for_the_requirement_delivery_over_http(api):
    project, requirement_id, plan, attempt, token = machine_attempt(api)
    pid = project["project_id"]
    base = f"/machine/attempts/{attempt.attempt_id}"
    headers = {"X-Instance-Token": token}
    wid = plan["work_items"][0]["work_item_id"]
    actions = f"/projects/{pid}/requirements/{requirement_id}/actions"
    delivery = f"/projects/{pid}/requirements/{requirement_id}/delivery"
    integration = f"/projects/{pid}/requirements/{requirement_id}/integration"
    acceptance = f"/projects/{pid}/requirements/{requirement_id}/acceptance"

    reported = checked(api.send(None, "POST", base + "/result",
                                json={**machine_claims(attempt), "status": "submitted",
                                      "summary": "完成", "usage_units": 2}, headers=headers))
    version = reported["attempt"]["version"]
    checked(api.send("member", "POST", delivery_path(pid, attempt.attempt_id),
                     json={"decision": "confirmed", "expected_version": version}))
    item = checked(api.send("member", "GET", plans_path(project, requirement_id)
                            + f"/{plan['plan']['plan_id']}"))["work_items"][0]
    checked(api.send("manager", "POST", f"/projects/{pid}/work-items/{wid}/accept",
                     json={"decision": "accepted", "expected_version": item["version"]}))

    requirement = checked(api.send("member", "GET",
                                   f"/projects/{pid}/requirements/{requirement_id}"))
    checked(api.send("member", "POST", actions,
                     json={"action": "restart", "expected_version": requirement["version"]}), 422)
    checked(api.send("member", "POST", actions,
                     json={"action": "advance", "expected_version": requirement["version"] + 5}),
              409)
    checked(api.send("member", "POST", actions,
                     json={"action": "advance", "expected_version": requirement["version"] + 5}),
              409)
    checked(api.send("outsider", "POST", actions,
                     json={"action": "advance", "expected_version": requirement["version"]}), 404)
    stopping = checked(api.send("member", "POST", actions,
                                json={"action": "advance",
                                      "expected_version": requirement["version"]}))
    # 交付齐了先停在 integrating：提出人签字之前，得有人对着原文核过一次覆盖。
    assert stopping["status"] == "integrating"
    checked(api.send("member", "POST", actions,
                     json={"action": "advance", "expected_version": stopping["version"]}))
    checked(api.send("outsider", "GET", delivery), 404)
    view = checked(api.send("member", "GET", delivery))
    assert (view["requirement"]["status"], view["acceptances"]) == ("integrating", [])
    assert [(entry["key"], entry["status"], entry["attempt_number"], entry["artifacts"])
            for entry in view["delivery"]["items"]] == [("A", "accepted", 1, [])]
    assert view["delivery"]["items"][0]["acceptance"] == [
        {"item_id": "AC-001", "text": "第一条验收"}]
    assert view["delivery"]["items"][0]["result_hash"] == reported["attempt"]["result_hash"]

    # 核对报告把「哪条验收由哪个工作项交付」摆出来，签的人不用自己对着原文数。
    report = checked(api.send("member", "GET", integration))
    assert report["report"] == {"evidence_hash": view["delivery"]["evidence_hash"],
                                "revision_id": plan["plan"]["revision_id"],
                                "acceptance": [{"item_id": "AC-001", "text": "第一条验收",
                                                "covered_by": ["A"]}], "uncovered": []}
    assert report["verdicts"] == []

    checked(api.send("outsider", "POST", integration,
                     json={"decision": "passed", "expected_version": stopping["version"]}), 404)
    checked(api.send("member", "POST", integration,
                     json={"decision": "passed", "expected_version": stopping["version"]}), 403)
    checked(api.send("manager", "POST", integration,
                     json={"decision": "approved", "expected_version": stopping["version"]}), 422)
    # 退回要写清缺了哪一条：下一轮执行的人只能照着这句话改。
    checked(api.send("manager", "POST", integration,
                     json={"decision": "returned", "expected_version": stopping["version"]}), 422)
    checked(api.send("manager", "POST", integration,
                     json={"decision": "passed", "expected_version": stopping["version"] + 3}), 409)
    waiting = checked(api.send("manager", "POST", integration,
                               json={"decision": "passed",
                                     "expected_version": stopping["version"]}))
    assert waiting["status"] == "awaiting_acceptance"
    assert [(entry["decision"], entry["actor_user_id"], entry["reason"]) for entry in
            checked(api.send("member", "GET", integration))["verdicts"]] == [
        ("passed", api.identities["manager"]["user"]["user_id"], "")]

    checked(api.send("manager", "POST", acceptance,
                     json={"decision": "accepted", "expected_version": waiting["version"]}), 403)
    checked(api.send("outsider", "POST", acceptance,
                     json={"decision": "accepted", "expected_version": waiting["version"]}), 404)
    checked(api.send("member", "POST", acceptance,
                     json={"decision": "signed_off", "expected_version": waiting["version"]}), 422)
    checked(api.send("member", "POST", acceptance,
                     json={"decision": "changes_requested", "expected_version": waiting["version"]}),
              422)
    done = checked(api.send("member", "POST", acceptance,
                            json={"decision": "accepted", "expected_version": waiting["version"],
                                  "reason": "对照原文逐条可用"}))
    assert done["status"] == "completed"
    signed = checked(api.send("member", "GET", delivery))["acceptances"]
    assert [(entry["decision"], entry["reason"], entry["reviewer_user_id"])
            for entry in signed] == [("accepted", "对照原文逐条可用",
                                      api.identities["member"]["user"]["user_id"])]
    assert signed[0]["evidence_hash"] == view["delivery"]["evidence_hash"]
    replay = checked(api.send("member", "POST", acceptance,
                              json={"decision": "accepted", "expected_version": 1}))
    assert (replay["status"], replay["version"]) == ("completed", done["version"])
    assert len(checked(api.send("member", "GET", delivery))["acceptances"]) == 1
    checked(api.send("member", "POST", actions,
                     json={"action": "advance", "expected_version": done["version"]}), 409)
    checked(api.send("member", "POST", actions,
                     json={"action": "cancel", "expected_version": done["version"]}), 409)


def test_a_return_over_http_sends_back_only_the_items_it_names(api):
    project, requirement_id, plan, attempt, token = machine_attempt(
        api, acceptance=("第一条验收", "第二条验收"),
        items=lambda member, binding: [
            plan_candidate(binding, member["user_id"], "A", ["AC-001"]),
            plan_candidate(binding, member["user_id"], "B", ["AC-002"])])
    pid = project["project_id"]
    headers = {"X-Instance-Token": token}
    base = f"/projects/{pid}/requirements/{requirement_id}"
    keys = [entry["key"] for entry in plan["work_items"]]
    assert keys == ["A", "B"]

    def finish(attempt_id, claims, work_item_id):
        """One item, driven from the machine's first call to a business acceptance."""
        checked(api.send(None, "POST", f"/machine/attempts/{attempt_id}/start", json=claims,
                         headers=headers))
        reported = checked(api.send(None, "POST", f"/machine/attempts/{attempt_id}/result",
                                    json={**claims, "status": "submitted", "summary": "按原文做完",
                                          "usage_units": 2}, headers=headers))
        checked(api.send("member", "POST", delivery_path(pid, attempt_id),
                         json={"decision": "confirmed",
                               "expected_version": reported["attempt"]["version"]}))
        stored = next(entry for entry in checked(
            api.send("member", "GET", plans_path(project, requirement_id)
                     + f"/{plan['plan']['plan_id']}"))["work_items"]
            if entry["work_item_id"] == work_item_id)
        return checked(api.send("manager", "POST",
                                f"/projects/{pid}/work-items/{work_item_id}/accept",
                                json={"decision": "accepted",
                                      "expected_version": stored["version"]}))

    finish(attempt.attempt_id, machine_claims(attempt), plan["work_items"][0]["work_item_id"])
    dispatched = checked(api.send("manager", "POST",
                                  dispatch_path(pid, plan["work_items"][1]["work_item_id"])))
    item_b = plan["work_items"][1]["work_item_id"]
    finish(dispatched["attempt_id"], machine_claims(dispatched), item_b)
    assert work_item_statuses(api, project, requirement_id, plan) == ["accepted", "accepted"]

    requirement = checked(api.send("member", "GET", base))
    stopping = checked(api.send("member", "POST", f"{base}/actions",
                                json={"action": "advance",
                                      "expected_version": requirement["version"]}))
    assert stopping["status"] == "integrating"
    waiting = checked(api.send("manager", "POST", f"{base}/integration",
                               json={"decision": "passed",
                                     "expected_version": stopping["version"]}))
    assert waiting["status"] == "awaiting_acceptance"

    checked(api.send("member", "POST", f"{base}/acceptance",
                     json={"decision": "accepted", "expected_version": waiting["version"],
                           "returned_items": ["B"]}), 422)
    checked(api.send("member", "POST", f"{base}/acceptance",
                     json={"decision": "changes_requested", "reason": "第二条没做",
                           "expected_version": waiting["version"], "returned_items": ["Z"]}), 422)
    refused = checked(api.send("member", "POST", f"{base}/acceptance",
                               json={"decision": "changes_requested",
                                     "reason": "对照原文，第二条验收没做",
                                     "expected_version": waiting["version"],
                                     "returned_items": ["B"]}))
    assert refused["status"] == "rework"
    # A 没有被点名，它那一版已验收的交付就还站在原地。
    assert work_item_statuses(api, project, requirement_id, plan) == ["accepted",
                                                                      "changes_requested"]
    rows = checked(api.send("member", "GET", f"{base}/delivery"))["acceptances"]
    assert [(entry["decision"], entry["returned_items"]) for entry in rows] == [
        ("changes_requested", ["B"])]
    assert [(entry["decision"], entry["returned_items"]) for entry in
            checked(api.send("member", "GET", f"{base}/integration"))["verdicts"]] == [
        ("passed", [])]
    # 提出人手里那条待办已经清了，欠的下一步是管理者把 B 重新排队。
    assert [(entry["item_type"], entry["work_item_id"]) for entry in
            checked(api.send("member", "GET", "/inbox"))] == []
    reopened = checked(api.send("manager", "GET", plans_path(project, requirement_id)
                                + f"/{plan['plan']['plan_id']}"))["work_items"][1]
    queued = checked(api.send("manager", "POST", f"/projects/{pid}/work-items/{item_b}/rework",
                              json={"expected_version": reopened["version"]}))
    assert queued["status"] == "ready"
    second = checked(api.send("manager", "POST", dispatch_path(pid, item_b)))
    payload = checked(api.send(None, "GET",
                               f"/machine/attempts/{second['attempt_id']}/package",
                               headers=headers))
    assert [(entry["kind"], entry["reason"]) for entry in payload["rework"]] == [
        ("requirement_acceptance", "对照原文，第二条验收没做")]


def test_a_returned_requirement_reopens_its_items_and_informs_the_next_attempt(api):
    seen = delivered(api)
    pid = seen.project["project_id"]
    wid = seen.plan["work_items"][0]["work_item_id"]
    base = f"/projects/{pid}/requirements/{seen.requirement_id}"
    sign, actions, delivery = f"{base}/acceptance", f"{base}/actions", f"{base}/delivery"
    rework = f"/projects/{pid}/work-items/{wid}/rework"

    def owed(actor):
        return [(entry["item_type"], entry["action"], entry["href"]) for entry in
                checked(api.send(actor, "GET", "/inbox"))
                if not entry["item_type"].startswith("requirement_")]

    checked(api.send("member", "POST", delivery_path(pid, seen.attempt.attempt_id),
                     json={"decision": "confirmed", "expected_version": seen.reported["version"]}))
    item = checked(api.send("manager", "GET", plans_path(seen.project, seen.requirement_id)
                            + f"/{seen.plan['plan']['plan_id']}"))["work_items"][0]
    checked(api.send("manager", "POST", f"/projects/{pid}/work-items/{wid}/accept",
                     json={"decision": "accepted", "expected_version": item["version"]}))
    requirement = checked(api.send("member", "GET", base))
    stopping = checked(api.send("member", "POST", actions,
                                json={"action": "advance",
                                      "expected_version": requirement["version"]}))
    waiting = checked(api.send("manager", "POST", f"{base}/integration",
                               json={"decision": "passed",
                                     "expected_version": stopping["version"]}))
    assert (stopping["status"], waiting["status"]) == ("integrating", "awaiting_acceptance")
    refused = checked(api.send("member", "GET", delivery))["delivery"]["evidence_hash"]

    checked(api.send("outsider", "POST", sign,
                     json={"decision": "changes_requested", "reason": "缺灰度开关",
                           "expected_version": waiting["version"]}), 404)
    checked(api.send("manager", "POST", sign,
                     json={"decision": "changes_requested", "reason": "缺灰度开关",
                           "expected_version": waiting["version"]}), 403)
    checked(api.send("member", "POST", sign,
                     json={"decision": "changes_requested", "expected_version": waiting["version"]}),
              422)
    checked(api.send("member", "POST", sign,
                     json={"decision": "changes_requested", "reason": "缺灰度开关",
                           "expected_version": waiting["version"] + 4}), 409)
    returned = checked(api.send("member", "POST", sign,
                                json={"decision": "changes_requested",
                                      "reason": "对照原文，灰度开关整条没做",
                                      "expected_version": waiting["version"]}))
    assert (returned["status"], returned["version"]) == ("rework", waiting["version"] + 1)
    assert work_item_statuses(api, seen.project, seen.requirement_id, seen.plan) == [
        "changes_requested"]
    rows = checked(api.send("member", "GET", delivery))["acceptances"]
    assert [(entry["decision"], entry["reason"], entry["reviewer_user_id"],
             entry["evidence_hash"]) for entry in rows] == [
        ("changes_requested", "对照原文，灰度开关整条没做",
         api.identities["member"]["user"]["user_id"], refused)]
    # 需求级裁定不冒充工作项裁定：那一次尝试仍然只有一条 accepted 的业务验收记录。
    assert checked(api.send("member", "GET",
                            delivery_path(pid, seen.attempt.attempt_id)))["acceptance_review"][
        "decision"] == "accepted"
    assert owed("manager") == [("rework_queue", "rework_work_item", rework)]
    assert owed("member") == []
    replay = checked(api.send("member", "POST", sign,
                              json={"decision": "changes_requested",
                                    "reason": "对照原文，灰度开关整条没做",
                                    "expected_version": 1}))
    assert (replay["status"], replay["version"]) == ("rework", returned["version"])
    assert len(checked(api.send("member", "GET", delivery))["acceptances"]) == 1

    reopened = checked(api.send("manager", "GET", plans_path(seen.project, seen.requirement_id)
                                + f"/{seen.plan['plan']['plan_id']}"))["work_items"][0]
    assert (reopened["status"], reopened["version"]) == ("changes_requested",
                                                         item["version"] + 2)
    queued = checked(api.send("manager", "POST", rework,
                              json={"expected_version": reopened["version"]}))
    assert queued["status"] == "ready"
    second = checked(api.send("manager", "POST", dispatch_path(pid, wid)))
    payload = checked(api.send(None, "GET", f"/machine/attempts/{second['attempt_id']}/package",
                               headers={"X-Instance-Token": seen.token}))
    assert [(entry["kind"], entry["attempt_number"], entry["reason"], entry["attempt_id"])
            for entry in payload["rework"]] == [
        ("requirement_acceptance", None, "对照原文，灰度开关整条没做", None)]
    assert second["input_hash"] != seen.attempt.input_hash


def test_pausing_a_requirement_stops_new_attempts_over_http(api):
    project, requirement_id, plan, attempt, token = machine_attempt(api)
    pid = project["project_id"]
    wid = plan["work_items"][0]["work_item_id"]
    actions = f"/projects/{pid}/requirements/{requirement_id}/actions"
    pauses = f"/projects/{pid}/requirements/{requirement_id}/pauses"
    version = checked(api.send("member", "GET",
                               f"/projects/{pid}/requirements/{requirement_id}"))["version"]

    checked(api.send("outsider", "GET", pauses), 404)
    assert checked(api.send("member", "GET", pauses)) == []
    checked(api.send("outsider", "POST", actions,
                     json={"action": "pause", "reason": "等答复", "expected_version": version}), 404)
    checked(api.send("member", "POST", actions,
                     json={"action": "hold", "reason": "等答复", "expected_version": version}), 422)
    checked(api.send("member", "POST", actions,
                     json={"action": "pause", "expected_version": version}), 422)
    checked(api.send("member", "POST", actions,
                     json={"action": "pause", "reason": "等答复",
                           "expected_version": version + 3}), 409)
    paused = checked(api.send("member", "POST", actions,
                              json={"action": "pause", "reason": "等提出人确认外部接口",
                                    "expected_version": version}))
    assert paused["status"] == "paused"
    # 暂停不收回已经发手的执行权：那一次尝试仍能读自己的包，重复派发也只回到那一次。
    checked(api.send(None, "GET", f"/machine/attempts/{attempt.attempt_id}/package",
                     headers={"X-Instance-Token": token}))
    assert checked(api.send("manager", "POST", dispatch_path(pid, wid)))["attempt_id"] == (
        attempt.attempt_id)
    assert [(entry["action"], entry["reason"], entry["actor_user_id"])
            for entry in checked(api.send("member", "GET", pauses))] == [
        ("paused", "等提出人确认外部接口", api.identities["member"]["user"]["user_id"])]
    resumed = checked(api.send("manager", "POST", actions,
                               json={"action": "resume", "expected_version": paused["version"]}))
    assert resumed["status"] == "executing"
    assert [entry["action"] for entry in checked(api.send("member", "GET", pauses))] == [
        "paused", "resumed"]
    checked(api.send("manager", "POST", actions,
                     json={"action": "resume", "expected_version": resumed["version"]}), 409)


def test_the_requirement_timeline_reads_the_story_off_the_audit_trail(api):
    # 动态是审计的读视图：决定当时写过什么，这里就读出什么，不需要有人再写第二遍。
    project, requirement_id, plan, attempt, token = machine_attempt(api)
    pid = project["project_id"]
    activity = f"/projects/{pid}/requirements/{requirement_id}/activity"
    checked(api.send("outsider", "GET", activity), 404)
    manager = api.identities["manager"]["user"]["user_id"]
    member = api.identities["member"]["user"]["user_id"]
    assert [(entry["action"], entry["kind"], entry["label"], entry["actor_id"])
            for entry in checked(api.send("member", "GET", activity))] == [
        ("attempt.assigned", "attempt", "第 1 次尝试 · A", manager),
        # 计划发布与需求转执行写在同一个事务里：同一刻的先后按写入顺序倒过来，不靠随机 id 猜。
        ("requirement.executing", "requirement", "需求本身", manager),
        ("plan.published", "plan", "拆解计划", manager),
        ("requirement.review_approved", "requirement", "需求本身", manager),
        ("requirement.review_requested", "requirement", "需求本身", member),
        ("requirement.revision_saved", "revision", "第 1 版原文", member),
        ("requirement.created", "requirement", "需求本身", member)]
    # 租约续期按定时器重复、被拒的回调什么也没改变：它们不该把这个故事挤掉。
    def timeline(limit=200):
        return [entry["action"] for entry in
                checked(api.send("member", "GET", f"{activity}?limit={limit}"))]

    before = timeline()
    checked(api.send(None, "POST", f"/machine/attempts/{attempt.attempt_id}/lease",
                     json=machine_claims(attempt), headers={"X-Instance-Token": token}))
    assert timeline() == before
    # 少掉的每一条都是被点名的噪音，不是漏掉的记录：同一份审计里它就在那儿。
    assert "attempt.lease" in {entry["action"] for entry in
                               checked(api.send("manager", "GET",
                                                f"/projects/{pid}/audit?limit=200"))}
    assert before[:3] == ["attempt.assigned", "requirement.executing", "plan.published"]
    checked(api.send("member", "GET", f"{activity}?limit=0"), 422)
    checked(api.send("member", "GET", f"{activity}?limit=201"), 422)


def test_one_requirements_timeline_never_carries_another_ones_events(api):
    # 一个项目的审计流水是混在一起的：读视图按归属取行，取错就等于把别人的草稿摊给全项目。
    project, requirement_id, plan, attempt, token = machine_attempt(api)
    pid = project["project_id"]
    other = checked(api.send("member", "POST", f"/projects/{pid}/requirements",
                             json={"title": "另一份需求", "content": "# 另一篇原文\n别混在一起。\n",
                                   "acceptance_items": ["两份需求互不影响"]}), 201)
    base = f"/projects/{pid}/requirements"
    mine = checked(api.send("member", "GET", f"{base}/{requirement_id}/activity?limit=200"))
    theirs = checked(api.send("member", "GET",
                              f"{base}/{other['requirement_id']}/activity?limit=200"))
    assert [(entry["action"], entry["kind"], entry["label"]) for entry in theirs] == [
        ("requirement.revision_saved", "revision", "第 1 版原文"),
        ("requirement.created", "requirement", "需求本身")]
    assert not ({entry["event_id"] for entry in mine} & {entry["event_id"] for entry in theirs})


def test_inbox_to_dos_lead_to_working_endpoints(api):
    seen = delivered(api)
    pid = seen.project["project_id"]
    wid = seen.plan["work_items"][0]["work_item_id"]
    requirement_id = seen.requirement_id
    base = f"/projects/{pid}/requirements/{requirement_id}"

    def owed(actor):
        return [(entry["item_type"], entry["action"], entry["href"]) for entry in
                checked(api.send(actor, "GET", "/inbox"))
                if not entry["item_type"].startswith("requirement_")]

    reviews = f"/projects/{pid}/attempts/{seen.attempt.attempt_id}/reviews"
    assert owed("member") == [("delivery_confirmation", "confirm_delivery", reviews)]
    assert owed("outsider") == []
    checked(api.send("member", "POST", reviews,
                     json={"decision": "confirmed", "expected_version": seen.reported["version"]}))
    assert owed("member") == []
    accept = f"/projects/{pid}/work-items/{wid}/accept"
    assert owed("manager") == [("business_acceptance", "accept_work_item", accept)]
    item = checked(api.send("manager", "GET", plans_path(seen.project, requirement_id)
                            + f"/{seen.plan['plan']['plan_id']}"))["work_items"][0]
    checked(api.send("manager", "POST", accept, json={"decision": "accepted",
                                                      "expected_version": item["version"]}))
    assert owed("manager") == []
    requirement = checked(api.send("member", "GET", base))
    stopping = checked(api.send("member", "POST", f"{base}/actions",
                                json={"action": "advance",
                                      "expected_version": requirement["version"]}))
    assert stopping["status"] == "integrating"
    check = f"{base}/integration"
    # 交付停在 integrating 时，欠的是项目管理者的一次覆盖核对，不是提出人的签字。
    assert owed("manager") == [("integration_check", "record_integration", check)]
    assert owed("member") == []
    checked(api.send("manager", "POST", check, json={"decision": "passed",
                                                     "expected_version": stopping["version"]}))
    final = f"{base}/acceptance"
    assert owed("member") == [("final_acceptance", "accept_requirement", final)]
    assert owed("manager") == []
    waiting = checked(api.send("member", "GET", base))
    checked(api.send("member", "POST", final, json={"decision": "accepted",
                                                    "expected_version": waiting["version"]}))
    assert owed("member") == [] and owed("manager") == []


def test_an_integration_return_reopens_the_delivery_and_tells_the_people(api):
    # 核对退回去的是整份交付：待办换到执行侧，原文一个字不改，动态里说得出是谁退的。
    seen = delivered(api)
    pid = seen.project["project_id"]
    wid = seen.plan["work_items"][0]["work_item_id"]
    base = f"/projects/{pid}/requirements/{seen.requirement_id}"

    def owed(actor):
        return [(entry["item_type"], entry["action"], entry["href"]) for entry in
                checked(api.send(actor, "GET", "/inbox"))
                if not entry["item_type"].startswith("requirement_")]

    checked(api.send("member", "POST", delivery_path(pid, seen.attempt.attempt_id),
                     json={"decision": "confirmed", "expected_version": seen.reported["version"]}))
    item = checked(api.send("manager", "GET", plans_path(seen.project, seen.requirement_id)
                            + f"/{seen.plan['plan']['plan_id']}"))["work_items"][0]
    checked(api.send("manager", "POST", f"/projects/{pid}/work-items/{wid}/accept",
                     json={"decision": "accepted", "expected_version": item["version"]}))
    requirement = checked(api.send("member", "GET", base))
    stopping = checked(api.send("member", "POST", f"{base}/actions",
                                json={"action": "advance",
                                      "expected_version": requirement["version"]}))
    assert stopping["status"] == "integrating"

    returned = checked(api.send("manager", "POST", f"{base}/integration",
                                json={"decision": "returned", "reason": "灰度开关整条没做",
                                      "expected_version": stopping["version"]}))
    assert (returned["status"], returned["version"]) == ("rework", stopping["version"] + 1)
    assert work_item_statuses(api, seen.project, seen.requirement_id, seen.plan) == [
        "changes_requested"]
    assert owed("manager") == [(
        "rework_queue", "rework_work_item", f"/projects/{pid}/work-items/{wid}/rework")]
    assert owed("member") == []
    after = checked(api.send("member", "GET", base))
    assert after["current_revision_id"] == requirement["current_revision_id"]

    rows = checked(api.send("member", "GET", f"{base}/activity?limit=200"))
    actions = {entry["action"] for entry in rows}
    assert {"requirement.integrating", "integration.returned"} <= actions
    source = (RUNTIME / "collaboration" / "web" / "app.js").read_text(encoding="utf-8")
    block = source.split("const NEWS = {", 1)[1].split("\n};", 1)[0]
    named = set(re.findall(r'^  "([\w.]+)":', block, re.MULTILINE))
    assert actions <= named
    # 提出人没签这一次退回，但退回的消息是他的：动态把它送到眼前，不用他刷新页面去猜。
    assert "integration.returned" in {entry["action"] for entry in
                                      checked(api.send("member", "GET", "/feed?limit=200"))}
    # 交付被退回后报告如实说"没有已验收的工作项盖住这一条"：它列事实，不替人判断。
    view = checked(api.send("member", "GET", f"{base}/integration"))
    assert [entry["decision"] for entry in view["verdicts"]] == ["returned"]
    assert view["verdicts"][0]["reason"] == "灰度开关整条没做"
    assert view["report"]["acceptance"] == [
        {"item_id": "AC-001", "text": "第一条验收", "covered_by": []}]
    assert view["report"]["uncovered"] == ["AC-001"]


def test_a_to_do_stays_owed_after_the_person_marks_it_read(api):
    seen = delivered(api)
    pid = seen.project["project_id"]
    key = f"delivery_confirmation:{pid}:{seen.attempt.attempt_id}"
    ack = "/inbox/ack"

    def rows(actor):
        return [(entry["key"], entry["unread"])
                for entry in checked(api.send(actor, "GET", "/inbox"))
                if not entry["item_type"].startswith("requirement_")]

    assert rows("member") == [(key, True)]
    checked(api.send(None, "POST", ack, json={"item_key": key}), 401)
    checked(api.send("member", "POST", ack, json={"item_key": key},
                     headers={"X-CSRF-Token": None}), 403)
    for junk in ("", " " * 3, "k" * 201, key + "f"):
        checked(api.send("member", "POST", ack, json={"item_key": junk}), 422)
    # 局外人的收件箱里没有这一条，所以连「已读」都不能替他标。
    assert rows("outsider") == []
    checked(api.send("outsider", "POST", ack, json={"item_key": key}), 422)
    checked(api.send("member", "POST", ack, json={"item_key": key}), 204)
    checked(api.send("member", "POST", ack, json={"item_key": key}), 204)
    assert rows("member") == [(key, False)]
    entry = next(item for item in checked(api.send("member", "GET", "/inbox"))
                 if not item["item_type"].startswith("requirement_"))
    # 标过已读只是少了一个红点：能写哪个动作、写去哪、对着哪个版本，一个字都没变。
    assert (entry["action"], entry["href"], entry["expected_version"]) == (
        "confirm_delivery", f"/projects/{pid}/attempts/{seen.attempt.attempt_id}/reviews",
        seen.reported["version"])
    checked(api.send("member", "POST", entry["href"],
                     json={"decision": "confirmed",
                           "expected_version": entry["expected_version"]}))
    assert rows("member") == []


def test_the_inbox_hands_back_one_page_and_reclaims_acks_that_can_never_show_again(api):
    # 两个项目各欠一份产出确认：收件箱就此有了可翻的第二页。
    delivered(api), delivered(api)
    manager = api.identities["manager"]["user"]["user_id"]

    def page(query=""):
        response = api.send("manager", "GET", f"/inbox{query}")
        return checked(response), int(response.headers["X-Total-Count"])

    whole, total = page()
    keys = [entry["key"] for entry in whole]
    assert total == len(keys) == 2 and len(set(keys)) == 2
    # 不传参数走的就是那一档默认边界，且它报的总数是「一共欠多少」，不是「这一页有几条」。
    assert page("?limit=50") == (whole, total)
    assert [page(f"?limit=1&offset={index}")[0] for index in (0, 1)] == [[whole[0]], [whole[1]]]
    assert page("?limit=1&offset=2") == ([], 2)
    for junk in ("?limit=0", "?limit=201", "?offset=-1"):
        checked(api.send("manager", "GET", f"/inbox{junk}"), 422)

    def stored():
        with api.database.read() as conn:
            return {row["item_key"] for row in conn.execute(
                "SELECT item_key FROM inbox_acks WHERE user_id=?", (manager,))}

    assert stored() == set()
    checked(api.send("manager", "POST", "/inbox/ack", json={"item_key": keys[0]}), 204)
    assert stored() == {keys[0]}
    # 分页只收窄了「一次给几条」，没有收窄「这条是不是你欠的」：第二页上的照样能标已读。
    checked(api.send("manager", "POST", "/inbox/ack", json={"item_key": keys[1]}), 204)
    assert stored() == set(keys)

    # 第一份交付签掉了，那条待办不再是任何人的：它的已读标记也一起走——没有别的东西会再把它画出来。
    checked(api.send("manager", "POST", whole[0]["href"],
                     json={"decision": "confirmed",
                           "expected_version": whole[0]["expected_version"]}))
    checked(api.send("manager", "POST", "/inbox/ack", json={"item_key": keys[1]}), 204)
    assert stored() == {keys[1]}
    remaining = page("")[0]
    assert keys[0] not in [entry["key"] for entry in remaining]
    # 签完产出立刻长出下一格要裁的（业务验收）：它是新长出来的，不该被上一轮的已读沾上。
    assert [(entry["item_type"], entry["unread"]) for entry in remaining] == [
        ("business_acceptance", True), ("delivery_confirmation", False)]
    # 另一份还挂着：标过已读只是少了一个红点，要写的动作与版本一个字都没变。
    still = next(entry for entry in remaining if entry["item_type"] == "delivery_confirmation")
    assert (still["action"], still["href"]) == ("confirm_delivery", whole[1]["href"])


def test_a_new_requirement_revision_is_unread_again(api):
    project = api.project()
    pid = project["project_id"]
    api.member(project)
    api.member(project, "manager", "manager")
    reviewer = api.identities["manager"]["user"]["user_id"]
    created = create_requirement(api, project, content="# 需求文档\n第一版")
    rid, first = created["requirement_id"], created["current_revision_id"]
    reviews = reviews_path(project, rid)

    def version():
        path = f"/projects/{pid}/requirements/{rid}"
        return checked(api.send("member", "GET", path))["version"]

    def keys(actor):
        return [(entry["key"], entry["unread"])
                for entry in checked(api.send(actor, "GET", "/inbox"))]

    checked(api.send("member", "POST", reviews, json={"action": "request", "revision_id": first,
                                                      "reviewer_user_id": reviewer,
                                                      "expected_version": version()}))
    assert keys("manager") == [(f"requirement_review:{pid}:{rid}:{first}", True)]
    checked(api.send("manager", "POST", "/inbox/ack",
                     json={"item_key": f"requirement_review:{pid}:{rid}:{first}"}), 204)
    assert keys("manager") == [(f"requirement_review:{pid}:{rid}:{first}", False)]
    checked(api.send("manager", "POST", reviews, json={"action": "reject", "revision_id": first,
                                                       "reason": "验收标准没写全",
                                                       "expected_version": version()}))
    saved = checked(api.send("member", "POST", f"/projects/{pid}/requirements/{rid}/revisions",
                             json={"content": "# 需求文档\n第一版\n补上验收标准",
                                   "expected_version": version()}), 201)
    second = saved["revision"]["revision_id"]
    checked(api.send("member", "POST", reviews, json={"action": "request", "revision_id": second,
                                                      "reviewer_user_id": reviewer,
                                                      "expected_version": version()}))
    # 上一版读过不等于这一版读过：换了版本就重新变成未读，审核人不会漏掉新内容。
    assert keys("manager") == [(f"requirement_review:{pid}:{rid}:{second}", True)]


def test_the_drafting_todos_advertise_a_review_action_that_works(api):
    project = api.project()
    pid = project["project_id"]
    api.member(project)
    api.member(project, "manager", "manager")
    manager = api.identities["manager"]["user"]["user_id"]
    created = create_requirement(api, project, content="# 需求文档\n少了验收标准")
    rid = created["requirement_id"]
    reviews = reviews_path(project, rid)

    def drafting(actor):
        return [entry for entry in checked(api.send(actor, "GET", "/inbox"))
                if entry["item_type"].startswith("requirement_")]

    assert drafting("manager") == [] and drafting("member") == []
    checked(api.send("member", "POST", f"/projects/{pid}/requirements/{rid}/messages",
                     json={"content": "导出先只支持 CSV"}), 201)
    checked(api.send("member", "POST", reviews,
                     json={"action": "request", "revision_id": created["current_revision_id"],
                           "reviewer_user_id": manager, "expected_version": created["version"]}))
    owed = drafting("manager")
    # 草稿阶段待办与执行阶段同一种契约：写回哪里、对着哪个版本、要看哪一份原文与澄清。
    assert [(entry["item_type"], entry["action"], entry["href"]) for entry in owed] == [
        ("requirement_review", "review_requirement", reviews)]
    entry = owed[0]
    live = checked(api.send("member", "GET", f"/projects/{pid}/requirements/{rid}"))
    assert (entry["expected_version"], entry["review_revision_id"], entry["unread"]) == (
        live["version"], created["current_revision_id"], True)
    # 澄清只能在起草期写，所以被点名的审核人照待办给的链接读到的就是那一份上下文。
    assert [item["content"] for item in checked(api.send("manager", "GET",
                                                         entry["links"]["messages"]))] == [
        "导出先只支持 CSV"]
    assert checked(api.send("manager", "GET", entry["links"]["revision"]))["content"] \
        == "# 需求文档\n少了验收标准"
    rejected = checked(api.send("manager", "POST", entry["href"],
                                json={"action": "reject",
                                      "revision_id": entry["review_revision_id"],
                                      "reason": "补齐验收标准",
                                      "expected_version": entry["expected_version"]}))
    assert rejected["requirement"]["status"] == "draft"
    assert drafting("manager") == []
    back = drafting("member")
    assert [(item["item_type"], item["action"]) for item in back] == [
        ("requirement_returned", "request_review")]
    # 驳回把审核人清空了，所以重送必须重新点名一位：待办不会假装还有一个默认审核人。
    assert back[0]["reviewer_id"] is None
    assert back[0]["links"]["revision"].endswith(back[0]["current_revision_id"])
    saved = checked(api.send("member", "POST", f"/projects/{pid}/requirements/{rid}/revisions",
                             json={"content": "# 需求文档\n补上验收标准",
                                   "expected_version": back[0]["expected_version"]}), 201)
    checked(api.send("member", "POST", back[0]["href"],
                     json={"action": "request", "revision_id": saved["revision"]["revision_id"],
                           "reviewer_user_id": manager,
                           "expected_version": back[0]["expected_version"]}), 409)
    again = drafting("member")[0]
    assert again["key"] != back[0]["key"] and again["unread"] is True
    assert again["current_revision_id"] == saved["revision"]["revision_id"]
    checked(api.send("member", "POST", again["href"],
                     json={"action": "request", "revision_id": again["current_revision_id"],
                           "reviewer_user_id": manager,
                           "expected_version": again["expected_version"]}))
    assert drafting("member") == []
    review = drafting("manager")[0]
    assert (review["item_type"], review["unread"], review["review_revision_id"]) == (
        "requirement_review", True, saved["revision"]["revision_id"])
    approved = checked(api.send("manager", "POST", review["href"],
                                json={"action": "approve",
                                      "revision_id": review["review_revision_id"],
                                      "expected_version": review["expected_version"]}))
    assert approved["requirement"]["status"] == "approved"
    assert drafting("manager") == [] and drafting("member") == []


def test_the_feed_delivers_news_to_the_people_it_belongs_to(api):
    # 动态按归属发：提出人、被点名的审核人、被派到活的人；自己按下的按钮不是给自己的新闻。
    project, requirement_id, plan, attempt, token = machine_attempt(api)
    pid = project["project_id"]
    manager = api.identities["manager"]["user"]["user_id"]
    member = api.identities["member"]["user"]["user_id"]

    def feed(actor):
        return [(entry["action"], entry["project_id"], entry["actor_id"], entry["label"])
                for entry in checked(api.send(actor, "GET", "/feed?limit=200"))]

    requester = feed("member")
    assert [(entry[0], entry[2], entry[3]) for entry in requester] == [
        ("attempt.assigned", manager, "第 1 次尝试 · A"),
        ("requirement.executing", manager, "需求本身"),
        ("plan.published", manager, "拆解计划"),
        ("requirement.review_approved", manager, "需求本身")]
    assert {entry[1] for entry in requester} == {pid}
    # 审核人看到的是他要批的那份文本的完整来历：归属按需求算，不按「点名那一刻之后」切。
    # 他自己做的那些（批准、发布计划、派发）不再报给他一遍。
    assert [(entry[0], entry[2], entry[3]) for entry in feed("manager")] == [
        ("requirement.review_requested", member, "需求本身"),
        ("requirement.revision_saved", member, "第 1 版原文"),
        ("requirement.created", member, "需求本身")]
    assert feed("owner") == [] and feed("outsider") == []
    # 续租仍不进动态：两份读视图共用同一张排除表，不各写一份。
    checked(api.send(None, "POST", f"/machine/attempts/{attempt.attempt_id}/lease",
                     json=machine_claims(attempt), headers={"X-Instance-Token": token}))
    assert feed("member") == requester


def test_a_feed_entry_is_acked_once_and_only_by_its_reader(api):
    project, requirement_id, plan, attempt, token = machine_attempt(api)
    mine = checked(api.send("member", "GET", "/feed?limit=200"))
    owed = [entry["key"] for entry in checked(api.send("member", "GET", "/inbox"))]
    assert [entry["unread"] for entry in mine] == [True] * len(mine)
    assert all(entry["key"] == f"activity:{entry['event_id']}" for entry in mine)
    first = mine[0]
    checked(api.send("manager", "POST", "/inbox/ack", json={"item_key": first["key"]}), 422)
    checked(api.send("member", "POST", "/inbox/ack",
                     json={"item_key": "activity:" + "0" * 32}), 422)
    checked(api.send("member", "POST", "/inbox/ack", json={"item_key": first["key"]}), 204)
    after = checked(api.send("member", "GET", "/feed?limit=200"))
    assert [(entry["action"], entry["unread"]) for entry in after] == [
        (entry["action"], entry["key"] != first["key"]) for entry in mine]
    # 已读不是裁定：标过一条动态，不该顺手清掉任何一件还欠着的决定。
    assert [entry["key"] for entry in checked(api.send("member", "GET", "/inbox"))] == owed


def test_the_feed_spans_the_projects_a_person_belongs_to(api):
    first, _, _, first_requirement = approval_setup(api)
    second, _, _, second_requirement = approval_setup(api)
    rows = checked(api.send("member", "GET", "/feed?limit=200"))
    assert [(entry["action"], entry["project_id"], entry["requirement_id"]) for entry in rows] == [
        ("requirement.review_approved", second["project_id"], second_requirement),
        ("requirement.review_approved", first["project_id"], first_requirement)]


def test_the_panel_is_static_and_gives_nobody_a_data_free_path(api):
    def asset(path):
        return api.client.request("GET", path, headers={"Origin": ORIGIN})

    page = asset("/ui/")
    assert page.status_code == 200 and page.headers["cache-control"] == "no-store"
    assert 'id="todos"' in page.text and 'id="login-form"' in page.text
    assert 'id="todoCount"' in page.text and 'id="createBox"' in page.text
    # 动态栏缺一个元素，刷新就会在 renderFeed 里抛错：整块面板跟着空白，所以这里钉住。
    assert 'id="feedList"' in page.text and 'id="feedCount"' in page.text
    assert page.headers["content-type"].startswith("text/html")
    assert asset("/ui/app.js").status_code == 200
    assert asset("/ui/styles.css").status_code == 200
    # The page is public, the data is not: without a session every read stays 401.
    checked(api.send(None, "GET", "/inbox"), 401)
    checked(api.send(None, "GET", "/projects"), 401)
    for escape in ("/ui/../collab/v1/projects", "/ui/%2e%2e/collab/v1/projects",
                   "/ui/../requirements", "/ui/../../../../etc/passwd"):
        response = asset(escape)
        # 点段在客户端就按 RFC 3986 归一化：落到 API 也要会话（401），否则 404；拿不到 200。
        assert response.status_code in (400, 401, 404), (escape, response.status_code)
    typed = asset("/ui")
    assert typed.status_code == 200 and typed.url.path == "/ui/" and 'id="todos"' in typed.text


def test_the_panel_drafting_chain_creates_clarifies_and_submits(api):
    project = api.project()
    pid = project["project_id"]
    api.member(project)
    api.member(project, "manager", "manager")
    manager = api.identities["manager"]["user"]["user_id"]

    # 面板的创建表单发的就是这一份载荷：标题 + 整篇原文 + 每行一条验收标准。
    created = checked(api.send("member", "POST", f"/projects/{pid}/requirements",
                               json={"title": "给报表加导出", "content": "# 需求原文\n按当前筛选导出。\n",
                                     "acceptance_items": ["导出行数与筛选一致"]}), 201)
    rid = created["requirement_id"]
    path = f"/projects/{pid}/requirements/{rid}"
    assert (created["status"], created["requester_id"]) == (
        "draft", api.identities["member"]["user"]["user_id"])
    posted = checked(api.send("member", "POST", path + "/messages",
                              json={"content": "导出格式先只支持 CSV"}), 201)
    assert [entry["content"] for entry in posted] == ["导出格式先只支持 CSV"]
    listed = checked(api.send("member", "GET", path + "/messages"))
    # 本服务没接澄清模型，对话里只有人写下的话：面板不能把空列表渲染成「有人回复了」。
    assert [entry["role"] for entry in listed] == ["user"]
    checked(api.send("member", "POST", path + "/reviews",
                     json={"action": "request", "revision_id": created["current_revision_id"],
                           "reviewer_user_id": manager, "expected_version": created["version"]}))
    review = [entry for entry in checked(api.send("manager", "GET", "/inbox"))
              if entry["item_type"] == "requirement_review"]
    assert [(entry["href"], entry["expected_version"], entry["unread"]) for entry in review] == [
        (path + "/reviews", created["version"] + 1, True)]
    approved = checked(api.send("manager", "POST", review[0]["href"],
                                json={"action": "approve",
                                      "revision_id": review[0]["review_revision_id"],
                                      "expected_version": review[0]["expected_version"]}))
    assert approved["requirement"]["status"] == "approved"


def test_the_panel_renders_every_kind_of_to_do_the_inbox_can_advertise():
    # 这一条守住的是本轮补上的那类静默缺口：后端新增一种待办，面板忘了承接，人就永远看不到它。
    from collaboration.inbox import ACTIONS, DRAFT_ACTIONS

    source = (RUNTIME / "collaboration" / "web" / "app.js").read_text(encoding="utf-8")
    block = source.split("const DECISIONS = {", 1)[1].split("\n};", 1)[0]
    assert set(re.findall(r"^  (\w+): \{", block, re.MULTILINE)) == set(ACTIONS) | set(DRAFT_ACTIONS)


def test_the_panel_asks_for_a_page_of_todos_and_says_what_it_left_out():
    # 后端把 /inbox 收成一次一屏之后，面板若仍按「一次拿全部」来画，屏外那些待办就悄悄不见了。
    source = (RUNTIME / "collaboration" / "web" / "app.js").read_text(encoding="utf-8")
    assert re.search(r"const TODO_LIMIT = \d+;", source)
    assert "/inbox?limit=${TODO_LIMIT}" in source
    assert "/inbox?limit=${TODO_LIMIT}&offset=${todoShown}" in source
    assert "X-Total-Count" in source
    # 没取回来多少要说得出数，还要有下一步：只写「还有更多」等于把人留在原地猜。
    assert re.search(r"还有 \$\{todoTotal - todoShown\} 条", source)
    assert re.search(r"add\(row, \"button\", \"soft\", \"再看一批\"\)", source)


def test_the_panel_remembers_which_project_and_requirement_were_open():
    # 刷新不该把人看东西的位置丢掉：选择器重建时先认这台浏览器上次停在哪一格。
    source = (RUNTIME / "collaboration" / "web" / "app.js").read_text(encoding="utf-8")
    assert "collab.panel.selection" in source
    assert "fields.project.value || remembered().project" in source
    assert "fields.requirement.value || remembered().requirement" in source
    assert "remember(projectId, requirementId)" in source
    # 那一格这一轮拿不回来要说一声，而不是静默换到第一项；退出时这把位置要交回去。
    assert 'note("上次看的那个项目这一轮不在你的列表里，先回到第一项。")' in source
    assert re.search(r"remember\(\"\", \"\"\)", source)
    # 状态条只经 flushNotice 清：谁在重画之前埋下一句话，那句话就不该被 say("") 抹掉。
    # 全文件只允许两处直接清空——flushNotice 自己，和退出登录那一次交回屏幕。
    assert source.count('say("");') == 2
    # 界面用了一个样式类而样式表里没有它，那句话说出来就跟正文一个颜色，没人会去读它。
    styles = (RUNTIME / "collaboration" / "web" / "styles.css").read_text(encoding="utf-8")
    assert ".note {" in styles


def test_the_panel_leads_from_a_verdict_to_the_files_it_signed():
    # 后端给了按尝试读产物的出口而界面没有出口，裁定行就仍然是一串要逐位比的十六进制。
    source = (RUNTIME / "collaboration" / "web" / "app.js").read_text(encoding="utf-8")
    assert "/artifacts" in source
    assert "artifactPeek(row, projectId, item)" in source
    assert "看那一次的产物" in source
    # 下载走服务端给的 href，不在界面里拿 id 拼路径。
    assert "link.href = API + entry.href;" in source
    assert "signedSameBundle(verdict.evidence_hash" in source
    assert "签的就是屏幕上这一份" in source
    assert "签的是另一份交付" in source


def test_the_panel_lets_a_return_name_the_items_it_refuses():
    # 后端接住了「只退其中几项」，界面没有出口就等于这条能力只活在 API 里。
    source = (RUNTIME / "collaboration" / "web" / "app.js").read_text(encoding="utf-8")
    assert source.count("returns: ") == 2  # 两种签字待办都能点名，别的裁定不行。
    assert 'returns: "changes_requested"' in source and 'returns: "returned"' in source
    assert "const view = await get(entry.links.delivery);" in source
    assert "body.returned_items = picked;" in source
    assert "只退其中几项" in source
    # 不勾任何一项就是整篇退回：这一格不能被读成「什么都没退」。
    assert "不勾任何一项，就是退回整篇交付" in source
    # 一次定义、两处裁定行（核对裁定与提出人裁定）都说得出点名了谁。
    assert source.count("returnScopeOf(verdict)") == 3
    assert 'verdict.decision !== "changes_requested" && verdict.decision !== "returned"' in source
    assert "只退了 ${verdict.returned_items.join" in source


def test_the_panel_names_the_binding_that_runs_the_merge_round():
    # 后端能点名开一轮，界面却没有说「谁来合」的那一格：管理者就只能在命令行里做这个决定。
    source = (RUNTIME / "collaboration" / "web" / "app.js").read_text(encoding="utf-8")
    assert source.count("opens: true") == 1  # 只有核对待办能开这一轮，签字那位没有这个权限。
    assert ('(await get(entry.links.bindings)).filter(item => item.status === "active")'
            in source)
    assert "await post(entry.links.integration_item," in source
    assert "开一轮集成" in source and "选择执行身份" in source
    # 没有活跃执行身份时不能只留一个空下拉：那读起来像「这里没人能干」，其实是还没登记一台。
    assert "这个项目没有活跃的执行身份" in source
    assert "!Number.isInteger(amount) || amount < 1" in source
    assert "集成工作项 ${item.key} 已开出来" in source
    # 挡住「核对通过」的那一行要在点之前就看得见，而不是只回一句服务器的英文。
    assert "view.pending_integration" in source
    assert "集成那一轮还开着" in source
    # 这一行不在批准的计划里，列出来就得说清来源。
    assert 'item.kind === "integration" ? " · 集成这一轮（不在批准的计划里）"' in source
    assert '"work_item.integration_created"' in source


def test_a_manager_opens_the_integration_round_over_http(api):
    project, requirement_id, plan, attempt, token = machine_attempt(
        api, acceptance=("第一条验收", "第二条验收"),
        items=lambda member, binding: [
            plan_candidate(binding, member["user_id"], "A", ["AC-001"]),
            plan_candidate(binding, member["user_id"], "B", ["AC-002"])])
    pid = project["project_id"]
    headers = {"X-Instance-Token": token}
    base = f"/projects/{pid}/requirements/{requirement_id}"
    plans = plans_path(project, requirement_id) + f"/{plan['plan']['plan_id']}"

    def finish(attempt_id, claims, work_item_id):
        checked(api.send(None, "POST", f"/machine/attempts/{attempt_id}/start", json=claims,
                         headers=headers))
        reported = checked(api.send(None, "POST", f"/machine/attempts/{attempt_id}/result",
                                    json={**claims, "status": "submitted", "summary": "按原文做完",
                                          "usage_units": 2}, headers=headers))
        checked(api.send("member", "POST", delivery_path(pid, attempt_id),
                         json={"decision": "confirmed",
                               "expected_version": reported["attempt"]["version"]}))
        stored = next(entry for entry in checked(api.send("member", "GET", plans))["work_items"]
                      if entry["work_item_id"] == work_item_id)
        checked(api.send("manager", "POST", f"/projects/{pid}/work-items/{work_item_id}/accept",
                         json={"decision": "accepted", "expected_version": stored["version"]}))

    finish(attempt.attempt_id, machine_claims(attempt), plan["work_items"][0]["work_item_id"])
    second = checked(api.send("manager", "POST",
                              dispatch_path(pid, plan["work_items"][1]["work_item_id"])))
    finish(second["attempt_id"], machine_claims(second),
           plan["work_items"][1]["work_item_id"])
    requirement = checked(api.send("member", "GET", base))
    stopping = checked(api.send("member", "POST", f"{base}/actions",
                                json={"action": "advance",
                                      "expected_version": requirement["version"]}))
    assert stopping["status"] == "integrating"
    item_path = f"{base}/integration-item"
    binding_id = plan["work_items"][0]["binding_id"]

    checked(api.send("outsider", "POST", item_path,
                     json={"binding_id": binding_id, "estimated_units": 3,
                           "expected_version": stopping["version"]}), 404)
    checked(api.send("member", "POST", item_path,
                     json={"binding_id": binding_id, "estimated_units": 3,
                           "expected_version": stopping["version"]}), 403)
    checked(api.send("manager", "POST", item_path,
                     json={"binding_id": binding_id, "estimated_units": 0,
                           "expected_version": stopping["version"]}), 422)
    checked(api.send("manager", "POST", item_path,
                     json={"binding_id": binding_id, "estimated_units": 3,
                           "expected_version": stopping["version"] + 4}), 409)
    checked(api.send("manager", "POST", item_path,
                     json={"binding_id": "0" * 32, "estimated_units": 3,
                           "expected_version": stopping["version"]}), 404)

    item = checked(api.send("manager", "POST", item_path,
                            json={"binding_id": binding_id, "estimated_units": 3,
                                  "expected_version": stopping["version"]}), 201)
    # 合并那一轮已经开着了：它没交付之前，核对不能说「可以签字」。
    checked(api.send("manager", "POST", f"{base}/integration",
                     json={"decision": "passed", "expected_version": stopping["version"]}), 409)
    # 挡住核对的是哪一行要说得出名字：核对视图带上还没交付的那一轮，界面不用让人点下去才知道。
    assert checked(api.send("member", "GET", f"{base}/integration"))["pending_integration"][
        "work_item_id"] == item["work_item_id"]
    assert (item["kind"], item["key"], item["status"], item["estimated_units"]) == (
        "integration", "integration", "ready", 3)
    assert item["assignee_user_id"] == api.identities["member"]["user"]["user_id"]
    assert checked(api.send("manager", "POST", item_path,
                            json={"binding_id": binding_id, "estimated_units": 9,
                                  "expected_version": stopping["version"]}),
                   201)["work_item_id"] == item["work_item_id"]
    listed = checked(api.send("member", "GET", plans))["work_items"]
    assert [entry["key"] for entry in listed] == ["A", "B", "integration"]
    assert checked(api.send("member", "GET", f"{base}/delivery"))["delivery"]["items"][2]["key"] \
        == "integration"

    merge = checked(api.send("manager", "POST", dispatch_path(pid, item["work_item_id"])))
    finish(merge["attempt_id"], machine_claims(merge), item["work_item_id"])
    # 那一轮交付完了就从视图里消失：核对不再被挡，挡路的那句也不该继续挂着。
    assert checked(api.send("member", "GET",
                            f"{base}/integration"))["pending_integration"] is None
    waiting = checked(api.send("manager", "POST", f"{base}/integration",
                               json={"decision": "passed",
                                     "expected_version": stopping["version"]}))
    assert waiting["status"] == "awaiting_acceptance"
    # 签字绑的是包含这次合并结果的交付：清单里第三行就是它，摘要跟着走。
    bundle = checked(api.send("member", "GET", f"{base}/delivery"))["delivery"]
    assert [(entry["key"], entry["status"], entry["attempt_number"])
            for entry in bundle["items"]] == [("A", "accepted", 1), ("B", "accepted", 1),
                                              ("integration", "accepted", 1)]
    assert bundle["evidence_hash"] == checked(
        api.send("member", "GET", f"{base}/integration"))["report"]["evidence_hash"]


def test_the_panel_offers_every_action_the_requirement_endpoint_accepts():
    # 后端多一个需求动作而面板没有按钮，等待的人就只剩命令行能停手或恢复——这条守住那道接缝。
    from typing import get_args

    from collaboration.app import RequirementAction

    source = (RUNTIME / "collaboration" / "web" / "app.js").read_text(encoding="utf-8")
    offered = set(re.findall(r'/actions`,\s*\{action: "(\w+)"', source))
    assert offered == set(get_args(RequirementAction.model_fields["action"].annotation))


def test_the_pause_reason_the_panel_banner_shows_is_readable_by_every_member(api):
    # 横幅取「最后一条 paused」，而且渲染在权限门之外：每个等待的人都要看到同一句原因。
    project, requirement_id, plan, attempt, token = machine_attempt(api)
    pid = project["project_id"]
    api.member(project, "outsider")
    actions = f"/projects/{pid}/requirements/{requirement_id}/actions"
    pauses = f"/projects/{pid}/requirements/{requirement_id}/pauses"
    first = checked(api.send("member", "GET", f"/projects/{pid}/requirements/{requirement_id}"))

    stopped = checked(api.send("member", "POST", actions, json={
        "action": "pause", "reason": "等外部接口答复", "expected_version": first["version"]}))
    back = checked(api.send("manager", "POST", actions, json={
        "action": "resume", "expected_version": stopped["version"]}))
    again = checked(api.send("member", "POST", actions, json={
        "action": "pause", "reason": "先别派，我要改一条验收项", "expected_version": back["version"]}))
    assert (again["status"], again["version"]) == ("paused", first["version"] + 3)

    history = checked(api.send("outsider", "GET", pauses))
    assert [entry["action"] for entry in history] == ["paused", "resumed", "paused"]
    # 面板用 filter(paused).pop() 取当前这一次的原因：排序一反就把它停手前的旧理由当成现在的。
    assert (history[-1]["reason"], history[-1]["actor_user_id"]) == (
        "先别派，我要改一条验收项", api.identities["member"]["user"]["user_id"])


# 需求动态与「我的动态」都是审计流水的投影：主语由服务端算好，界面只补谓语那半句。
# 值集从写它的那行代码里读，不在测试里抄一份——抄的那份会跟着代码烂掉。
_SUBJECTS = ("attempt.", "integration.", "plan.", "requirement.", "work_item.")


def _story_actions() -> set[str]:
    """Every action name the two read views can hand out, read off the writers themselves."""
    package = RUNTIME / "collaboration"
    found = set()
    for path in package.glob("*.py"):
        lines = path.read_text(encoding="utf-8").splitlines()
        for index, line in enumerate(lines):
            if "record_audit(" in line:
                found |= set(re.findall(r'f?"([a-z_]+(?:\.[a-z_]+)+)"',
                                         "\n".join(lines[index:index + 3])))
    reviews = (package / "reviews.py").read_text(encoding="utf-8")
    outcomes = re.findall(r'"(\w+)"',
                          re.search(r"_choice\(outcome, \(([^)]*)\)", reviews).group(1))
    stages = re.findall(r'stage="(\w+)"', (package / "execution.py").read_text(encoding="utf-8"))
    assert outcomes and stages  # 形状一变就明说，而不是悄悄少扫到一批动作。
    found |= {f"attempt.{stage}" for stage in stages}
    found |= {f"attempt.reconciled.{outcome}" for outcome in outcomes}
    found |= {f"requirement.{status}" for status in REQUIREMENT_TRANSITIONS}
    found |= {f"work_item.{status}" for status in WORK_ITEM_STATUSES}
    found |= {f"integration.{verdict}" for verdict in INTEGRATION_DECISIONS}
    return {action for action in found if action.startswith(_SUBJECTS)} - set(_ACTIVITY_DENIED)


def test_the_panel_has_a_verb_for_every_action_the_story_can_emit():
    # 「漏一个词只是难看」这句话由人肉守着，就会一直靠人在浏览器里撞见：这里把它钉成集合相等。
    source = (RUNTIME / "collaboration" / "web" / "app.js").read_text(encoding="utf-8")
    block = source.split("const NEWS = {", 1)[1].split("\n};", 1)[0]
    named = set(re.findall(r'^  "([\w.]+)":', block, re.MULTILINE))
    emitted = _story_actions()
    # 冒出来却没有中文谓语的那一格，时间线上就写成 attempt.artifact 这种机器名。
    assert emitted - named == set()
    # 反过来也空：留着一句再也不会发生的话，读的人就被多骗一次。
    assert named - emitted == set()


def test_the_panel_names_every_action_either_read_view_can_show(api):
    # 动态读的是审计：新动作一出现就自己冒到时间线与「我的动态」上，界面得有词称呼它。
    source = (RUNTIME / "collaboration" / "web" / "app.js").read_text(encoding="utf-8")
    block = source.split("const NEWS = {", 1)[1].split("\n};", 1)[0]
    named = set(re.findall(r'^  "([\w.]+)":', block, re.MULTILINE))
    seen = delivered(api)
    rows = checked(api.send("member", "GET",
                            f"/projects/{seen.project['project_id']}/requirements"
                            f"/{seen.requirement_id}/activity?limit=200"))
    feed = checked(api.send("member", "GET", "/feed?limit=200"))
    assert {entry["action"] for entry in rows} <= named
    assert {entry["action"] for entry in feed} <= named
    # 租约到点由服务端自己收回执行权，没有谁按下按钮：这一格同样要有中文谓语，不能等谁来补。
    assert "attempt.lease_reclaimed" in named


def test_the_coverage_report_travels_with_the_two_signatures(api):
    # 签字的人不该只看见状态变了：覆盖映射与「谁核对过」要摆在要签的那一个人眼前。
    seen = delivered(api)
    pid = seen.project["project_id"]
    wid = seen.plan["work_items"][0]["work_item_id"]
    base = f"/projects/{pid}/requirements/{seen.requirement_id}"
    checked(api.send("member", "POST", delivery_path(pid, seen.attempt.attempt_id),
                     json={"decision": "confirmed", "expected_version": seen.reported["version"]}))
    item = checked(api.send("manager", "GET", plans_path(seen.project, seen.requirement_id)
                            + f"/{seen.plan['plan']['plan_id']}"))["work_items"][0]
    checked(api.send("manager", "POST", f"/projects/{pid}/work-items/{wid}/accept",
                     json={"decision": "accepted", "expected_version": item["version"]}))
    requirement = checked(api.send("member", "GET", base))
    stopping = checked(api.send("member", "POST", f"{base}/actions",
                                json={"action": "advance",
                                      "expected_version": requirement["version"]}))
    # 两份签字待办都带着核对报告，也带着那一份交付：退回要点名工作项，客户端不该自己拼路径。
    coverage = {"coverage": f"{base}/integration", "delivery": f"{base}/delivery"}
    # 只有管理者那条核对待办多带两句：谁可以被点名，以及点名写到哪里去。
    opening = {**coverage, "bindings": f"/projects/{pid}/agent-bindings",
               "integration_item": f"{base}/integration-item"}

    def link_of(actor, item_type):
        rows = [entry for entry in checked(api.send(actor, "GET", "/inbox"))
                if entry["item_type"] == item_type]
        assert len(rows) == 1
        return rows[0]["links"]

    assert stopping["status"] == "integrating"
    # 核对待办把报告一起递到手边：管理者签之前读的就是这份映射，不用自己去拼路径。
    assert link_of("manager", "integration_check") == opening
    checked(api.send("manager", "POST", f"{base}/integration",
                     json={"decision": "passed", "expected_version": stopping["version"]}))
    # 提出人的签字待办带同一份报告：他签的是「有人核过的这一版交付」，不是「系统说完成了」。
    assert link_of("member", "final_acceptance") == coverage
    view = checked(api.send("member", "GET", f"{base}/integration"))
    assert [(entry["decision"], entry["actor_user_id"]) for entry in view["verdicts"]] == [
        ("passed", api.identities["manager"]["user"]["user_id"])]


def test_the_panel_opens_the_coverage_report_where_a_signature_is_asked():
    # 后端给了链接而界面没有出口，报告就只活在 API 里：这一条钉住那半步。
    source = (RUNTIME / "collaboration" / "web" / "app.js").read_text(encoding="utf-8")
    block = source.split("const DECISIONS = {", 1)[1].split("\n};", 1)[0]
    parts = re.split(r"^  (\w+): \{", block, flags=re.MULTILINE)
    specs = {parts[index]: parts[index + 1] for index in range(1, len(parts) - 1, 2)}
    for kind in ("final_acceptance", "integration_check"):
        assert 'look: "coverage"' in specs[kind], kind
        assert "shown: showCoverage" in specs[kind], kind
    # 需求详情区也摊开同一份报告：不签字的相关人（例如另一位管理者）也读得到核对痕迹。
    assert 'add(block, "dt", null, "覆盖核对' in source
    assert "showCoverage(block, await get(`${path}/integration`))" in source


# --- T09aa: verified backups --------------------------------------------------

def test_the_backup_command_writes_a_verified_snapshot_that_boots_as_a_service(api, tmp_path,
                                                                               capsys):
    # 第 15.2 节要的是一份记着 schema 版本与原文/产物校验结果的备份：没验过的副本等于把数据丢两次。
    project, requirement_id, plan, attempt, token = machine_attempt(api)
    pid = project["project_id"]
    headers = {"X-Instance-Token": token}
    base = f"/machine/attempts/{attempt.attempt_id}"
    started = checked(api.send(None, "POST", base + "/start", json=machine_claims(attempt),
                               headers=headers))
    content = "交付说明".encode("utf-8")
    digest = hashlib.sha256(content).hexdigest()
    checked(api.send(None, "POST", base + "/artifacts", headers=headers, json={
        **machine_claims(started["attempt"]), "name": "result.md", "media_type": "text/markdown",
        "sha256": digest, "content": base64.b64encode(content).decode("ascii")}), 201)

    archive = tmp_path / "archive" / "collab.sqlite3"
    archive.parent.mkdir()
    assert cli.main(["backup", "--data-dir", str(tmp_path), "--out", str(archive)]) == 0
    printed = json.loads(capsys.readouterr().out)
    manifest = json.loads((tmp_path / "archive" / "collab.sqlite3.manifest.json").read_text(
        encoding="utf-8"))
    assert printed == manifest
    assert manifest["schema_version"] == SCHEMA_VERSION
    assert manifest["backup"]["sha256"] == hashlib.sha256(archive.read_bytes()).hexdigest()
    assert manifest["backup"]["size_bytes"] == archive.stat().st_size
    assert manifest["verified"] == {"requirement_texts": 1, "artifacts": 1,
                                    "audit_append_only": True}
    with Database(archive).read() as conn:
        for table, count in manifest["counts"].items():
            assert conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == count, table
    assert manifest["counts"]["attempt_artifacts"] == 1 and manifest["counts"]["plans"] == 1

    # 档案要能被服务起来读，才算备份：登录态、需求与那份交付文件都还在原来的位置上。
    restored = tmp_path / "restored"
    restored.mkdir()
    (restored / "metadata.sqlite3").write_bytes(archive.read_bytes())
    session = {"Origin": ORIGIN, "Cookie": f"{COOKIE}={api.identities['member']['token']}"}
    with TestClient(create_app(AppConfig(restored, public_origin=ORIGIN)),
                    base_url=ORIGIN) as client:
        seen = checked(client.get(PREFIX + f"/projects/{pid}/requirements/{requirement_id}",
                                  headers=session))
        assert (seen["title"], seen["status"]) == ("需求文档", "executing")
        files = f"/projects/{pid}/attempts/{attempt.attempt_id}/artifacts"
        listed = checked(client.get(PREFIX + files, headers=session))
        blob = client.get(PREFIX + f"{files}/{listed[0]['artifact_id']}", headers=session)
        assert (blob.content, blob.headers["x-content-hash"]) == (content, digest)
        assert blob.headers["content-disposition"].startswith("attachment;")


def test_a_backup_whose_copy_does_not_match_its_digests_is_never_promoted(api, tmp_path, capsys):
    project, requirement_id, plan, attempt, token = machine_attempt(api)
    with sqlite3.connect(api.database.path) as conn:
        # 服务自己写不出摘要对不上的产物行，但一份坏档案正是从这样一行开始的：它不该变成备份。
        conn.execute("INSERT INTO attempt_artifacts(artifact_id,attempt_id,project_id,"
                     "work_item_id,name,media_type,size_bytes,sha256,content,created_at) "
                     "VALUES(?,?,?,?,?,?,?,?,?,?)",
                     ("bad" * 8, attempt.attempt_id, project["project_id"],
                      plan["work_items"][0]["work_item_id"], "bad.md", "text/markdown", 1,
                      "0" * 64, b"z", 1.0))
    archive = tmp_path / "archive.sqlite3"
    with pytest.raises(SystemExit) as error:
        cli.main(["backup", "--data-dir", str(tmp_path), "--out", str(archive)])
    assert error.value.code == 2
    output = capsys.readouterr()
    assert not output.out and "digest" not in output.err
    assert not archive.exists() and not (tmp_path / "archive.sqlite3.part").exists()
    assert not (tmp_path / "archive.sqlite3.manifest.json").exists()


def test_a_second_backup_never_replaces_the_first(api, tmp_path, capsys):
    machine_attempt(api)
    archive = tmp_path / "archive.sqlite3"
    assert cli.main(["backup", "--data-dir", str(tmp_path), "--out", str(archive)]) == 0
    capsys.readouterr()
    before = archive.read_bytes()
    with pytest.raises(SystemExit) as error:
        cli.main(["backup", "--data-dir", str(tmp_path), "--out", str(archive)])
    assert error.value.code == 2
    assert archive.read_bytes() == before


# --- T09ab: restore fence ------------------------------------------------------

def test_a_restored_archive_comes_back_behind_a_fence(api, tmp_path, capsys):
    # 第 15.3 节说的不是重启进程，而是把信任退回去：档案里的机器令牌与登录态都属于过去那一刻。
    project, requirement_id, plan, attempt, token = machine_attempt(
        api, acceptance=("第一条验收", "第二条验收"),
        items=lambda member, binding: [
            plan_candidate(binding, member["user_id"], "A", ["AC-001"]),
            plan_candidate(binding, member["user_id"], "B", ["AC-002"])])
    pid = project["project_id"]
    headers = {"X-Instance-Token": token}
    base = f"/machine/attempts/{attempt.attempt_id}"
    acked = checked(api.send(None, "POST", base + "/ack", json=machine_claims(attempt),
                             headers=headers))
    item_b = plan["work_items"][1]["work_item_id"]

    archive = tmp_path / "archive" / "collab.sqlite3"
    archive.parent.mkdir()
    assert cli.main(["backup", "--data-dir", str(tmp_path), "--out", str(archive)]) == 0
    capsys.readouterr()

    # 档案之后实时那一格继续往前走：B 被派了出去。恢复的意思是这一派不曾发生。
    checked(api.send("manager", "POST", dispatch_path(pid, item_b)))

    with Database(archive).read() as conn:
        signed_in = conn.execute("SELECT COUNT(*) FROM auth_sessions WHERE revoked=0").fetchone()[0]
    assert signed_in == len(api.identities)  # 五个人的登录态都被复制进了档案

    restored = tmp_path / "restored"
    restored.mkdir()
    (restored / "metadata.sqlite3").write_bytes(archive.read_bytes())
    assert cli.main(["restore-fence", "--data-dir", str(restored)]) == 0
    assert json.loads(capsys.readouterr().out) == {"nodes_disabled": 1, "instances_disabled": 1,
                                                   "sessions_revoked": signed_in}
    with Database(restored / "metadata.sqlite3").read() as conn:
        assert conn.execute("SELECT COUNT(*) FROM auth_sessions WHERE revoked=0").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM attempts").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM nodes WHERE status='active'").fetchone()[0] == 0
    # 围栏是可以重跑的：第二遍找不到任何要收回的东西。
    assert cli.main(["restore-fence", "--data-dir", str(restored)]) == 0
    assert json.loads(capsys.readouterr().out) == {"nodes_disabled": 0, "instances_disabled": 0,
                                                   "sessions_revoked": 0}

    # 档案当作服务起来：旧令牌与旧登录态一律不认，重新登录的人才读得到原来那些字。
    with TestClient(create_app(AppConfig(restored, public_origin=ORIGIN)), base_url=ORIGIN,
                    client=("127.0.0.1", 42001)) as client:
        old = {"Origin": ORIGIN, "Cookie": f"{COOKIE}={api.identities['member']['token']}"}
        checked(client.get(PREFIX + "/me", headers=old), 401)
        machine = {"Origin": ORIGIN, "X-Instance-Token": token}
        checked(client.get(PREFIX + "/machine/instances/me/attempts", headers=machine), 401)
        checked(client.post(PREFIX + base + "/start", json=machine_claims(acked["attempt"]),
                            headers=machine), 401)
        again = checked(client.post(PREFIX + "/auth/login",
                                    json={"username": "manager", "password": PASSWORD},
                                    headers={"Origin": ORIGIN}))
        fresh = {"Origin": ORIGIN, "Cookie": f"{COOKIE}={client.cookies.get(COOKIE)}",
                 "X-CSRF-Token": again["csrf_token"]}
        assert again["user"]["user_id"] == api.identities["manager"]["user"]["user_id"]
        seen = checked(client.get(PREFIX + f"/projects/{pid}/requirements/{requirement_id}",
                                  headers=fresh))
        assert (seen["title"], seen["status"]) == ("需求文档", "executing")
        # 围栏不判定任何一轮：档案里那一轮仍停在 acked，等的是去看那台机器的人。
        read = checked(client.get(PREFIX + delivery_path(pid, attempt.attempt_id), headers=fresh))
        assert (read["attempt"]["status"], read["attempt"]["control_epoch"]) == ("acked", 1)
        # 派发冻在「没有机器」这一事实上，而不是停在某人忘记撤掉的旗子上。
        checked(client.post(PREFIX + dispatch_path(pid, item_b), headers=fresh), 409)
        tick = checked(client.post(PREFIX + f"/projects/{pid}/scheduler/tick", headers=fresh))
        assert tick["work_items_changed"] == 1
        # 档案里那些没投完的事件仍然会被重新认领：两条按当下的事实成了无害的空转，第三条是
        # 「去唤醒那台已被围栏关掉的机器」，它只会被一再推迟。15.3 要的「过期派发事件作废」
        # 还没实现——围栏收走的是执行权，不是那条事件。
        assert (tick["claimed"], tick["delivered"], tick["deferred"]) == (3, 2, 1)
        detail = checked(client.get(PREFIX + plans_path(project, requirement_id)
                                    + f"/{plan['plan']['plan_id']}", headers=fresh))
        assert [entry["status"] for entry in detail["work_items"]] == ["running", "waiting_agent"]
    with Database(restored / "metadata.sqlite3").read() as conn:
        stuck = [(row[0], row[1]) for row in conn.execute(
            "SELECT topic,last_error FROM outbox_events WHERE delivered_at IS NULL")]
        assert stuck == [("attempt.assigned", "AdmissionDenied")]
        # 重放不重复造计划：需求早已走过 approved，那一条被认成「不再适用」而不是再生成一版。
        assert conn.execute("SELECT COUNT(*) FROM plans").fetchone()[0] == 1

    # 围栏只写它被指到的那一个目录：实时那一格的那台机器与那两个工作项都没被波及。
    held = checked(api.send(None, "GET", "/machine/instances/me/attempts", headers=headers))
    assert len(held) == 2
    assert work_item_statuses(api, project, requirement_id, plan) == ["running", "running"]

