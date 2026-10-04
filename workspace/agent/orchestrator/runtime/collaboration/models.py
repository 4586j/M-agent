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


class Unavailable(CollaborationError):
    """A required dependency (for example the planner) is not available."""


# Statuses that mean the approved text is the requirement's working version, so
# every project member may see it even after the drafting cycle has moved on.
POST_APPROVAL_STATUSES = (
    "approved", "planning", "planning_blocked", "executing", "integrating",
    "awaiting_acceptance", "rework", "paused", "cancelling", "cancelled", "completed",
)

# An attempt holds the work item while it is in one of these statuses; only one
# attempt per work item may be open at a time (partial unique index).
OPEN_ATTEMPT_STATUSES = ("assigned", "acked", "running", "reconciliation_required")
# A lease says a machine is still answering; an attempt already parked for a person is excluded,
# because re-claiming it every round would rewrite the same lost run over and over.
LEASED_ATTEMPT_STATUSES = ("assigned", "acked", "running")
ATTEMPT_TERMINAL_STATUSES = ("accepted", "changes_requested", "failed", "cancelled")
ATTEMPT_STATUSES = OPEN_ATTEMPT_STATUSES + ATTEMPT_TERMINAL_STATUSES + ("submitted",)
REPORT_STAGES = ("ack", "start", "lease", "artifact", "result", "cancel")


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
    worker_url: str | None = None
    control_epoch: int = 1


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


@dataclass(frozen=True)
class RequirementAcceptance:
    acceptance_id: str
    project_id: str
    requirement_id: str
    reviewer_user_id: str
    decision: str
    evidence_hash: str
    reason: str = ""
    created_at: float = 0.0
    returned_items: tuple[str, ...] = ()


@dataclass(frozen=True)
class RequirementPause:
    pause_id: str
    project_id: str
    requirement_id: str
    actor_user_id: str
    action: str
    reason: str = ""
    created_at: float = 0.0


@dataclass(frozen=True)
class RequirementIntegration:
    integration_id: str
    project_id: str
    requirement_id: str
    actor_user_id: str
    decision: str
    evidence_hash: str
    reason: str = ""
    created_at: float = 0.0
    returned_items: tuple[str, ...] = ()


@dataclass(frozen=True)
class RevisionAcceptanceItem:
    revision_id: str
    requirement_id: str
    item_id: str
    text: str
    position: int


@dataclass(frozen=True)
class Plan:
    plan_id: str
    project_id: str
    requirement_id: str
    revision_id: str
    revision_hash: str
    status: str
    report: str
    total_units: int
    created_by: str | None = None
    created_at: float = 0.0
    published_at: float | None = None
    version: int = 1


@dataclass(frozen=True)
class WorkItem:
    work_item_id: str
    plan_id: str
    project_id: str
    key: str
    title: str
    objective: str
    scope: str
    non_goals: str
    duty: str
    assignee_user_id: str
    binding_id: str
    status: str = "pending_dependency"
    estimated_units: int = 0
    version: int = 1
    created_at: float = 0.0
    updated_at: float = 0.0


@dataclass(frozen=True)
class WorkItemDependency:
    work_item_id: str
    depends_on_id: str
    plan_id: str
    project_id: str


@dataclass(frozen=True)
class OutboxEvent:
    event_id: str
    project_id: str
    topic: str
    payload: str
    created_at: float
    next_attempt_at: float
    attempts: int = 0
    claim_expires_at: float | None = None
    delivered_at: float | None = None
    last_error: str | None = None


@dataclass(frozen=True)
class BudgetAccount:
    scope: str
    ref_id: str
    limit_units: int
    version: int = 1
    updated_at: float = 0.0


@dataclass(frozen=True)
class BudgetEntry:
    entry_id: str
    scope: str
    ref_id: str
    ref_type: str
    ref_key: str
    kind: str
    delta_units: int
    created_at: float = 0.0


@dataclass(frozen=True)
class Attempt:
    attempt_id: str
    project_id: str
    work_item_id: str
    plan_id: str
    requirement_id: str
    revision_id: str
    revision_hash: str
    instance_id: str
    binding_id: str
    binding_version: int
    assignee_user_id: str
    number: int
    schema_version: str
    input_hash: str
    package_json: str
    status: str = "assigned"
    lease_epoch: int = 1
    control_epoch: int = 1
    lease_expires_at: float = 0.0
    usage_units: int = 0
    result_json: str | None = None
    result_hash: str | None = None
    trace_id: str | None = None
    version: int = 1
    created_at: float = 0.0
    updated_at: float = 0.0


@dataclass(frozen=True)
class AttemptReport:
    report_id: str
    attempt_id: str
    project_id: str
    stage: str
    lease_epoch: int
    control_epoch: int
    input_hash: str
    payload: str
    accepted: int
    reason: str | None = None
    created_at: float = 0.0


@dataclass(frozen=True)
class AttemptArtifact:
    artifact_id: str
    attempt_id: str
    project_id: str
    work_item_id: str
    name: str
    media_type: str
    size_bytes: int
    sha256: str
    created_at: float = 0.0


@dataclass(frozen=True)
class OwnerReview:
    review_id: str
    attempt_id: str
    project_id: str
    work_item_id: str
    reviewer_user_id: str
    decision: str
    result_hash: str
    reason: str = ""
    created_at: float = 0.0


@dataclass(frozen=True)
class AcceptanceReview:
    review_id: str
    attempt_id: str
    project_id: str
    work_item_id: str
    reviewer_user_id: str
    decision: str
    result_hash: str
    reason: str = ""
    created_at: float = 0.0


@dataclass(frozen=True)
class SecurityReview:
    security_review_id: str
    attempt_id: str
    project_id: str
    rule_key: str
    input_hash: str
    requirement_id: str
    revision_id: str
    binding_version: int
    tool_policy_version: str
    decision: str = "pending"
    reviewer_user_id: str | None = None
    reason: str = ""
    expires_at: float = 0.0
    version: int = 1
    created_at: float = 0.0
    updated_at: float = 0.0


@dataclass(frozen=True)
class SecurityGrant:
    project_id: str
    user_id: str
    capability: str
    status: str = "active"
    granted_by: str = ""
    version: int = 1
    created_at: float = 0.0
    updated_at: float = 0.0
