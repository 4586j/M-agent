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
