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
    Agent, AgentProjectBinding, ATTEMPT_STATUSES, Attempt, AttemptArtifact, AttemptReport,
    BudgetAccount,
    BudgetEntry, Conflict, ExecutionInstance, ExecutionNode, InvalidState, NotFound, OutboxEvent,
    OPEN_ATTEMPT_STATUSES, OwnerReview, Plan, POST_APPROVAL_STATUSES, Project, ProjectMember,
    Requirement,
    RequirementMessage, RequirementReview, RequirementRevision, REPORT_STAGES,
    RevisionAcceptanceItem,
    SecurityGrant, SecurityReview, User, ValidationError, WorkItem, WorkItemDependency,
)


REVISION_SAVE_STATUSES = ("draft", "approved", "planning_blocked")
WORK_ITEM_QUEUE_STATUSES = ("pending_dependency", "ready", "waiting_agent")
WORK_ITEM_STATUSES = (
    "pending_dependency", "ready", "waiting_agent", "running", "awaiting_owner_review",
    "submitted", "accepted", "changes_requested", "blocked", "failed", "cancelled",
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


def _digest(value: str, field: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
        raise ValidationError(f"Invalid {field}")
    return value


def _epoch(value: int, field: str) -> int:
    if type(value) is not int or value < 1:
        raise ValidationError(f"Invalid {field}")
    return value


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
    "allowed_tools,env_allowlist,created_by,created_at,claimed_at,heartbeat_at,worker_url,"
    "control_epoch"
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
_ACCEPTANCE_COLUMNS = "revision_id,requirement_id,item_id,text,position"
_PLAN_COLUMNS = (
    "plan_id,project_id,requirement_id,revision_id,revision_hash,status,report,total_units,"
    "created_by,created_at,published_at,version"
)
_WORK_ITEM_COLUMNS = (
    "work_item_id,plan_id,project_id,key,title,objective,scope,non_goals,duty,"
    "assignee_user_id,binding_id,status,estimated_units,version,created_at,updated_at"
)
_OUTBOX_COLUMNS = (
    "event_id,project_id,topic,payload,created_at,next_attempt_at,attempts,"
    "claim_expires_at,delivered_at,last_error"
)
_BUDGET_ACCOUNT_COLUMNS = "scope,ref_id,limit_units,version,updated_at"
_BUDGET_ENTRY_COLUMNS = "entry_id,scope,ref_id,ref_type,ref_key,kind,delta_units,created_at"
_ATTEMPT_COLUMNS = (
    "attempt_id,project_id,work_item_id,plan_id,requirement_id,revision_id,revision_hash,"
    "instance_id,binding_id,binding_version,assignee_user_id,number,schema_version,input_hash,"
    "package_json,status,lease_epoch,control_epoch,lease_expires_at,usage_units,result_json,"
    "result_hash,trace_id,version,created_at,updated_at"
)
_ATTEMPT_SELECT = ",".join(f"a.{name}" for name in _ATTEMPT_COLUMNS.split(","))
_REPORT_COLUMNS = (
    "report_id,attempt_id,project_id,stage,lease_epoch,control_epoch,input_hash,payload,"
    "accepted,reason,created_at"
)
_ARTIFACT_COLUMNS = (
    "artifact_id,attempt_id,project_id,work_item_id,name,media_type,size_bytes,sha256,created_at"
)
_OWNER_REVIEW_COLUMNS = (
    "review_id,attempt_id,project_id,work_item_id,reviewer_user_id,decision,result_hash,reason,"
    "created_at"
)
_SECURITY_REVIEW_COLUMNS = (
    "security_review_id,attempt_id,project_id,rule_key,input_hash,requirement_id,revision_id,"
    "binding_version,tool_policy_version,decision,reviewer_user_id,reason,expires_at,version,"
    "created_at,updated_at"
)
_SECURITY_GRANT_COLUMNS = (
    "project_id,user_id,capability,status,granted_by,version,created_at,updated_at"
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
                        claim_expires_at: float, created_by: str, created_at: float,
                        worker_url: str | None = None) -> ExecutionInstance:
        with self._write() as conn:
            self._active(self._binding_by_id(conn, binding_id))
            self._active(self._node(conn, node_id))
            instance_id = uuid4().hex
            conn.execute(
                "INSERT INTO execution_instances(instance_id,binding_id,node_id,claim_token_hash,"
                "claim_expires_at,created_by,created_at,worker_url) VALUES(?,?,?,?,?,?,?,?)",
                (instance_id, binding_id, node_id, claim_token_hash, claim_expires_at,
                 created_by, created_at, worker_url))
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
            # Revoking the instance advances its control epoch so attempts dispatched
            # under the old epoch can no longer report progress.
            conn.execute(
                "UPDATE execution_instances SET status='disabled',version=version+1,"
                "control_epoch=control_epoch+1 WHERE instance_id=?",
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
            visible = ",".join("?" * len(POST_APPROVAL_STATUSES))
            return [_record(Requirement, row) for row in conn.execute(
                f"SELECT {_REQUIREMENT_COLUMNS} FROM requirements WHERE project_id=? AND "
                f"(status IN ({visible}) OR requester_id=? "
                "OR (status='in_review' AND reviewer_id=?)) "
                "ORDER BY created_at,requirement_id",
                (project_id, *POST_APPROVAL_STATUSES, user_id, user_id))]

    def add_revision(self, project_id: str, requirement_id: str, *, content: str, content_hash: str,
                     content_size: int, author_id: str, expected_version: int,
                     created_at: float,
                     acceptance_items: tuple[tuple[str, str], ...] = ()) -> tuple[Requirement, RequirementRevision]:
        acceptance = self._acceptance_items(acceptance_items)
        with self._write() as conn:
            requirement = self._requirement(conn, project_id, requirement_id)
            _version(requirement.version, expected_version)
            if requirement.status not in REVISION_SAVE_STATUSES:
                raise InvalidState("Cannot change a revision in the current state")
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
            for position, (item_id, text) in enumerate(acceptance):
                conn.execute(
                    "INSERT INTO revision_acceptance_items(revision_id,requirement_id,item_id,text,"
                    "position) VALUES(?,?,?,?,?)",
                    (revision_id, requirement_id, item_id, text, position))
            conn.execute(
                "UPDATE requirements SET current_revision_id=?,status='draft',version=version+1,"
                "updated_at=? WHERE requirement_id=?", (revision_id, created_at, requirement_id))
            return (self._requirement(conn, project_id, requirement_id),
                    self._revision(conn, project_id, requirement_id, revision_id))

    @staticmethod
    def _acceptance_items(pairs) -> list[tuple[str, str]]:
        if not isinstance(pairs, (list, tuple)) or len(pairs) > 256:
            raise ValidationError("Invalid acceptance items")
        normalized = []
        for pair in pairs:
            if not isinstance(pair, (list, tuple)) or len(pair) != 2:
                raise ValidationError("Invalid acceptance item")
            item_id, text = pair
            if (not isinstance(item_id, str)
                    or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", item_id)):
                raise ValidationError("Invalid acceptance item id")
            _text(text, "acceptance item text", 4096)
            normalized.append((item_id, text))
        if len({item_id for item_id, _ in normalized}) != len(normalized):
            raise ValidationError("Duplicate acceptance item ids")
        return normalized

    def list_acceptance_items(self, project_id: str, requirement_id: str,
                              revision_id: str) -> list[RevisionAcceptanceItem]:
        with self._read() as conn:
            self._revision(conn, project_id, requirement_id, revision_id)
            return [_record(RevisionAcceptanceItem, row) for row in conn.execute(
                f"SELECT {_ACCEPTANCE_COLUMNS} FROM revision_acceptance_items WHERE revision_id=? "
                "AND requirement_id=? ORDER BY position,item_id", (revision_id, requirement_id))]

    def set_requirement_status(self, project_id: str, requirement_id: str, *, status: str,
                               expected_version: int, updated_at: float) -> Requirement:
        _choice(status, ("planning_blocked", "executing"), "status")
        with self._write() as conn:
            requirement = self._requirement(conn, project_id, requirement_id)
            _version(requirement.version, expected_version)
            conn.execute(
                "UPDATE requirements SET status=?,version=version+1,updated_at=? WHERE requirement_id=?",
                (status, updated_at, requirement_id))
            return self._requirement(conn, project_id, requirement_id)

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

    @staticmethod
    def _work_item_fields(raw) -> dict:
        if not isinstance(raw, dict):
            raise ValidationError("Invalid work item")
        key = raw.get("key")
        if not isinstance(key, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", key):
            raise ValidationError("Invalid work item key")
        fields = {"key": key}
        for name, limit in (("title", 512), ("objective", 8192), ("scope", 8192),
                            ("non_goals", 8192), ("duty", 8192)):
            fields[name] = _text(raw.get(name), name, limit)
        for name in ("assignee_user_id", "binding_id"):
            fields[name] = _text(raw.get(name), name)
        estimated = raw.get("estimated_units")
        if type(estimated) is not int or not 0 <= estimated <= 10 ** 9:
            raise ValidationError("Invalid estimated_units")
        fields["estimated_units"] = estimated
        depends_on = raw.get("depends_on", [])
        if (not isinstance(depends_on, (list, tuple))
                or any(not isinstance(value, str) or not value for value in depends_on)):
            raise ValidationError("Invalid depends_on")
        fields["depends_on"] = list(dict.fromkeys(depends_on))
        acceptance = raw.get("acceptance_ids", [])
        if not isinstance(acceptance, (list, tuple)):
            raise ValidationError("Invalid acceptance_ids")
        fields["acceptance_ids"] = list(dict.fromkeys(
            _text(item, "acceptance item id", 128) for item in acceptance))
        return fields

    def create_plan(self, project_id: str, requirement_id: str, *, revision_id: str,
                    revision_hash: str, status: str, report: str, total_units: int,
                    created_by: str | None, created_at: float, items=()) -> Plan:
        _choice(status, ("blocked", "published"), "status")
        if not isinstance(report, str) or not report.strip() or len(report) > 65536:
            raise ValidationError("Invalid plan report")
        if type(total_units) is not int or not 0 <= total_units <= 10 ** 12:
            raise ValidationError("Invalid plan total")
        fields = [self._work_item_fields(raw) for raw in items]
        if status == "blocked" and fields:
            raise ValidationError("A blocked plan cannot carry work items")
        with self._write() as conn:
            self._requirement(conn, project_id, requirement_id)
            plan_id = uuid4().hex
            conn.execute(
                "INSERT INTO plans(plan_id,project_id,requirement_id,revision_id,revision_hash,status,"
                "report,total_units,created_by,created_at,published_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (plan_id, project_id, requirement_id, revision_id, revision_hash, status, report,
                 total_units, created_by, created_at,
                 created_at if status == "published" else None))
            if status == "published":
                ids_by_key = {}
                for raw in fields:
                    work_item_id = uuid4().hex
                    ids_by_key[raw["key"]] = work_item_id
                    conn.execute(
                        "INSERT INTO work_items(work_item_id,plan_id,project_id,key,title,objective,"
                        "scope,non_goals,duty,assignee_user_id,binding_id,status,estimated_units,"
                        "created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (work_item_id, plan_id, project_id, raw["key"], raw["title"], raw["objective"],
                         raw["scope"], raw["non_goals"], raw["duty"], raw["assignee_user_id"],
                         raw["binding_id"],
                         "pending_dependency" if raw["depends_on"] else "ready",
                         raw["estimated_units"], created_at, created_at))
                    for position, item_id in enumerate(raw["acceptance_ids"]):
                        conn.execute(
                            "INSERT INTO work_item_acceptance_items(work_item_id,item_id,position) "
                            "VALUES(?,?,?)", (work_item_id, item_id, position))
                for raw in fields:
                    for dependency_key in raw["depends_on"]:
                        dependency_id = ids_by_key.get(dependency_key)
                        if dependency_id is None:
                            raise ValidationError("Unknown dependency reference")
                        conn.execute(
                            "INSERT INTO work_item_dependencies(work_item_id,depends_on_id,plan_id,"
                            "project_id) VALUES(?,?,?,?)",
                            (ids_by_key[raw["key"]], dependency_id, plan_id, project_id))
            return self._plan(conn, project_id, plan_id)

    @staticmethod
    def _plan(conn, project_id, plan_id):
        return _one(conn, Plan, f"SELECT {_PLAN_COLUMNS} FROM plans WHERE project_id=? AND plan_id=?",
                    (project_id, plan_id))

    def get_plan(self, project_id: str, plan_id: str) -> Plan:
        with self._read() as conn:
            return self._plan(conn, project_id, plan_id)

    def list_plans(self, project_id: str, requirement_id: str) -> list[Plan]:
        with self._read() as conn:
            self._requirement(conn, project_id, requirement_id)
            return [_record(Plan, row) for row in conn.execute(
                f"SELECT {_PLAN_COLUMNS} FROM plans WHERE project_id=? AND requirement_id=? "
                "ORDER BY created_at,rowid", (project_id, requirement_id))]

    def published_plan(self, project_id: str, requirement_id: str) -> Plan | None:
        with self._read() as conn:
            return _maybe(conn, Plan,
                          f"SELECT {_PLAN_COLUMNS} FROM plans WHERE project_id=? AND requirement_id=? "
                          "AND status='published'", (project_id, requirement_id))

    @staticmethod
    def _work_item(conn, work_item_id):
        return _one(conn, WorkItem,
                    f"SELECT {_WORK_ITEM_COLUMNS} FROM work_items WHERE work_item_id=?",
                    (work_item_id,))

    def get_work_item(self, project_id: str, work_item_id: str) -> WorkItem:
        with self._read() as conn:
            return _one(conn, WorkItem,
                        f"SELECT {_WORK_ITEM_COLUMNS} FROM work_items WHERE project_id=? AND "
                        "work_item_id=?", (project_id, work_item_id))

    def list_work_items(self, project_id: str, plan_id: str) -> list[WorkItem]:
        with self._read() as conn:
            self._plan(conn, project_id, plan_id)
            return [_record(WorkItem, row) for row in conn.execute(
                f"SELECT {_WORK_ITEM_COLUMNS} FROM work_items WHERE project_id=? AND plan_id=? "
                "ORDER BY rowid", (project_id, plan_id))]

    def list_work_item_dependencies(self, project_id: str, plan_id: str) -> list[WorkItemDependency]:
        with self._read() as conn:
            self._plan(conn, project_id, plan_id)
            return [_record(WorkItemDependency, row) for row in conn.execute(
                "SELECT work_item_id,depends_on_id,plan_id,project_id FROM work_item_dependencies "
                "WHERE project_id=? AND plan_id=? ORDER BY rowid", (project_id, plan_id))]

    def pending_work_items(self, project_id: str | None = None) -> list[WorkItem]:
        statuses = ",".join("?" * len(WORK_ITEM_QUEUE_STATUSES))
        with self._read() as conn:
            if project_id is None:
                rows = conn.execute(
                    f"SELECT {_WORK_ITEM_COLUMNS} FROM work_items WHERE status IN ({statuses}) "
                    "ORDER BY rowid", WORK_ITEM_QUEUE_STATUSES)
            else:
                rows = conn.execute(
                    f"SELECT {_WORK_ITEM_COLUMNS} FROM work_items WHERE project_id=? AND "
                    f"status IN ({statuses}) ORDER BY rowid", (project_id, *WORK_ITEM_QUEUE_STATUSES))
            return [_record(WorkItem, row) for row in rows]

    def dependency_gap_count(self, work_item_id: str) -> int:
        with self._read() as conn:
            return conn.execute(
                "SELECT COUNT(*) FROM work_item_dependencies d JOIN work_items x "
                "ON x.work_item_id=d.depends_on_id WHERE d.work_item_id=? AND x.status<>'accepted'",
                (work_item_id,)).fetchone()[0]

    def list_work_item_acceptance(self, work_item_id: str) -> list[tuple[str, str]]:
        with self._read() as conn:
            rows = conn.execute(
                "SELECT w.item_id, r.text FROM work_item_acceptance_items w "
                "JOIN work_items x ON x.work_item_id=w.work_item_id "
                "JOIN plans p ON p.plan_id=x.plan_id "
                "JOIN revision_acceptance_items r "
                "ON r.revision_id=p.revision_id AND r.item_id=w.item_id "
                "WHERE w.work_item_id=? ORDER BY w.position", (work_item_id,)).fetchall()
            return [(row["item_id"], row["text"]) for row in rows]

    def binding_has_live_instance(self, binding_id: str, *, fresh_since: float) -> bool:
        with self._read() as conn:
            return conn.execute(
                "SELECT 1 FROM execution_instances WHERE binding_id=? AND status='active' "
                "AND heartbeat_at IS NOT NULL AND heartbeat_at>=? LIMIT 1",
                (binding_id, fresh_since)).fetchone() is not None

    def advance_work_item(self, work_item_id: str, *, status: str, expected_version: int,
                          updated_at: float) -> WorkItem:
        _choice(status, WORK_ITEM_STATUSES, "status")
        with self._write() as conn:
            item = self._work_item(conn, work_item_id)
            _version(item.version, expected_version)
            conn.execute(
                "UPDATE work_items SET status=?,version=version+1,updated_at=? WHERE work_item_id=?",
                (status, updated_at, work_item_id))
            return self._work_item(conn, work_item_id)

    def enqueue_event(self, project_id: str, topic: str, payload: dict, created_at: float) -> OutboxEvent:
        _text(topic, "topic", 128)
        if not isinstance(payload, dict):
            raise ValidationError("Invalid event payload")
        serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        if len(serialized) > 65536:
            raise ValidationError("Event payload is too large")
        with self._write() as conn:
            event_id = uuid4().hex
            conn.execute(
                "INSERT INTO outbox_events(event_id,project_id,topic,payload,created_at,"
                "next_attempt_at) VALUES(?,?,?,?,?,?)",
                (event_id, project_id, topic, serialized, created_at, created_at))
            return _one(conn, OutboxEvent,
                        f"SELECT {_OUTBOX_COLUMNS} FROM outbox_events WHERE event_id=?", (event_id,))

    def get_event(self, event_id: str) -> OutboxEvent:
        with self._read() as conn:
            return _one(conn, OutboxEvent,
                        f"SELECT {_OUTBOX_COLUMNS} FROM outbox_events WHERE event_id=?", (event_id,))

    def list_events(self, project_id: str | None = None, *, limit: int = 200) -> list[OutboxEvent]:
        with self._read() as conn:
            if project_id is None:
                rows = conn.execute(
                    f"SELECT {_OUTBOX_COLUMNS} FROM outbox_events ORDER BY created_at DESC,"
                    "event_id DESC LIMIT ?", (limit,))
            else:
                rows = conn.execute(
                    f"SELECT {_OUTBOX_COLUMNS} FROM outbox_events WHERE project_id=? "
                    "ORDER BY created_at DESC,event_id DESC LIMIT ?", (project_id, limit))
            return [_record(OutboxEvent, row) for row in rows]

    def claim_due_events(self, *, now: float, lease_ttl: int, batch_size: int,
                         project_id: str | None = None) -> list[OutboxEvent]:
        with self._write() as conn:
            if project_id is None:
                rows = conn.execute(
                    f"SELECT {_OUTBOX_COLUMNS} FROM outbox_events WHERE delivered_at IS NULL "
                    "AND next_attempt_at<=? AND (claim_expires_at IS NULL OR claim_expires_at<=?) "
                    "ORDER BY next_attempt_at,created_at,event_id LIMIT ?",
                    (now, now, batch_size)).fetchall()
            else:
                rows = conn.execute(
                    f"SELECT {_OUTBOX_COLUMNS} FROM outbox_events WHERE project_id=? "
                    "AND delivered_at IS NULL AND next_attempt_at<=? "
                    "AND (claim_expires_at IS NULL OR claim_expires_at<=?) "
                    "ORDER BY next_attempt_at,created_at,event_id LIMIT ?",
                    (project_id, now, now, batch_size)).fetchall()
            claimed = []
            for row in rows:
                claim_expires_at = now + lease_ttl
                conn.execute("UPDATE outbox_events SET claim_expires_at=? WHERE event_id=?",
                             (claim_expires_at, row["event_id"]))
                values = dict(row)
                values["claim_expires_at"] = claim_expires_at
                claimed.append(OutboxEvent(**values))
            return claimed

    def mark_event_delivered(self, event_id: str, *, claim_expires_at: float,
                             delivered_at: float) -> bool:
        with self._write() as conn:
            cursor = conn.execute(
                "UPDATE outbox_events SET delivered_at=?,claim_expires_at=NULL "
                "WHERE event_id=? AND claim_expires_at=?",
                (delivered_at, event_id, claim_expires_at))
            return cursor.rowcount == 1

    def defer_event(self, event_id: str, *, claim_expires_at: float, next_attempt_at: float,
                    last_error: str | None) -> bool:
        with self._write() as conn:
            cursor = conn.execute(
                "UPDATE outbox_events SET attempts=attempts+1,next_attempt_at=?,claim_expires_at=NULL,"
                "last_error=? WHERE event_id=? AND claim_expires_at=?",
                (next_attempt_at, last_error, event_id, claim_expires_at))
            return cursor.rowcount == 1

    def get_budget_account(self, scope: str, ref_id: str) -> BudgetAccount | None:
        with self._read() as conn:
            return _maybe(conn, BudgetAccount,
                          f"SELECT {_BUDGET_ACCOUNT_COLUMNS} FROM budget_accounts "
                          "WHERE scope=? AND ref_id=?", (scope, ref_id))

    def list_budget_accounts(self, project_id: str | None = None) -> list[BudgetAccount]:
        with self._read() as conn:
            if project_id is None:
                rows = conn.execute(
                    f"SELECT {_BUDGET_ACCOUNT_COLUMNS} FROM budget_accounts ORDER BY scope,ref_id")
            else:
                rows = conn.execute(
                    f"SELECT {_BUDGET_ACCOUNT_COLUMNS} FROM budget_accounts WHERE scope='global' "
                    "OR (scope='project' AND ref_id=?) OR (scope='requirement' AND ref_id IN "
                    "(SELECT requirement_id FROM requirements WHERE project_id=?)) "
                    "ORDER BY scope,ref_id", (project_id, project_id))
            return [_record(BudgetAccount, row) for row in rows]

    def upsert_budget_account(self, scope: str, ref_id: str, *, limit_units: int,
                              expected_version: int | None, updated_at: float) -> BudgetAccount:
        _choice(scope, ("global", "project", "requirement"), "scope")
        if scope == "global":
            if ref_id != "":
                raise ValidationError("The global budget uses an empty reference")
        elif not isinstance(ref_id, str) or not ref_id.strip() or len(ref_id) > 64:
            raise ValidationError("Invalid budget reference")
        if type(limit_units) is not int or not 0 <= limit_units <= 10 ** 12:
            raise ValidationError("Invalid budget limit")
        with self._write() as conn:
            row = conn.execute("SELECT * FROM budget_accounts WHERE scope=? AND ref_id=?",
                               (scope, ref_id)).fetchone()
            if row is None:
                if expected_version is not None:
                    raise Conflict("Budget account does not exist")
                conn.execute(
                    "INSERT INTO budget_accounts(scope,ref_id,limit_units,version,updated_at) "
                    "VALUES(?,?,?,1,?)", (scope, ref_id, limit_units, updated_at))
            else:
                if expected_version is None:
                    raise Conflict("expected_version is required for an existing budget account")
                _version(row["version"], expected_version)
                conn.execute(
                    "UPDATE budget_accounts SET limit_units=?,version=version+1,updated_at=? "
                    "WHERE scope=? AND ref_id=?", (limit_units, updated_at, scope, ref_id))
            return _one(conn, BudgetAccount,
                        f"SELECT {_BUDGET_ACCOUNT_COLUMNS} FROM budget_accounts "
                        "WHERE scope=? AND ref_id=?", (scope, ref_id))

    def add_budget_entry(self, scope: str, ref_id: str, *, ref_type: str, ref_key: str, kind: str,
                         delta_units: int, created_at: float) -> BudgetEntry:
        _choice(scope, ("global", "project", "requirement"), "scope")
        _choice(ref_type, ("plan", "work_item", "attempt", "adjustment"), "ref_type")
        _choice(kind, ("reserve", "settle", "release", "adjustment"), "kind")
        _text(ref_key, "ref_key", 128)
        if type(delta_units) is not int or not -10 ** 12 <= delta_units <= 10 ** 12:
            raise ValidationError("Invalid budget delta")
        with self._write() as conn:
            conn.execute(
                "INSERT INTO budget_entries(entry_id,scope,ref_id,ref_type,ref_key,kind,delta_units,"
                "created_at) VALUES(?,?,?,?,?,?,?,?) "
                "ON CONFLICT(scope,ref_id,ref_type,ref_key,kind) DO NOTHING",
                (uuid4().hex, scope, ref_id, ref_type, ref_key, kind, delta_units, created_at))
            return _one(conn, BudgetEntry,
                        f"SELECT {_BUDGET_ENTRY_COLUMNS} FROM budget_entries WHERE scope=? AND ref_id=? "
                        "AND ref_type=? AND ref_key=? AND kind=?",
                        (scope, ref_id, ref_type, ref_key, kind))

    def list_budget_entries(self, scope: str, ref_id: str) -> list[BudgetEntry]:
        with self._read() as conn:
            return [_record(BudgetEntry, row) for row in conn.execute(
                f"SELECT {_BUDGET_ENTRY_COLUMNS} FROM budget_entries WHERE scope=? AND ref_id=? "
                "ORDER BY created_at,rowid", (scope, ref_id))]

    # --- execution attempts -----------------------------------------------------

    def _attempt_row(self, conn, attempt_id: str) -> Attempt:
        return _one(conn, Attempt,
                    f"SELECT {_ATTEMPT_COLUMNS} FROM attempts WHERE attempt_id=?", (attempt_id,))

    def create_attempt(self, project_id: str, work_item_id: str, *, plan_id: str,
                       requirement_id: str, revision_id: str, revision_hash: str,
                       instance_id: str, binding_id: str, binding_version: int,
                       assignee_user_id: str, number: int, schema_version: str, input_hash: str,
                       package_json: str, control_epoch: int, lease_expires_at: float,
                       created_at: float) -> Attempt:
        _text(schema_version, "schema_version", 64)
        _digest(revision_hash, "revision_hash")
        _digest(input_hash, "input_hash")
        _epoch(binding_version, "binding_version")
        _epoch(control_epoch, "control_epoch")
        if type(number) is not int or number < 1:
            raise ValidationError("Invalid attempt number")
        if not isinstance(package_json, str) or not package_json.strip():
            raise ValidationError("Invalid task package")
        attempt_id = uuid4().hex
        with self._write() as conn:
            conn.execute(
                "INSERT INTO attempts(attempt_id,project_id,work_item_id,plan_id,requirement_id,"
                "revision_id,revision_hash,instance_id,binding_id,binding_version,"
                "assignee_user_id,number,schema_version,input_hash,package_json,status,"
                "lease_epoch,control_epoch,lease_expires_at,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'assigned',1,?,?,?,?)",
                (attempt_id, project_id, work_item_id, plan_id, requirement_id, revision_id,
                 revision_hash, instance_id, binding_id, binding_version, assignee_user_id,
                 number, schema_version, input_hash, package_json, control_epoch,
                 lease_expires_at, created_at, created_at))
            return self._attempt_row(conn, attempt_id)

    def get_attempt(self, project_id: str, attempt_id: str) -> Attempt:
        with self._read() as conn:
            attempt = self._attempt_row(conn, attempt_id)
            if attempt.project_id != project_id:
                raise NotFound("Resource not found")
            return attempt

    def list_attempts(self, project_id: str, work_item_id: str) -> list[Attempt]:
        with self._read() as conn:
            if self._work_item(conn, work_item_id).project_id != project_id:
                raise NotFound("Resource not found")
            return [_record(Attempt, row) for row in conn.execute(
                f"SELECT {_ATTEMPT_COLUMNS} FROM attempts WHERE project_id=? AND work_item_id=? "
                "ORDER BY number", (project_id, work_item_id))]

    def attempts_for_instance(self, instance_id: str) -> list[Attempt]:
        with self._read() as conn:
            return [_record(Attempt, row) for row in conn.execute(
                f"SELECT {_ATTEMPT_COLUMNS} FROM attempts WHERE instance_id=? "
                "ORDER BY created_at DESC,rowid", (instance_id,))]

    def open_attempt(self, work_item_id: str) -> Attempt | None:
        placeholders = ",".join("?" * len(OPEN_ATTEMPT_STATUSES))
        with self._read() as conn:
            row = conn.execute(
                f"SELECT {_ATTEMPT_COLUMNS} FROM attempts WHERE work_item_id=? "
                f"AND status IN ({placeholders}) ORDER BY number LIMIT 1",
                (work_item_id, *OPEN_ATTEMPT_STATUSES)).fetchone()
            return None if row is None else _record(Attempt, row)

    def next_attempt_number(self, work_item_id: str) -> int:
        with self._read() as conn:
            return conn.execute("SELECT COALESCE(MAX(number),0)+1 FROM attempts "
                                "WHERE work_item_id=?", (work_item_id,)).fetchone()[0]

    def bump_attempt_lease(self, attempt_id: str, *, control_epoch: int,
                           lease_expires_at: float, updated_at: float) -> Attempt:
        """Take over the lease: only a live attempt under the current control epoch."""
        with self._write() as conn:
            attempt = self._attempt_row(conn, attempt_id)
            if attempt.control_epoch != control_epoch:
                raise Conflict("Control epoch changed")
            if attempt.status not in OPEN_ATTEMPT_STATUSES:
                raise InvalidState("Attempt is no longer open")
            conn.execute(
                "UPDATE attempts SET lease_epoch=lease_epoch+1,lease_expires_at=?,"
                "version=version+1,updated_at=? WHERE attempt_id=?",
                (lease_expires_at, updated_at, attempt_id))
            return self._attempt_row(conn, attempt_id)

    def advance_attempt(self, attempt_id: str, *, status: str, expected_version: int,
                        updated_at: float, lease_epoch: int | None = None,
                        control_epoch: int | None = None, result_json: str | None = None,
                        result_hash: str | None = None, usage_units: int | None = None,
                        trace_id: str | None = None) -> Attempt:
        _choice(status, ATTEMPT_STATUSES, "status")
        if result_hash is not None:
            _digest(result_hash, "result_hash")
        if usage_units is not None and (type(usage_units) is not int or usage_units < 0):
            raise ValidationError("Invalid usage")
        with self._write() as conn:
            attempt = self._attempt_row(conn, attempt_id)
            _version(attempt.version, expected_version)
            if lease_epoch is not None and attempt.lease_epoch != lease_epoch:
                raise Conflict("Lease epoch changed")
            if control_epoch is not None and attempt.control_epoch != control_epoch:
                raise Conflict("Control epoch changed")
            assignments = ["status=?", "version=version+1", "updated_at=?"]
            values: list = [status, updated_at]
            for column, value in (("result_json", result_json), ("result_hash", result_hash),
                                  ("usage_units", usage_units), ("trace_id", trace_id)):
                if value is not None:
                    assignments.append(f"{column}=?")
                    values.append(value)
            cursor = conn.execute(
                f"UPDATE attempts SET {','.join(assignments)} WHERE attempt_id=? AND version=?",
                (*values, attempt_id, attempt.version))
            if cursor.rowcount != 1:
                raise Conflict("Version changed")
            return self._attempt_row(conn, attempt_id)

    def set_attempt_control_epoch(self, instance_id: str, control_epoch: int) -> int:
        """Freeze the open attempts of a revoked instance so late reports cannot advance."""
        placeholders = ",".join("?" * len(OPEN_ATTEMPT_STATUSES))
        with self._write() as conn:
            cursor = conn.execute(
                f"UPDATE attempts SET control_epoch=?,version=version+1 "
                f"WHERE instance_id=? AND status IN ({placeholders})",
                (control_epoch, instance_id, *OPEN_ATTEMPT_STATUSES))
            return cursor.rowcount

    def revoke_attempt_execution_right(self, attempt_id: str, *, expected_version: int,
                                       updated_at: float) -> Attempt:
        """Move one open attempt past its instance epoch so later reports only leave evidence."""
        with self._write() as conn:
            attempt = self._attempt_row(conn, attempt_id)
            _version(attempt.version, expected_version)
            if attempt.status not in OPEN_ATTEMPT_STATUSES:
                raise InvalidState("Attempt is no longer open")
            cursor = conn.execute(
                "UPDATE attempts SET control_epoch=control_epoch+1,version=version+1,"
                "updated_at=? WHERE attempt_id=? AND version=?",
                (updated_at, attempt_id, attempt.version))
            if cursor.rowcount != 1:
                raise Conflict("Version changed")
            return self._attempt_row(conn, attempt_id)

    # --- callback reports -------------------------------------------------------

    def record_attempt_report(self, attempt_id: str, *, stage: str, lease_epoch: int,
                              control_epoch: int, input_hash: str, payload: str,
                              accepted: bool, reason: str | None,
                              created_at: float) -> AttemptReport:
        _choice(stage, REPORT_STAGES, "stage")
        _digest(input_hash, "input_hash")
        for value, field in ((lease_epoch, "lease_epoch"), (control_epoch, "control_epoch")):
            if type(value) is not int or value < 0:
                raise ValidationError(f"Invalid {field}")
        if not isinstance(payload, str) or len(payload) > 262144:
            raise ValidationError("Invalid report payload")
        report_id = uuid4().hex
        with self._write() as conn:
            attempt = self._attempt_row(conn, attempt_id)
            conn.execute(
                "INSERT INTO attempt_reports(report_id,attempt_id,project_id,stage,lease_epoch,"
                "control_epoch,input_hash,payload,accepted,reason,created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (report_id, attempt_id, attempt.project_id, stage, lease_epoch, control_epoch,
                 input_hash, payload, 1 if accepted else 0,
                 None if reason is None else _text(reason, "reason", 512), created_at))
            return _one(conn, AttemptReport,
                        f"SELECT {_REPORT_COLUMNS} FROM attempt_reports WHERE report_id=?",
                        (report_id,))

    def list_attempt_reports(self, project_id: str, attempt_id: str) -> list[AttemptReport]:
        with self._read() as conn:
            self.get_attempt(project_id, attempt_id)
            return [_record(AttemptReport, row) for row in conn.execute(
                f"SELECT {_REPORT_COLUMNS} FROM attempt_reports WHERE attempt_id=? "
                "ORDER BY created_at,rowid", (attempt_id,))]

    # --- artifacts --------------------------------------------------------------

    def add_attempt_artifact(self, attempt_id: str, *, name: str, media_type: str, content: bytes,
                             sha256: str, created_at: float) -> AttemptArtifact:
        _text(name, "artifact name", 512)
        _text(media_type, "media_type", 128)
        _digest(sha256, "sha256")
        if not isinstance(content, bytes):
            raise ValidationError("Artifact content must be bytes")
        if len(content) > 32 * 1024 * 1024:
            raise ValidationError("Artifact exceeds the maximum size")
        if hashlib.sha256(content).hexdigest() != sha256:
            raise ValidationError("Artifact digest mismatch")
        artifact_id = uuid4().hex
        with self._write() as conn:
            attempt = self._attempt_row(conn, attempt_id)
            conn.execute(
                "INSERT INTO attempt_artifacts(artifact_id,attempt_id,project_id,work_item_id,"
                "name,media_type,size_bytes,sha256,content,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (artifact_id, attempt_id, attempt.project_id, attempt.work_item_id, name,
                 media_type, len(content), sha256, sqlite3.Binary(content), created_at))
            return _one(conn, AttemptArtifact,
                        f"SELECT {_ARTIFACT_COLUMNS} FROM attempt_artifacts WHERE artifact_id=?",
                        (artifact_id,))

    def get_artifact_by_name(self, attempt_id: str, name: str) -> AttemptArtifact | None:
        with self._read() as conn:
            row = conn.execute(
                f"SELECT {_ARTIFACT_COLUMNS} FROM attempt_artifacts WHERE attempt_id=? AND name=?",
                (attempt_id, name)).fetchone()
            return None if row is None else _record(AttemptArtifact, row)

    def get_artifact(self, project_id: str, artifact_id: str) -> AttemptArtifact:
        with self._read() as conn:
            artifact = _one(conn, AttemptArtifact,
                            f"SELECT {_ARTIFACT_COLUMNS} FROM attempt_artifacts WHERE artifact_id=?",
                            (artifact_id,))
            if artifact.project_id != project_id:
                raise NotFound("Resource not found")
            return artifact

    def get_artifact_content(self, project_id: str, artifact_id: str) -> bytes:
        with self._read() as conn:
            self.get_artifact(project_id, artifact_id)
            data = conn.execute("SELECT content FROM attempt_artifacts WHERE artifact_id=?",
                                (artifact_id,)).fetchone()[0]
            return bytes(data)

    def list_artifacts(self, project_id: str, attempt_id: str) -> list[AttemptArtifact]:
        with self._read() as conn:
            self.get_attempt(project_id, attempt_id)
            return [_record(AttemptArtifact, row) for row in conn.execute(
                f"SELECT {_ARTIFACT_COLUMNS} FROM attempt_artifacts WHERE attempt_id=? "
                "ORDER BY name", (attempt_id,))]

    # --- owner review -----------------------------------------------------------

    def add_owner_review(self, attempt_id: str, *, reviewer_user_id: str, decision: str,
                         result_hash: str, reason: str, created_at: float) -> OwnerReview:
        _choice(decision, ("confirmed", "changes_requested"), "decision")
        _digest(result_hash, "result_hash")
        review_id = uuid4().hex
        with self._write() as conn:
            attempt = self._attempt_row(conn, attempt_id)
            conn.execute(
                "INSERT INTO owner_reviews(review_id,attempt_id,project_id,work_item_id,"
                "reviewer_user_id,decision,result_hash,reason,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (review_id, attempt_id, attempt.project_id, attempt.work_item_id, reviewer_user_id,
                 decision, result_hash, reason, created_at))
            return _one(conn, OwnerReview,
                        f"SELECT {_OWNER_REVIEW_COLUMNS} FROM owner_reviews WHERE review_id=?",
                        (review_id,))

    def get_owner_review(self, attempt_id: str) -> OwnerReview | None:
        with self._read() as conn:
            row = conn.execute(
                f"SELECT {_OWNER_REVIEW_COLUMNS} FROM owner_reviews WHERE attempt_id=?",
                (attempt_id,)).fetchone()
            return None if row is None else _record(OwnerReview, row)

    # --- security gate ----------------------------------------------------------

    def add_security_review(self, attempt_id: str, *, rule_key: str, requirement_id: str,
                            revision_id: str, binding_version: int, tool_policy_version: str,
                            expires_at: float, created_at: float) -> SecurityReview:
        _text(rule_key, "rule_key", 128)
        _epoch(binding_version, "binding_version")
        _digest(tool_policy_version, "tool_policy_version")
        if type(expires_at) not in (int, float) or expires_at <= created_at:
            raise ValidationError("Invalid security review expiry")
        security_review_id = uuid4().hex
        with self._write() as conn:
            attempt = self._attempt_row(conn, attempt_id)
            conn.execute(
                "INSERT INTO security_reviews(security_review_id,attempt_id,project_id,rule_key,"
                "input_hash,requirement_id,revision_id,binding_version,tool_policy_version,"
                "decision,reason,expires_at,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,"
                "'pending','',?,?,?)",
                (security_review_id, attempt_id, attempt.project_id, rule_key, attempt.input_hash,
                 requirement_id, revision_id, binding_version, tool_policy_version, expires_at,
                 created_at, created_at))
            return self._security_review(conn, security_review_id)

    def _security_review(self, conn, security_review_id: str) -> SecurityReview:
        return _one(conn, SecurityReview,
                    f"SELECT {_SECURITY_REVIEW_COLUMNS} FROM security_reviews "
                    "WHERE security_review_id=?", (security_review_id,))

    def security_review_by_rule(self, attempt_id: str, rule_key: str) -> SecurityReview | None:
        with self._read() as conn:
            row = conn.execute(
                f"SELECT {_SECURITY_REVIEW_COLUMNS} FROM security_reviews WHERE attempt_id=? "
                "AND rule_key=?", (attempt_id, rule_key)).fetchone()
            return None if row is None else _record(SecurityReview, row)

    def list_security_reviews(self, project_id: str, attempt_id: str) -> list[SecurityReview]:
        with self._read() as conn:
            self.get_attempt(project_id, attempt_id)
            return [_record(SecurityReview, row) for row in conn.execute(
                f"SELECT {_SECURITY_REVIEW_COLUMNS} FROM security_reviews WHERE attempt_id=? "
                "ORDER BY rule_key", (attempt_id,))]

    def get_security_review(self, project_id: str, security_review_id: str) -> SecurityReview:
        with self._read() as conn:
            review = self._security_review(conn, security_review_id)
            if review.project_id != project_id:
                raise NotFound("Resource not found")
            return review

    def decide_security_review(self, security_review_id: str, *, decision: str,
                               reviewer_user_id: str, reason: str, expected_version: int,
                               updated_at: float) -> SecurityReview:
        _choice(decision, ("approved", "rejected"), "decision")
        with self._write() as conn:
            review = self._security_review(conn, security_review_id)
            _version(review.version, expected_version)
            if review.decision != "pending":
                raise InvalidState("Security review is already decided")
            cursor = conn.execute(
                "UPDATE security_reviews SET decision=?,reviewer_user_id=?,reason=?,"
                "version=version+1,updated_at=? WHERE security_review_id=? AND version=?",
                (decision, reviewer_user_id, reason, updated_at, security_review_id,
                 review.version))
            if cursor.rowcount != 1:
                raise Conflict("Version changed")
            return self._security_review(conn, security_review_id)

    def get_security_grant(self, project_id: str, user_id: str,
                           capability: str) -> SecurityGrant | None:
        with self._read() as conn:
            row = conn.execute(
                f"SELECT {_SECURITY_GRANT_COLUMNS} FROM security_grants WHERE project_id=? "
                "AND user_id=? AND capability=?", (project_id, user_id, capability)).fetchone()
            return None if row is None else _record(SecurityGrant, row)

    def upsert_security_grant(self, project_id: str, user_id: str, *, capability: str,
                              granted_by: str, active: bool, created_at: float,
                              expected_version: int | None = None) -> SecurityGrant:
        _choice(capability, ("security:approve",), "capability")
        with self._write() as conn:
            current = self.get_security_grant(project_id, user_id, capability)
            if current is None:
                if expected_version is not None:
                    raise Conflict("Version changed")
                conn.execute(
                    "INSERT INTO security_grants(project_id,user_id,capability,status,granted_by,"
                    "created_at,updated_at) VALUES(?,?,?,'active',?,?,?)",
                    (project_id, user_id, capability, granted_by, created_at, created_at))
            else:
                _version(current.version, expected_version if expected_version is not None
                         else current.version)
                conn.execute(
                    "UPDATE security_grants SET status=?,granted_by=?,version=version+1,"
                    "updated_at=? WHERE project_id=? AND user_id=? AND capability=?",
                    ("active" if active else "revoked", granted_by, created_at, project_id,
                     user_id, capability))
            return _one(conn, SecurityGrant,
                        f"SELECT {_SECURITY_GRANT_COLUMNS} FROM security_grants WHERE project_id=? "
                        "AND user_id=? AND capability=?", (project_id, user_id, capability))

    def list_security_grants(self, project_id: str) -> list[SecurityGrant]:
        with self._read() as conn:
            return [_record(SecurityGrant, row) for row in conn.execute(
                f"SELECT {_SECURITY_GRANT_COLUMNS} FROM security_grants WHERE project_id=? "
                "ORDER BY user_id", (project_id,))]
