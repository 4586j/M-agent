from __future__ import annotations

import json
import re
import sqlite3
from contextlib import contextmanager, nullcontext
from dataclasses import asdict
from uuid import uuid4

from .database import Database
from .models import (
    Agent, AgentProjectBinding, Conflict, InvalidState, NotFound,
    Project, ProjectMember, User, ValidationError,
)


def _text(value: str, field: str, limit: int = 512) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise ValidationError(f"Invalid {field}")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        raise ValidationError(f"Invalid {field}") from None
    return value


def _choice(value: str, choices: tuple[str, ...], field: str) -> str:
    if value not in choices:
        raise ValidationError(f"Invalid {field}")
    return value


def _version(actual: int, expected: int) -> None:
    if type(expected) is not int or expected < 1:
        raise ValidationError("expected_version must be a positive integer")
    if actual != expected:
        raise Conflict("Version changed")


def _record(cls, row):
    if row is None:
        raise NotFound("Resource not found")
    values = dict(row)
    if cls is AgentProjectBinding:
        values["allowed_tools"] = tuple(json.loads(values["allowed_tools"]))
    return cls(**values)


def _one(conn, cls, sql, params):
    return _record(cls, conn.execute(sql, params).fetchone())


def _binding_fields(duty, persona_version, allowed_tools, workspace_ref, provider_ref):
    _text(duty, "duty", 8192)
    _text(persona_version, "persona_version")
    for name, value in (("workspace_ref", workspace_ref), ("provider_ref", provider_ref)):
        if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", value):
            raise ValidationError(f"{name} must be an opaque reference")
    if not isinstance(allowed_tools, (list, tuple)) or len(allowed_tools) > 256:
        raise ValidationError("Invalid allowed_tools")
    for tool in allowed_tools:
        _text(tool, "tool", 128)
    if len(set(allowed_tools)) != len(allowed_tools):
        raise ValidationError("Duplicate tools")
    return duty, persona_version, json.dumps(allowed_tools), workspace_ref, provider_ref


class CollaborationStore:
    def __init__(self, database: Database, *, connection: sqlite3.Connection | None = None):
        self.database = database
        self.connection = connection
        if connection is not None and not connection.in_transaction:
            raise InvalidState("A bound store requires an active transaction")

    def _read(self):
        return nullcontext(self.connection) if self.connection is not None else self.database.read()

    @contextmanager
    def _write(self):
        try:
            context = nullcontext(self.connection) if self.connection is not None else self.database.transaction()
            with context as conn:
                yield conn
        except sqlite3.IntegrityError as exc:
            raise Conflict("Identity or uniqueness constraint violated") from exc

    @staticmethod
    def _user(conn, user_id):
        return _one(conn, User, "SELECT * FROM users WHERE user_id=?", (user_id,))

    @staticmethod
    def _project(conn, project_id):
        return _one(conn, Project, "SELECT * FROM projects WHERE project_id=?", (project_id,))

    @staticmethod
    def _member(conn, project_id, user_id):
        return _one(conn, ProjectMember,
                    "SELECT * FROM project_members WHERE project_id=? AND user_id=?",
                    (project_id, user_id))

    @staticmethod
    def _agent(conn, agent_id):
        return _one(conn, Agent, "SELECT * FROM agents WHERE agent_id=?", (agent_id,))

    @staticmethod
    def _binding(conn, project_id, binding_id):
        return _one(conn, AgentProjectBinding,
                    "SELECT * FROM agent_bindings WHERE project_id=? AND binding_id=?",
                    (project_id, binding_id))

    @staticmethod
    def _active(record):
        if record.status != "active":
            raise InvalidState("Resource is not active")
        return record

    def _eligible(self, conn, project_id, agent_id, member_user_id):
        self._active(self._project(conn, project_id))
        self._active(self._user(conn, member_user_id))
        self._active(self._member(conn, project_id, member_user_id))
        agent = self._active(self._agent(conn, agent_id))
        if agent.owner_user_id != member_user_id:
            raise ValidationError("Agent owner must equal binding member")

    @staticmethod
    def _snapshot(conn, binding):
        conn.execute(
            "INSERT INTO binding_revisions(binding_id,version,snapshot) VALUES(?,?,?)",
            (binding.binding_id, binding.version,
             json.dumps(asdict(binding), ensure_ascii=False, sort_keys=True)),
        )

    def _revoke(self, conn, bindings):
        for binding in bindings:
            conn.execute(
                "UPDATE agent_bindings SET status='revoked',version=version+1 WHERE binding_id=?",
                (binding.binding_id,),
            )
            self._snapshot(conn, self._binding(conn, binding.project_id, binding.binding_id))

    def create_user(self, display_name: str) -> User:
        _text(display_name, "display_name")
        user_id = uuid4().hex
        with self._write() as conn:
            conn.execute("INSERT INTO users(user_id,display_name) VALUES(?,?)", (user_id, display_name))
            return self._user(conn, user_id)

    def get_user(self, user_id: str) -> User:
        with self._read() as conn:
            return self._user(conn, user_id)

    def update_user(self, user_id: str, *, display_name: str, status: str, expected_version: int) -> User:
        _text(display_name, "display_name")
        _choice(status, ("active", "disabled"), "status")
        with self._write() as conn:
            _version(self._user(conn, user_id).version, expected_version)
            conn.execute("UPDATE users SET display_name=?,status=?,version=version+1 WHERE user_id=?",
                         (display_name, status, user_id))
            if status == "disabled":
                bindings = [_record(AgentProjectBinding, row) for row in conn.execute(
                    "SELECT * FROM agent_bindings WHERE member_user_id=? AND status='active'", (user_id,))]
                self._revoke(conn, bindings)
            return self._user(conn, user_id)

    def create_project(self, name: str, owner_id: str) -> Project:
        _text(name, "name")
        project_id = uuid4().hex
        with self._write() as conn:
            self._active(self._user(conn, owner_id))
            conn.execute("INSERT INTO projects(project_id,name,owner_id) VALUES(?,?,?)",
                         (project_id, name, owner_id))
            conn.execute("INSERT INTO project_members(project_id,user_id,access_role) VALUES(?,?,'owner')",
                         (project_id, owner_id))
            return self._project(conn, project_id)

    def get_project(self, project_id: str) -> Project:
        with self._read() as conn:
            return self._project(conn, project_id)

    def list_projects(self, user_id: str) -> list[Project]:
        with self._read() as conn:
            self._active(self._user(conn, user_id))
            return [_record(Project, row) for row in conn.execute(
                "SELECT p.* FROM projects p JOIN project_members m ON m.project_id=p.project_id "
                "WHERE m.user_id=? AND m.status='active' ORDER BY p.project_id", (user_id,))]

    def update_project(self, project_id: str, *, name: str, status: str, expected_version: int) -> Project:
        _text(name, "name")
        _choice(status, ("active", "archived"), "status")
        with self._write() as conn:
            project = self._active(self._project(conn, project_id))
            _version(project.version, expected_version)
            conn.execute("UPDATE projects SET name=?,status=?,version=version+1 WHERE project_id=?",
                         (name, status, project_id))
            if status == "archived":
                bindings = [_record(AgentProjectBinding, row) for row in conn.execute(
                    "SELECT * FROM agent_bindings WHERE project_id=? AND status='active'", (project_id,))]
                self._revoke(conn, bindings)
            return self._project(conn, project_id)

    def add_member(self, project_id: str, user_id: str, *, access_role: str = "member") -> ProjectMember:
        _choice(access_role, ("manager", "reviewer", "member"), "access_role")
        with self._write() as conn:
            self._active(self._project(conn, project_id))
            self._active(self._user(conn, user_id))
            conn.execute("INSERT INTO project_members(project_id,user_id,access_role) VALUES(?,?,?)",
                         (project_id, user_id, access_role))
            return self._member(conn, project_id, user_id)

    def get_member(self, project_id: str, user_id: str) -> ProjectMember:
        with self._read() as conn:
            return self._member(conn, project_id, user_id)

    def list_members(self, project_id: str) -> list[ProjectMember]:
        with self._read() as conn:
            self._project(conn, project_id)
            return [_record(ProjectMember, row) for row in conn.execute(
                "SELECT * FROM project_members WHERE project_id=? ORDER BY user_id", (project_id,))]

    def update_member(self, project_id: str, user_id: str, *, access_role: str,
                      status: str, expected_version: int) -> ProjectMember:
        _choice(access_role, ("owner", "manager", "reviewer", "member"), "access_role")
        _choice(status, ("active", "removed"), "status")
        with self._write() as conn:
            project = self._active(self._project(conn, project_id))
            member = self._member(conn, project_id, user_id)
            _version(member.version, expected_version)
            is_owner = project.owner_id == user_id
            if (access_role == "owner") != is_owner or (is_owner and status != "active"):
                raise ValidationError("Project owner membership cannot be removed or reassigned")
            if status == "active":
                self._active(self._user(conn, user_id))
            conn.execute(
                "UPDATE project_members SET access_role=?,status=?,version=version+1 "
                "WHERE project_id=? AND user_id=?", (access_role, status, project_id, user_id))
            if status == "removed":
                bindings = [_record(AgentProjectBinding, row) for row in conn.execute(
                    "SELECT * FROM agent_bindings WHERE project_id=? AND member_user_id=? AND status='active'",
                    (project_id, user_id))]
                self._revoke(conn, bindings)
            return self._member(conn, project_id, user_id)

    def create_agent(self, owner_user_id: str, name: str) -> Agent:
        _text(name, "name")
        agent_id = uuid4().hex
        with self._write() as conn:
            self._active(self._user(conn, owner_user_id))
            conn.execute("INSERT INTO agents(agent_id,owner_user_id,name) VALUES(?,?,?)",
                         (agent_id, owner_user_id, name))
            return self._agent(conn, agent_id)

    def get_agent(self, agent_id: str, owner_user_id: str) -> Agent:
        with self._read() as conn:
            return _one(conn, Agent, "SELECT * FROM agents WHERE agent_id=? AND owner_user_id=?",
                        (agent_id, owner_user_id))

    def list_agents(self, owner_user_id: str) -> list[Agent]:
        with self._read() as conn:
            self._active(self._user(conn, owner_user_id))
            return [_record(Agent, row) for row in conn.execute(
                "SELECT * FROM agents WHERE owner_user_id=? ORDER BY agent_id", (owner_user_id,))]

    def update_agent(self, agent_id: str, owner_user_id: str, *, name: str,
                     status: str, expected_version: int) -> Agent:
        _text(name, "name")
        _choice(status, ("active", "disabled"), "status")
        with self._write() as conn:
            agent = _one(conn, Agent, "SELECT * FROM agents WHERE agent_id=? AND owner_user_id=?",
                         (agent_id, owner_user_id))
            _version(agent.version, expected_version)
            conn.execute("UPDATE agents SET name=?,status=?,version=version+1 WHERE agent_id=?",
                         (name, status, agent_id))
            if status == "disabled":
                bindings = [_record(AgentProjectBinding, row) for row in conn.execute(
                    "SELECT * FROM agent_bindings WHERE agent_id=? AND status='active'", (agent_id,))]
                self._revoke(conn, bindings)
            return self._agent(conn, agent_id)

    def create_binding(self, project_id: str, agent_id: str, member_user_id: str, *,
                       duty: str, persona_version: str, allowed_tools: tuple[str, ...],
                       workspace_ref: str, provider_ref: str) -> AgentProjectBinding:
        fields = _binding_fields(duty, persona_version, allowed_tools, workspace_ref, provider_ref)
        binding_id = uuid4().hex
        with self._write() as conn:
            self._eligible(conn, project_id, agent_id, member_user_id)
            conn.execute(
                "INSERT INTO agent_bindings(binding_id,project_id,agent_id,member_user_id,duty,"
                "persona_version,allowed_tools,workspace_ref,provider_ref) VALUES(?,?,?,?,?,?,?,?,?)",
                (binding_id, project_id, agent_id, member_user_id, *fields))
            binding = self._binding(conn, project_id, binding_id)
            self._snapshot(conn, binding)
            return binding

    def get_binding(self, project_id: str, binding_id: str) -> AgentProjectBinding:
        with self._read() as conn:
            return self._binding(conn, project_id, binding_id)

    def list_bindings(self, project_id: str) -> list[AgentProjectBinding]:
        with self._read() as conn:
            self._project(conn, project_id)
            return [_record(AgentProjectBinding, row) for row in conn.execute(
                "SELECT * FROM agent_bindings WHERE project_id=? ORDER BY binding_id", (project_id,))]

    def update_binding(self, project_id: str, binding_id: str, *, expected_version: int,
                       duty: str, persona_version: str, allowed_tools: tuple[str, ...],
                       workspace_ref: str, provider_ref: str) -> AgentProjectBinding:
        fields = _binding_fields(duty, persona_version, allowed_tools, workspace_ref, provider_ref)
        with self._write() as conn:
            current = self._active(self._binding(conn, project_id, binding_id))
            _version(current.version, expected_version)
            self._eligible(conn, project_id, current.agent_id, current.member_user_id)
            conn.execute(
                "UPDATE agent_bindings SET duty=?,persona_version=?,allowed_tools=?,workspace_ref=?,"
                "provider_ref=?,version=version+1 WHERE binding_id=?", (*fields, binding_id))
            binding = self._binding(conn, project_id, binding_id)
            self._snapshot(conn, binding)
            return binding

    def revoke_binding(self, project_id: str, binding_id: str, *, expected_version: int) -> AgentProjectBinding:
        with self._write() as conn:
            current = self._active(self._binding(conn, project_id, binding_id))
            _version(current.version, expected_version)
            self._revoke(conn, [current])
            return self._binding(conn, project_id, binding_id)

    def get_binding_revision(self, project_id: str, binding_id: str, version: int) -> AgentProjectBinding:
        with self._read() as conn:
            self._binding(conn, project_id, binding_id)
            row = conn.execute("SELECT snapshot FROM binding_revisions WHERE binding_id=? AND version=?",
                               (binding_id, version)).fetchone()
            if row is None:
                raise NotFound("Binding revision not found")
            values = json.loads(row["snapshot"])
            values["allowed_tools"] = tuple(values["allowed_tools"])
            return AgentProjectBinding(**values)

    def validate_binding(self, project_id: str, binding_id: str, *, expected_version: int) -> AgentProjectBinding:
        with self._read() as conn:
            binding = self._active(self._binding(conn, project_id, binding_id))
            _version(binding.version, expected_version)
            self._eligible(conn, project_id, binding.agent_id, binding.member_user_id)
            return binding
