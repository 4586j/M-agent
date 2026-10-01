"""Machine identity, execution-instance registration and admission checks.

Isolation is claimed by the machine and manually attested by the project owner;
the service records that attestation but never proves it. Instances that declare
``isolation_mode='none'`` can only be admitted for test data.
"""
from __future__ import annotations

import json
import re
import secrets
import time
from dataclasses import dataclass

from .auth import Access, Unauthorized, record_audit, token_digest
from .database import Database
from .models import (
    AgentProjectBinding, Conflict, ExecutionInstance, ExecutionNode,
    InvalidState, NotFound, ValidationError,
)
from .store import CollaborationStore, _text, _version

SUPPORTED_PROTOCOLS = ("1",)
ISOLATION_MODES = ("none", "os_account", "container", "vm")
_ENV_DENY_TOKENS = frozenset({"KEY", "TOKEN", "SECRET", "PASSWORD", "PASSWD",
                              "CREDENTIAL", "CREDENTIALS", "CREDS"})
_ENV_DENY_PREFIXES = ("ORCH_", "AGENT_", "COLLAB_", "ANTHROPIC_", "OPENAI_",
                      "AWS_", "AZURE_", "GCP_", "GOOGLE_", "HF_")


class AdmissionDenied(InvalidState):
    """An execution instance is not allowed to receive this work."""

    def __init__(self, reason: str):
        super().__init__(f"Instance is not admitted: {reason}")
        self.reason = reason


@dataclass(frozen=True)
class Admission:
    instance: ExecutionInstance
    binding: AgentProjectBinding
    real_data: bool


def _ref(value, field: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", value):
        raise ValidationError(f"Invalid {field}")
    return value


def _isolation(mode: str, attestation: str) -> tuple[str, str | None]:
    if mode not in ISOLATION_MODES:
        raise ValidationError("Invalid isolation_mode")
    if not isinstance(attestation, str) or len(attestation) > 1024:
        raise ValidationError("Invalid isolation_attestation")
    try:
        attestation = attestation.strip()
        attestation.encode("utf-8")
    except UnicodeEncodeError:
        raise ValidationError("Invalid isolation_attestation") from None
    if mode == "none":
        if attestation:
            raise ValidationError("Attestation requires a real isolation mode")
        return mode, None
    if not attestation:
        raise ValidationError("Isolation attestation is required")
    return mode, attestation


def _tool_policy(names) -> tuple[str, ...]:
    if not isinstance(names, (list, tuple)) or len(names) > 256:
        raise ValidationError("Invalid allowed_tools")
    tools = tuple(_text(name, "tool", 128) for name in names)
    if len(set(tools)) != len(tools):
        raise ValidationError("Duplicate tools")
    return tools


def _env_allowlist(names) -> tuple[str, ...]:
    if not isinstance(names, (list, tuple)) or len(names) > 64:
        raise ValidationError("Invalid env_allowlist")
    allowed = tuple(names)
    for name in allowed:
        if not isinstance(name, str) or not re.fullmatch(r"[A-Z][A-Z0-9_]{0,63}", name):
            raise ValidationError("env_allowlist entries must be uppercase variable names")
        if any(part in _ENV_DENY_TOKENS for part in name.split("_")) \
                or name.startswith(_ENV_DENY_PREFIXES):
            raise ValidationError("Credential-bearing environment names are not allowed")
    if len(set(allowed)) != len(allowed):
        raise ValidationError("Duplicate environment names")
    return allowed


class InstanceService:
    """Registers execution slots and admits work to claimed instances."""

    def __init__(self, database: Database, *, claim_ttl: int = 86400,
                 heartbeat_timeout: int = 120, clock=time.time):
        if type(claim_ttl) is not int or not 60 <= claim_ttl <= 604800:
            raise ValidationError("Invalid claim_ttl")
        if type(heartbeat_timeout) is not int or not 30 <= heartbeat_timeout <= 3600:
            raise ValidationError("Invalid heartbeat_timeout")
        self.database = database
        self.claim_ttl = claim_ttl
        self.heartbeat_timeout = heartbeat_timeout
        self.clock = clock

    def enroll_node(self, name: str) -> tuple[ExecutionNode, str]:
        _text(name, "name")
        token = secrets.token_urlsafe(32)
        now = self.clock()
        with self.database.transaction() as conn:
            store = CollaborationStore(self.database, connection=conn)
            node = store.create_node(name, token_digest(token))
            record_audit(conn, now, None, "node.enrolled", node.node_id,
                         target_version=node.version)
            return node, token

    def disable_node(self, node_id: str) -> tuple[ExecutionNode, list[ExecutionInstance]]:
        now = self.clock()
        with self.database.transaction() as conn:
            store = CollaborationStore(self.database, connection=conn)
            node = store.disable_node(node_id)
            record_audit(conn, now, None, "node.disabled", node_id, target_version=node.version)
            disabled = []
            for instance in store.live_instances_for_node(node_id):
                updated = store.set_disabled(instance.instance_id)
                binding = store.get_binding_by_id(instance.binding_id)
                record_audit(conn, now, None, "instance.disabled", instance.instance_id,
                             project_id=binding.project_id, target_version=updated.version)
                disabled.append(updated)
            return node, disabled

    def register_slot(self, access: Access, *, project_id: str, binding_id: str,
                      node_id: str) -> tuple[ExecutionInstance, str]:
        claim_token = secrets.token_urlsafe(32)
        now = self.clock()
        binding = access.store.get_binding(project_id, binding_id)
        if binding.status != "active":
            raise InvalidState("Binding is not active")
        node = access.store.get_node(node_id)
        if node.status != "active":
            raise InvalidState("Node is not active")
        instance = access.store.create_instance(
            binding_id, node_id, claim_token_hash=token_digest(claim_token),
            claim_expires_at=now + self.claim_ttl, created_by=access.user.user_id,
            created_at=now)
        record_audit(access.conn, now, access.user.user_id, "instance.registered",
                     instance.instance_id, project_id=project_id, target_version=instance.version)
        return instance, claim_token

    def _scoped_instance(self, access: Access, project_id: str, binding_id: str,
                         instance_id: str) -> ExecutionInstance:
        instance = access.store.get_instance(project_id, instance_id)
        if instance.binding_id != binding_id:
            raise NotFound("Resource not found")
        return instance

    def verify(self, access: Access, *, project_id: str, binding_id: str,
               instance_id: str, expected_version: int) -> ExecutionInstance:
        instance = self._scoped_instance(access, project_id, binding_id, instance_id)
        if instance.status != "active":
            raise InvalidState("Instance is not active")
        if instance.isolation_mode in (None, "none"):
            raise InvalidState("Instance declares no OS-level isolation")
        if instance.isolation_verified:
            raise InvalidState("Instance isolation is already verified")
        now = self.clock()
        updated = access.store.set_verified(instance_id, expected_version, now)
        record_audit(access.conn, now, access.user.user_id, "instance.verified",
                     instance_id, project_id=project_id, target_version=updated.version)
        return updated

    def disable(self, access: Access, *, project_id: str, binding_id: str,
                instance_id: str, expected_version: int | None = None) -> ExecutionInstance:
        self._scoped_instance(access, project_id, binding_id, instance_id)
        now = self.clock()
        updated = access.store.set_disabled(instance_id, expected_version)
        record_audit(access.conn, now, access.user.user_id, "instance.disabled",
                     instance_id, project_id=project_id, target_version=updated.version)
        return updated

    def disable_for_binding(self, access: Access, *, project_id: str,
                            binding_id: str) -> list[ExecutionInstance]:
        disabled = []
        now = self.clock()
        for instance in access.store.live_instances_for_binding(binding_id):
            updated = access.store.set_disabled(instance.instance_id)
            record_audit(access.conn, now, access.user.user_id, "instance.disabled",
                         instance.instance_id, project_id=project_id, target_version=updated.version)
            disabled.append(updated)
        return disabled

    def claim(self, node_token: str | None, claim_token: str | None, *,
              protocol_version: str, capacity: int, isolation_mode: str,
              isolation_attestation: str, workspace_ref: str, provider_ref: str,
              allowed_tools, env_allowlist) -> tuple[ExecutionInstance, str, AgentProjectBinding]:
        node_hash = token_digest(node_token)
        try:
            claim_hash = token_digest(claim_token)
        except Unauthorized:
            raise Conflict("Instance claim is not available") from None
        if protocol_version not in SUPPORTED_PROTOCOLS:
            raise ValidationError("Unsupported protocol_version")
        if type(capacity) is not int or not 1 <= capacity <= 64:
            raise ValidationError("Invalid capacity")
        mode, attestation = _isolation(isolation_mode, isolation_attestation)
        tools = _tool_policy(allowed_tools)
        environment = _env_allowlist(env_allowlist)
        workspace_ref = _ref(workspace_ref, "workspace_ref")
        provider_ref = _ref(provider_ref, "provider_ref")
        now = self.clock()
        instance_token = secrets.token_urlsafe(32)
        with self.database.transaction() as conn:
            store = CollaborationStore(self.database, connection=conn)
            try:
                node = store.get_node_by_credential(node_hash)
            except NotFound:
                raise Unauthorized("Authentication required") from None
            if node.status != "active":
                raise Unauthorized("Authentication required")
            try:
                instance = store.get_instance_by_claim(claim_hash)
            except NotFound:
                raise Conflict("Instance claim is not available") from None
            if (instance.status != "pending" or instance.claim_expires_at <= now
                    or instance.node_id != node.node_id):
                raise Conflict("Instance claim is not available")
            binding = store.get_binding_by_id(instance.binding_id)
            try:
                binding = store.validate_binding(binding.project_id, binding.binding_id,
                                                 expected_version=binding.version)
            except (Conflict, InvalidState, NotFound, ValidationError):
                raise Conflict("Instance claim is not available") from None
            if workspace_ref != binding.workspace_ref or provider_ref != binding.provider_ref:
                raise ValidationError("Workspace or provider does not match the binding")
            if not set(tools).issubset(set(binding.allowed_tools)):
                raise ValidationError("Tool policy exceeds the binding")
            updated = store.mark_claimed(
                instance.instance_id, protocol_version=protocol_version, capacity=capacity,
                isolation_mode=mode, isolation_attestation=attestation,
                workspace_ref=workspace_ref, provider_ref=provider_ref,
                allowed_tools=json.dumps(tools), env_allowlist=json.dumps(environment),
                instance_token_hash=token_digest(instance_token), claimed_at=now)
            record_audit(conn, now, None, "instance.claimed", instance.instance_id,
                         project_id=binding.project_id, target_version=updated.version)
            return updated, instance_token, binding

    def heartbeat(self, instance_token: str | None) -> ExecutionInstance:
        digest = token_digest(instance_token)
        now = self.clock()
        with self.database.transaction() as conn:
            store = CollaborationStore(self.database, connection=conn)
            instance = self._live_identity(store, digest)
            # Heartbeats are too frequent to audit; state changes carry the audit trail.
            return store.touch_heartbeat(instance.instance_id, now)

    def describe(self, instance_token: str | None) -> tuple[ExecutionInstance, AgentProjectBinding]:
        digest = token_digest(instance_token)
        with self.database.read() as conn:
            store = CollaborationStore(self.database, connection=conn)
            instance = self._live_identity(store, digest)
            return instance, store.get_binding_by_id(instance.binding_id)

    @staticmethod
    def _live_identity(store: CollaborationStore, digest: str) -> ExecutionInstance:
        try:
            instance = store.get_instance_by_token(digest)
        except NotFound:
            raise Unauthorized("Authentication required") from None
        if instance.status != "active":
            raise Unauthorized("Authentication required")
        node = store.get_node(instance.node_id)
        if node.status != "active":
            raise Unauthorized("Authentication required")
        if store.get_binding_by_id(instance.binding_id).status != "active":
            raise Unauthorized("Authentication required")
        return instance

    def admit(self, project_id: str, binding_id: str, instance_id: str, *,
              requires_real_data: bool, expected_binding_version: int | None = None) -> Admission:
        if type(requires_real_data) is not bool:
            raise ValidationError("requires_real_data must be a boolean")
        now = self.clock()
        with self.database.read() as conn:
            store = CollaborationStore(self.database, connection=conn)
            instance = store.get_instance(project_id, instance_id)
            if instance.binding_id != binding_id:
                raise NotFound("Resource not found")
            if instance.status != "active":
                raise AdmissionDenied("instance_inactive")
            binding = store.get_binding_by_id(binding_id)
            if expected_binding_version is not None:
                _version(binding.version, expected_binding_version)
            if binding.status != "active":
                raise AdmissionDenied("binding_inactive")
            try:
                binding = store.validate_binding(project_id, binding_id,
                                                 expected_version=binding.version)
            except (InvalidState, NotFound, ValidationError):
                raise AdmissionDenied("identity_mismatch") from None
            if store.get_node(instance.node_id).status != "active":
                raise AdmissionDenied("node_disabled")
            if instance.heartbeat_at is None or now - instance.heartbeat_at > self.heartbeat_timeout:
                raise AdmissionDenied("heartbeat_stale")
            if (instance.workspace_ref != binding.workspace_ref
                    or instance.provider_ref != binding.provider_ref
                    or not set(instance.allowed_tools or ()).issubset(set(binding.allowed_tools))):
                raise AdmissionDenied("policy_mismatch")
            if requires_real_data:
                if instance.isolation_mode in (None, "none"):
                    raise AdmissionDenied("isolation_none")
                if not instance.isolation_verified:
                    raise AdmissionDenied("isolation_unverified")
            return Admission(instance, binding, requires_real_data)
