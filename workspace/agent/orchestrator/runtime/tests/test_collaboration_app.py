"""HTTP/CLI contract tests using real authentication and temporary SQLite databases."""
from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass
import json
from pathlib import Path
import sqlite3
import subprocess
import sys

import pytest
from fastapi.testclient import TestClient

RUNTIME = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RUNTIME))

from collaboration import __main__ as cli
from collaboration.app import AppConfig, COOKIE, PREFIX, create_app
from collaboration.database import Database
from collaboration.store import CollaborationStore

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
    assert checked(api.send("owner", "GET", f"/projects/{pid}")) == project
    member = api.member(project)
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
    assert len(checked(api.send("member", "GET", f"/projects/{pid}/members"))) == 2
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
    assert checked(api.send("owner", "GET", f"/projects/{project['project_id']}")) == project


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
