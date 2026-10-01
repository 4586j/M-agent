"""Immutable collaboration data classes and shared domain errors."""

from dataclasses import dataclass


class CollaborationError(Exception):
    """Base error for collaboration operations."""


class NotFound(CollaborationError):
    """The requested entity does not exist."""


class Conflict(CollaborationError):
    """An operation conflicts with existing data or its version."""


class InvalidState(CollaborationError):
    """An operation is not allowed in the entity's current state."""


class ValidationError(CollaborationError):
    """An input does not satisfy the collaboration contract."""


class SchemaVersionError(CollaborationError):
    """The database schema is not supported by this runtime."""


@dataclass(frozen=True)
class User:
    user_id: str
    display_name: str
    status: str = "active"
    version: int = 1


@dataclass(frozen=True)
class Project:
    project_id: str
    name: str
    owner_id: str
    status: str = "active"
    version: int = 1


@dataclass(frozen=True)
class ProjectMember:
    project_id: str
    user_id: str
    access_role: str = "member"
    status: str = "active"
    version: int = 1


@dataclass(frozen=True)
class Agent:
    agent_id: str
    owner_user_id: str
    name: str
    status: str = "active"
    version: int = 1


@dataclass(frozen=True)
class AgentProjectBinding:
    binding_id: str
    project_id: str
    agent_id: str
    member_user_id: str
    duty: str
    persona_version: str
    allowed_tools: tuple[str, ...]
    workspace_ref: str
    provider_ref: str
    status: str = "active"
    version: int = 1


@dataclass(frozen=True)
class ExecutionNode:
    node_id: str
    name: str
    status: str = "active"
    version: int = 1


@dataclass(frozen=True)
class ExecutionInstance:
    instance_id: str
    binding_id: str
    node_id: str
    status: str = "pending"
    version: int = 1
    claim_expires_at: float = 0.0
    protocol_version: str | None = None
    capacity: int | None = None
    isolation_mode: str | None = None
    isolation_attestation: str | None = None
    isolation_verified: int = 0
    verified_at: float | None = None
    workspace_ref: str | None = None
    provider_ref: str | None = None
    allowed_tools: tuple[str, ...] | None = None
    env_allowlist: tuple[str, ...] | None = None
    created_by: str = ""
    created_at: float = 0.0
    claimed_at: float | None = None
    heartbeat_at: float | None = None


@dataclass(frozen=True)
class Requirement:
    requirement_id: str
    project_id: str
    title: str
    requester_id: str
    status: str = "draft"
    reviewer_id: str | None = None
    current_revision_id: str | None = None
    review_revision_id: str | None = None
    review_revision_hash: str | None = None
    approved_revision_id: str | None = None
    approved_revision_hash: str | None = None
    version: int = 1
    created_at: float = 0.0
    updated_at: float = 0.0


@dataclass(frozen=True)
class RequirementRevision:
    revision_id: str
    project_id: str
    requirement_id: str
    number: int
    content_hash: str
    content_size: int
    author_id: str
    created_at: float = 0.0


@dataclass(frozen=True)
class RequirementMessage:
    message_id: str
    project_id: str
    requirement_id: str
    role: str
    content: str
    author_id: str | None = None
    created_at: float = 0.0


@dataclass(frozen=True)
class RequirementReview:
    review_id: str
    project_id: str
    requirement_id: str
    revision_id: str
    revision_hash: str
    target_version: int
    reviewer_id: str
    decision: str
    reason: str = ""
    created_at: float = 0.0
