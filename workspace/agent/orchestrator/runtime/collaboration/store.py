from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from contextlib import contextmanager, nullcontext
from dataclasses import asdict
from uuid import uuid4

from .database import Database
from .models import (
    Agent, AgentProjectBinding, Conflict, ExecutionInstance, ExecutionNode,
    InvalidState, NotFound, Project, ProjectMember, Requirement, RequirementMessage,
    RequirementReview, RequirementRevision, User, ValidationError,
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
    elif cls is ExecutionInstance:
        for name in ("allowed_tools", "env_allowlist"):
            if values[name] is not None:
                values[name] = tuple(json.loads(values[name]))
    return cls(**values)


def _one(conn, cls, sql, params):
    return _record(cls, conn.execute(sql, params).fetchone())


def _maybe(conn, cls, sql, params):
    row = conn.execute(sql, params).fetchone()
    return None if row is None else _record(cls, row)


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


# Credential material never leaves these tables in public projections.
_NODE_COLUMNS = "node_id,name,status,version"
_INSTANCE_COLUMNS = (
    "instance_id,binding_id,node_id,status,version,claim_expires_at,protocol_version,capacity,"
    "isolation_mode,isolation_attestation,isolation_verified,verified_at,workspace_ref,provider_ref,"
    "allowed_tools,env_allowlist,created_by,created_at,claimed_at,heartbeat_at"
)
_REQUIREMENT_COLUMNS = (
    "requirement_id,project_id,title,requester_id,status,reviewer_id,current_revision_id,"
    "review_revision_id,review_revision_hash,approved_revision_id,approved_revision_hash,"
    "version,created_at,updated_at"
)
_REQUIREMENT_SELECT = ",".join(f"r.{name}" for name in _REQUIREMENT_COLUMNS.split(","))
_REVISION_COLUMNS = (
    "revision_id,project_id,requirement_id,number,content_hash,content_size,author_id,created_at"
)
_MESSAGE_COLUMNS = "message_id,project_id,requirement_id,role,content,author_id,created_at"
_REVIEW_COLUMNS = (
    "review_id,project_id,requirement_id,revision_id,revision_hash,target_version,reviewer_id,"
    "decision,reason,created_at"
)


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

    @staticmethod
    def _binding_by_id(conn, binding_id):
        return _one(conn, AgentProjectBinding, "SELECT * FROM agent_bindings WHERE binding_id=?",
                    (binding_id,))

    @staticmethod
    def _node(conn, node_id):
        return _one(conn, ExecutionNode,
                    f"SELECT {_NODE_COLUMNS} FROM nodes WHERE node_id=?", (node_id,))

    @staticmethod
    def _instance(conn, instance_id):
        return _one(conn, ExecutionInstance,
                    f"SELECT {_INSTANCE_COLUMNS} FROM execution_instances WHERE instance_id=?",
                    (instance_id,))

    def create_node(self, name: str, credential_hash: str) -> ExecutionNode:
        _text(name, "name")
        node_id = uuid4().hex
        with self._write() as conn:
            conn.execute("INSERT INTO nodes(node_id,name,credential_hash) VALUES(?,?,?)",
                         (node_id, name, credential_hash))
            return self._node(conn, node_id)

    def get_node(self, node_id: str) -> ExecutionNode:
        with self._read() as conn:
            return self._node(conn, node_id)

    def get_node_by_credential(self, credential_hash: str) -> ExecutionNode:
        with self._read() as conn:
            return _one(conn, ExecutionNode,
                        f"SELECT {_NODE_COLUMNS} FROM nodes WHERE credential_hash=?", (credential_hash,))

    def disable_node(self, node_id: str) -> ExecutionNode:
        with self._write() as conn:
            self._active(self._node(conn, node_id))
            conn.execute("UPDATE nodes SET status='disabled',version=version+1 WHERE node_id=?", (node_id,))
            return self._node(conn, node_id)

    def get_binding_by_id(self, binding_id: str) -> AgentProjectBinding:
        with self._read() as conn:
            return self._binding_by_id(conn, binding_id)

    def create_instance(self, binding_id: str, node_id: str, *, claim_token_hash: str,
                        claim_expires_at: float, created_by: str, created_at: float) -> ExecutionInstance:
        with self._write() as conn:
            self._active(self._binding_by_id(conn, binding_id))
            self._active(self._node(conn, node_id))
            instance_id = uuid4().hex
            conn.execute(
                "INSERT INTO execution_instances(instance_id,binding_id,node_id,claim_token_hash,"
                "claim_expires_at,created_by,created_at) VALUES(?,?,?,?,?,?,?)",
                (instance_id, binding_id, node_id, claim_token_hash, claim_expires_at,
                 created_by, created_at))
            return self._instance(conn, instance_id)

    def get_instance(self, project_id: str, instance_id: str) -> ExecutionInstance:
        with self._read() as conn:
            return _one(conn, ExecutionInstance,
                        f"SELECT {_INSTANCE_COLUMNS} FROM execution_instances WHERE instance_id=? "
                        "AND binding_id IN (SELECT binding_id FROM agent_bindings WHERE project_id=?)",
                        (instance_id, project_id))

    def list_instances(self, project_id: str, binding_id: str) -> list[ExecutionInstance]:
        with self._read() as conn:
            self._binding(conn, project_id, binding_id)
            return [_record(ExecutionInstance, row) for row in conn.execute(
                f"SELECT {_INSTANCE_COLUMNS} FROM execution_instances WHERE binding_id=? "
                "ORDER BY created_at,instance_id", (binding_id,))]

    def get_instance_by_claim(self, claim_token_hash: str) -> ExecutionInstance:
        with self._read() as conn:
            return _one(conn, ExecutionInstance,
                        f"SELECT {_INSTANCE_COLUMNS} FROM execution_instances WHERE claim_token_hash=?",
                        (claim_token_hash,))

    def get_instance_by_token(self, instance_token_hash: str) -> ExecutionInstance:
        with self._read() as conn:
            return _one(conn, ExecutionInstance,
                        f"SELECT {_INSTANCE_COLUMNS} FROM execution_instances WHERE instance_token_hash=?",
                        (instance_token_hash,))

    def live_instances_for_binding(self, binding_id: str) -> list[ExecutionInstance]:
        with self._read() as conn:
            return [_record(ExecutionInstance, row) for row in conn.execute(
                f"SELECT {_INSTANCE_COLUMNS} FROM execution_instances WHERE binding_id=? "
                "AND status IN ('pending','active') ORDER BY created_at,instance_id", (binding_id,))]

    def live_instances_for_node(self, node_id: str) -> list[ExecutionInstance]:
        with self._read() as conn:
            return [_record(ExecutionInstance, row) for row in conn.execute(
                f"SELECT {_INSTANCE_COLUMNS} FROM execution_instances WHERE node_id=? "
                "AND status IN ('pending','active') ORDER BY created_at,instance_id", (node_id,))]

    def mark_claimed(self, instance_id: str, *, protocol_version: str, capacity: int,
                     isolation_mode: str, isolation_attestation: str | None,
                     workspace_ref: str, provider_ref: str, allowed_tools: str,
                     env_allowlist: str, instance_token_hash: str,
                     claimed_at: float) -> ExecutionInstance:
        with self._write() as conn:
            cursor = conn.execute(
                "UPDATE execution_instances SET status='active',version=version+1,protocol_version=?,"
                "capacity=?,isolation_mode=?,isolation_attestation=?,workspace_ref=?,provider_ref=?,"
                "allowed_tools=?,env_allowlist=?,instance_token_hash=?,claimed_at=?,heartbeat_at=? "
                "WHERE instance_id=? AND status='pending'",
                (protocol_version, capacity, isolation_mode, isolation_attestation, workspace_ref,
                 provider_ref, allowed_tools, env_allowlist, instance_token_hash, claimed_at,
                 claimed_at, instance_id))
            if cursor.rowcount != 1:
                raise Conflict("Instance is not available")
            return self._instance(conn, instance_id)

    def touch_heartbeat(self, instance_id: str, heartbeat_at: float) -> ExecutionInstance:
        with self._write() as conn:
            cursor = conn.execute(
                "UPDATE execution_instances SET heartbeat_at=? WHERE instance_id=? AND status='active'",
                (heartbeat_at, instance_id))
            if cursor.rowcount != 1:
                raise InvalidState("Instance is not active")
            return self._instance(conn, instance_id)

    def set_verified(self, instance_id: str, expected_version: int, verified_at: float) -> ExecutionInstance:
        with self._write() as conn:
            instance = self._instance(conn, instance_id)
            _version(instance.version, expected_version)
            conn.execute(
                "UPDATE execution_instances SET isolation_verified=1,verified_at=?,version=version+1 "
                "WHERE instance_id=?", (verified_at, instance_id))
            return self._instance(conn, instance_id)

    def set_disabled(self, instance_id: str, expected_version: int | None = None) -> ExecutionInstance:
        with self._write() as conn:
            instance = self._instance(conn, instance_id)
            if instance.status == "disabled":
                raise InvalidState("Instance is already disabled")
            if expected_version is not None:
                _version(instance.version, expected_version)
            conn.execute(
                "UPDATE execution_instances SET status='disabled',version=version+1 WHERE instance_id=?",
                (instance_id,))
            return self._instance(conn, instance_id)

    @staticmethod
    def _requirement(conn, project_id, requirement_id):
        return _one(conn, Requirement,
                    f"SELECT {_REQUIREMENT_COLUMNS} FROM requirements "
                    "WHERE project_id=? AND requirement_id=?",
                    (project_id, requirement_id))

    def create_requirement(self, project_id: str, title: str, requester_id: str,
                           created_at: float) -> Requirement:
        _text(title, "title")
        requirement_id = uuid4().hex
        with self._write() as conn:
            self._active(self._project(conn, project_id))
            self._active(self._user(conn, requester_id))
            self._active(self._member(conn, project_id, requester_id))
            conn.execute(
                "INSERT INTO requirements(requirement_id,project_id,title,requester_id,created_at,"
                "updated_at) VALUES(?,?,?,?,?,?)",
                (requirement_id, project_id, title, requester_id, created_at, created_at))
            return self._requirement(conn, project_id, requirement_id)

    def get_requirement(self, project_id: str, requirement_id: str) -> Requirement:
        with self._read() as conn:
            return self._requirement(conn, project_id, requirement_id)

    def list_requirements(self, project_id: str, user_id: str) -> list[Requirement]:
        with self._read() as conn:
            self._project(conn, project_id)
            return [_record(Requirement, row) for row in conn.execute(
                f"SELECT {_REQUIREMENT_COLUMNS} FROM requirements WHERE project_id=? AND "
                "(status='approved' OR requester_id=? OR (status='in_review' AND reviewer_id=?)) "
                "ORDER BY created_at,requirement_id",
                (project_id, user_id, user_id))]

    def add_revision(self, project_id: str, requirement_id: str, *, content: str, content_hash: str,
                     content_size: int, author_id: str, expected_version: int,
                     created_at: float) -> tuple[Requirement, RequirementRevision]:
        with self._write() as conn:
            requirement = self._requirement(conn, project_id, requirement_id)
            _version(requirement.version, expected_version)
            if requirement.status == "in_review":
                raise InvalidState("Cannot change a revision under review")
            self._active(self._member(conn, project_id, author_id))
            number = conn.execute(
                "SELECT COALESCE(MAX(number),0)+1 FROM requirement_revisions WHERE requirement_id=?",
                (requirement_id,)).fetchone()[0]
            revision_id = uuid4().hex
            conn.execute(
                "INSERT INTO requirement_revisions(revision_id,project_id,requirement_id,number,content,"
                "content_hash,content_size,author_id,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (revision_id, project_id, requirement_id, number, content, content_hash,
                 content_size, author_id, created_at))
            conn.execute(
                "UPDATE requirements SET current_revision_id=?,status='draft',version=version+1,"
                "updated_at=? WHERE requirement_id=?", (revision_id, created_at, requirement_id))
            return (self._requirement(conn, project_id, requirement_id),
                    self._revision(conn, project_id, requirement_id, revision_id))

    @staticmethod
    def _revision(conn, project_id, requirement_id, revision_id):
        return _one(conn, RequirementRevision,
                    f"SELECT {_REVISION_COLUMNS} FROM requirement_revisions WHERE project_id=? "
                    "AND requirement_id=? AND revision_id=?",
                    (project_id, requirement_id, revision_id))

    def list_revisions(self, project_id: str, requirement_id: str) -> list[RequirementRevision]:
        with self._read() as conn:
            self._requirement(conn, project_id, requirement_id)
            return [_record(RequirementRevision, row) for row in conn.execute(
                f"SELECT {_REVISION_COLUMNS} FROM requirement_revisions WHERE project_id=? AND "
                "requirement_id=? ORDER BY number", (project_id, requirement_id))]

    def get_revision(self, project_id: str, requirement_id: str, revision_id: str) -> RequirementRevision:
        with self._read() as conn:
            self._requirement(conn, project_id, requirement_id)
            return self._revision(conn, project_id, requirement_id, revision_id)

    def get_revision_content(self, project_id: str, requirement_id: str, revision_id: str) -> str:
        with self._read() as conn:
            self._requirement(conn, project_id, requirement_id)
            row = conn.execute(
                "SELECT content FROM requirement_revisions WHERE project_id=? AND requirement_id=? "
                "AND revision_id=?", (project_id, requirement_id, revision_id)).fetchone()
            if row is None:
                raise NotFound("Resource not found")
            return row["content"]

    def list_messages(self, project_id: str, requirement_id: str) -> list[RequirementMessage]:
        with self._read() as conn:
            self._requirement(conn, project_id, requirement_id)
            return [_record(RequirementMessage, row) for row in conn.execute(
                f"SELECT {_MESSAGE_COLUMNS} FROM requirement_messages WHERE project_id=? AND "
                "requirement_id=? ORDER BY created_at,rowid", (project_id, requirement_id))]

    def add_messages(self, project_id: str, requirement_id: str,
                     messages: list[tuple[str, str | None, str]], *, created_at: float) -> list[RequirementMessage]:
        with self._write() as conn:
            requirement = self._requirement(conn, project_id, requirement_id)
            if requirement.status != "draft":
                raise InvalidState("Clarification is only available while drafting")
            stored = []
            for role, author_id, content in messages:
                message_id = uuid4().hex
                conn.execute(
                    "INSERT INTO requirement_messages(message_id,project_id,requirement_id,role,content,"
                    "author_id,created_at) VALUES(?,?,?,?,?,?,?)",
                    (message_id, project_id, requirement_id, role, content, author_id, created_at))
                stored.append(_one(conn, RequirementMessage,
                                   f"SELECT {_MESSAGE_COLUMNS} FROM requirement_messages WHERE message_id=?",
                                   (message_id,)))
            return stored

    def request_requirement_review(self, project_id: str, requirement_id: str, *, revision_id: str,
                                   reviewer_id: str, expected_version: int,
                                   created_at: float) -> tuple[Requirement, RequirementReview]:
        with self._write() as conn:
            requirement = self._requirement(conn, project_id, requirement_id)
            _version(requirement.version, expected_version)
            if requirement.status != "draft":
                raise InvalidState("Requirement is not a draft")
            if requirement.current_revision_id != revision_id:
                raise InvalidState("Only the current revision can be reviewed")
            row = conn.execute(
                "SELECT content,content_hash FROM requirement_revisions WHERE project_id=? AND "
                "requirement_id=? AND revision_id=?",
                (project_id, requirement_id, revision_id)).fetchone()
            if row is None:
                raise NotFound("Resource not found")
            if hashlib.sha256(row["content"].encode("utf-8")).hexdigest() != row["content_hash"]:
                raise Conflict("Stored revision content does not match its digest")
            self._active(self._user(conn, reviewer_id))
            self._active(self._member(conn, project_id, reviewer_id))
            version = requirement.version + 1
            conn.execute(
                "UPDATE requirements SET status='in_review',reviewer_id=?,review_revision_id=?,"
                "review_revision_hash=?,version=?,updated_at=? WHERE requirement_id=?",
                (reviewer_id, revision_id, row["content_hash"], version, created_at, requirement_id))
            review_id = uuid4().hex
            conn.execute(
                "INSERT INTO requirement_reviews(review_id,project_id,requirement_id,revision_id,"
                "revision_hash,target_version,reviewer_id,decision,reason,created_at) "
                "VALUES(?,?,?,?,?,?,?,'requested','',?)",
                (review_id, project_id, requirement_id, revision_id, row["content_hash"],
                 version, reviewer_id, created_at))
            return (self._requirement(conn, project_id, requirement_id),
                    _one(conn, RequirementReview,
                         f"SELECT {_REVIEW_COLUMNS} FROM requirement_reviews WHERE review_id=?",
                         (review_id,)))

    def decide_requirement_review(self, project_id: str, requirement_id: str, *, revision_id: str,
                                  reviewer_id: str, decision: str, reason: str, expected_version: int,
                                  created_at: float) -> tuple[Requirement, RequirementReview]:
        with self._write() as conn:
            requirement = self._requirement(conn, project_id, requirement_id)
            _version(requirement.version, expected_version)
            if requirement.status != "in_review":
                raise InvalidState("Requirement is not under review")
            if requirement.reviewer_id != reviewer_id:
                raise InvalidState("Not the designated reviewer")
            if requirement.review_revision_id != revision_id:
                raise Conflict("Review is bound to a different revision")
            row = conn.execute(
                "SELECT content,content_hash FROM requirement_revisions WHERE project_id=? AND "
                "requirement_id=? AND revision_id=?",
                (project_id, requirement_id, revision_id)).fetchone()
            if row is None:
                raise NotFound("Resource not found")
            if (hashlib.sha256(row["content"].encode("utf-8")).hexdigest() != row["content_hash"]
                    or row["content_hash"] != requirement.review_revision_hash):
                raise Conflict("Stored revision content does not match its digest")
            version = requirement.version + 1
            if decision == "approved":
                conn.execute(
                    "UPDATE requirements SET status='approved',approved_revision_id=?,"
                    "approved_revision_hash=?,reviewer_id=NULL,review_revision_id=NULL,"
                    "review_revision_hash=NULL,version=?,updated_at=? WHERE requirement_id=?",
                    (revision_id, row["content_hash"], version, created_at, requirement_id))
            elif decision == "rejected":
                conn.execute(
                    "UPDATE requirements SET status='draft',reviewer_id=NULL,review_revision_id=NULL,"
                    "review_revision_hash=NULL,version=?,updated_at=? WHERE requirement_id=?",
                    (version, created_at, requirement_id))
            else:
                raise ValidationError("Invalid review decision")
            review_id = uuid4().hex
            conn.execute(
                "INSERT INTO requirement_reviews(review_id,project_id,requirement_id,revision_id,"
                "revision_hash,target_version,reviewer_id,decision,reason,created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?)",
                (review_id, project_id, requirement_id, revision_id, row["content_hash"],
                 version, reviewer_id, decision, reason, created_at))
            return (self._requirement(conn, project_id, requirement_id),
                    _one(conn, RequirementReview,
                         f"SELECT {_REVIEW_COLUMNS} FROM requirement_reviews WHERE review_id=?",
                         (review_id,)))

    def last_review(self, project_id: str, requirement_id: str) -> RequirementReview | None:
        with self._read() as conn:
            return _maybe(conn, RequirementReview,
                          f"SELECT {_REVIEW_COLUMNS} FROM requirement_reviews WHERE project_id=? AND "
                          "requirement_id=? ORDER BY created_at DESC,rowid DESC LIMIT 1",
                          (project_id, requirement_id))

    def list_reviews(self, project_id: str, requirement_id: str) -> list[RequirementReview]:
        with self._read() as conn:
            self._requirement(conn, project_id, requirement_id)
            return [_record(RequirementReview, row) for row in conn.execute(
                f"SELECT {_REVIEW_COLUMNS} FROM requirement_reviews WHERE project_id=? AND "
                "requirement_id=? ORDER BY created_at,rowid", (project_id, requirement_id))]

    def inbox(self, user_id: str) -> list[tuple[str, Requirement]]:
        with self._read() as conn:
            base = (f"SELECT {_REQUIREMENT_SELECT} FROM requirements r JOIN project_members m "
                    "ON m.project_id=r.project_id AND m.user_id=? AND m.status='active' ")
            items = [("requirement_review", _record(Requirement, row)) for row in conn.execute(
                base + "WHERE r.status='in_review' AND r.reviewer_id=? ", (user_id, user_id))]
            returned = conn.execute(
                base + "WHERE r.status='draft' AND r.requester_id=? AND ("
                "SELECT decision FROM requirement_reviews v WHERE v.requirement_id=r.requirement_id "
                "ORDER BY v.created_at DESC,v.rowid DESC LIMIT 1)='rejected' ", (user_id, user_id))
            items += [("requirement_returned", _record(Requirement, row)) for row in returned]
        items.sort(key=lambda item: (-item[1].updated_at, item[1].requirement_id))
        return items
