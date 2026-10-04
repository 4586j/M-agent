"""Independent, cookie-authenticated collaboration HTTP entry point."""
from __future__ import annotations

from contextlib import asynccontextmanager, contextmanager
from dataclasses import asdict, dataclass
from ipaddress import ip_address
import json
from pathlib import Path
import re
import sqlite3
from typing import Annotated, Literal
from urllib.parse import urlsplit

from fastapi import APIRouter, FastAPI, Path as PathParameter, Query, Request, Response
from fastapi.exceptions import RequestValidationError
import httpx
from pydantic import BaseModel, ConfigDict, Field, SecretStr
from starlette.exceptions import HTTPException
from starlette.responses import JSONResponse, RedirectResponse
from starlette.staticfiles import StaticFiles

from .auth import AuthService, Unauthorized, Forbidden, RateLimited
from .budgets import BudgetService
from .database import Database
from .dispatch import DispatchService
from .execution import (ExecutionService, MAX_ARTIFACT_BYTES, PACKAGE_MIME, REQUIREMENT_MIME,
                        decode_artifact)
from .inbox import InboxService
from .instances import InstanceService
from .models import Conflict, InvalidState, NotFound, Unavailable, ValidationError
from .planning import PlanService, plan_document
from .requirements import RequirementService
from .reviews import ReviewService
from .scheduler import QueueService, Scheduler
from .store import CollaborationStore


PREFIX = "/collab/v1"
COOKIE = "collab_session"
MANAGERS = ("owner", "manager")


def _origin(value: str) -> tuple[str, str, bool]:
    """Validate a single origin without resolving names or trusting proxy headers."""
    invalid = ValueError("public_origin must be HTTPS or loopback HTTP, with no URL extras")
    if not isinstance(value, str) or re.search(r"[\x00-\x20\x7f\s\\?#]", value):
        raise invalid
    try:
        parsed = urlsplit(value)
        host, port = parsed.hostname, parsed.port
    except ValueError:
        raise invalid from None
    if (parsed.scheme not in ("http", "https") or not host
            or parsed.username is not None or parsed.password is not None
            or parsed.path not in ("", "/") or parsed.query or parsed.fragment
            or parsed.netloc.endswith(":") or (port is not None and not 1 <= port <= 65535)):
        raise invalid
    try:
        loopback = ip_address(host).is_loopback
    except ValueError:
        if not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?", host):
            raise invalid from None
        loopback = host == "localhost"
    if parsed.scheme == "http" and not loopback:
        raise invalid
    authority = parsed.netloc.lower()
    return f"{parsed.scheme}://{authority}", authority, parsed.scheme == "https"


@dataclass(frozen=True)
class AppConfig:
    data_dir: Path
    public_origin: str = "http://127.0.0.1:7400"
    session_ttl: int = 28800
    lease_ttl: int = 120
    auto_dispatch: bool = False

    def __post_init__(self):
        canonical, _, _ = _origin(self.public_origin)
        if type(self.session_ttl) is not int or not 60 <= self.session_ttl <= 86400:
            raise ValueError("session_ttl must be between 60 and 86400 seconds")
        if type(self.lease_ttl) is not int or not 30 <= self.lease_ttl <= 86400:
            raise ValueError("lease_ttl must be between 30 and 86400 seconds")
        if type(self.auto_dispatch) is not bool:
            raise ValueError("auto_dispatch must be a boolean")
        object.__setattr__(self, "data_dir", Path(self.data_dir))
        object.__setattr__(self, "public_origin", canonical)


Name = Annotated[str, Field(min_length=1, max_length=512)]
Username = Annotated[str, Field(min_length=3, max_length=64)]
Password = Annotated[SecretStr, Field(min_length=12, max_length=1024)]
Version = Annotated[int, Field(strict=True, ge=1)]
AuditLimit = Annotated[int, Query(ge=1, le=200)]
EventLimit = Annotated[int, Query(ge=1, le=200)]
AcceptanceText = Annotated[str, Field(min_length=1, max_length=4096)]
AcceptanceItems = Annotated[list[AcceptanceText], Field(max_length=256)]
Digest = Annotated[str, Field(pattern="^[0-9a-f]{64}$")]
Epoch = Annotated[int, Field(strict=True, ge=1, le=10 ** 12)]
ArtifactName = Annotated[str, Field(min_length=1, max_length=512)]
MediaType = Annotated[str, Field(min_length=1, max_length=128)]
ArtifactContent = Annotated[str, Field(
    min_length=1, max_length=4 * ((MAX_ARTIFACT_BYTES + 2) // 3) + 8)]


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Login(Input):
    username: Username
    password: Password


class Register(Input):
    invitation_token: Annotated[SecretStr, Field(min_length=1, max_length=512)]
    password: Password


class Invitation(Input):
    username: Username
    display_name: Name


class ExpectedVersion(Input):
    expected_version: Version


class UserStatus(ExpectedVersion):
    status: Literal["active", "disabled"]


class Named(Input):
    name: Name


class ProjectUpdate(Named, ExpectedVersion):
    status: Literal["active", "archived"]


class MemberCreate(Input):
    user_id: Name
    access_role: Literal["manager", "reviewer", "member"]


class MemberUpdate(ExpectedVersion):
    access_role: Literal["owner", "manager", "reviewer", "member"]
    status: Literal["active", "removed"]


class AgentUpdate(Named, ExpectedVersion):
    status: Literal["active", "disabled"]


class BindingFields(Input):
    duty: Annotated[str, Field(min_length=1, max_length=8192)]
    persona_version: Name
    allowed_tools: Annotated[list[str], Field(max_length=256)]
    workspace_ref: str
    provider_ref: str


class BindingCreate(BindingFields):
    agent_id: Name
    member_user_id: Name


class BindingUpdate(BindingFields, ExpectedVersion):
    pass


class InstanceCreate(Input):
    node_id: Name
    worker_url: Annotated[str, Field(min_length=1, max_length=512)] | None = None


class MachinePolicy(Input):
    workspace_ref: Annotated[str, Field(min_length=1, max_length=128)]
    provider_ref: Annotated[str, Field(min_length=1, max_length=128)]
    allowed_tools: Annotated[list[str], Field(max_length=256)]
    env_allowlist: Annotated[list[str], Field(max_length=64)]


class MachineClaim(Input):
    claim_token: Annotated[SecretStr, Field(min_length=1, max_length=512)]
    protocol_version: Name
    capacity: Annotated[int, Field(strict=True, ge=1, le=64)]
    isolation_mode: Literal["none", "os_account", "container", "vm"]
    isolation_attestation: Annotated[str, Field(max_length=1024)] = ""
    policy: MachinePolicy


class MachineCallback(Input):
    input_hash: Digest
    control_epoch: Epoch
    lease_epoch: Epoch


class MachineStart(MachineCallback):
    trace_id: Annotated[str, Field(min_length=1, max_length=128)] | None = None


class ArtifactEntry(Input):
    name: ArtifactName
    sha256: Digest


class MachineArtifact(MachineCallback):
    name: ArtifactName
    media_type: MediaType
    sha256: Digest
    content: ArtifactContent


class MachineResult(MachineCallback):
    status: Literal["submitted", "failed"]
    summary: Annotated[str, Field(min_length=1, max_length=8192)]
    artifacts: Annotated[list[ArtifactEntry], Field(max_length=256)] = []
    usage_units: Annotated[int, Field(strict=True, ge=0, le=10 ** 12)] = 0
    trace_id: Annotated[str, Field(min_length=1, max_length=128)] | None = None


class MachineCancel(MachineCallback):
    reason: Annotated[str, Field(max_length=2048)] = ""


class RequirementCreate(Input):
    title: Name
    content: Annotated[str, Field(max_length=8388608)] | None = None
    acceptance_items: AcceptanceItems | None = None


class RequirementMessageCreate(Input):
    content: Annotated[str, Field(min_length=1, max_length=8192)]


class RequirementRevisionCreate(Input):
    content: Annotated[str, Field(min_length=1, max_length=8388608)]
    acceptance_items: AcceptanceItems | None = None
    expected_version: Version


class RequirementReviewCreate(Input):
    action: Literal["request", "approve", "reject"]
    revision_id: Name
    reviewer_user_id: Name | None = None
    reason: Annotated[str, Field(max_length=2048)] = ""
    expected_version: Version


class BudgetLimit(Input):
    limit_units: Annotated[int, Field(strict=True, ge=0, le=10 ** 12)]
    expected_version: Version | None = None


class OwnerDecision(Input):
    decision: Literal["confirmed", "changes_requested"]
    expected_version: Version
    reason: Annotated[str, Field(max_length=2048)] = ""


class AcceptanceDecision(OwnerDecision):
    decision: Literal["accepted", "changes_requested"]


class RequirementAction(Input):
    action: Literal["cancel", "advance", "pause", "resume"]
    expected_version: Version
    reason: Annotated[str, Field(max_length=2048)] = ""


class RequirementAcceptanceCreate(Input):
    decision: Literal["accepted", "changes_requested"]
    expected_version: Version
    reason: Annotated[str, Field(max_length=2048)] = ""


class IntegrationDecision(Input):
    decision: Literal["passed", "returned"]
    expected_version: Version
    reason: Annotated[str, Field(max_length=2048)] = ""


class AttemptReconcile(Input):
    outcome: Literal["cancelled", "failed"]
    reason: Annotated[str, Field(min_length=1, max_length=2048)]
    expected_version: Version


class WorkItemCancel(ExpectedVersion):
    reason: Annotated[str, Field(max_length=2048)] = ""


class InboxAck(Input):
    item_key: Annotated[str, Field(min_length=1, max_length=200)]


class SecurityRuleCreate(Input):
    rule_key: Annotated[str, Field(min_length=1, max_length=128)]


class SecurityDecision(Input):
    decision: Literal["approved", "rejected"]
    reason: Annotated[str, Field(min_length=1, max_length=2048)]
    expected_version: Version


class SecurityGrantUpdate(Input):
    active: bool
    expected_version: Version | None = None


_ERRORS = {
    401: "Unauthorized", 403: "Forbidden", 404: "Not found", 405: "Method not allowed",
    409: "Conflict", 422: "Invalid request", 429: "Too many requests",
    500: "Internal server error", 503: "Service unavailable",
}


def _error(status: int) -> JSONResponse:
    return JSONResponse({"detail": _ERRORS.get(status, "Request failed")}, status_code=status,
                        headers={"Cache-Control": "no-store"})


def _access(request: Request, **kwargs):
    return request.app.state.auth.access(
        request.cookies.get(COOKIE), request.headers.get("X-CSRF-Token"), **kwargs)


def _authorized_actor(request: Request, **kwargs) -> str:
    """Authorize on a short-lived transaction, then release it before service work.

    Collaboration services open their own connections; holding this transaction
    across them would keep the single-writer lock of the SQLite file busy.
    """
    with _access(request, write=True, **kwargs) as access:
        return access.user.user_id


def _audit(request: Request, access, action: str, target_id: str, version: int,
           project_id: str | None = None):
    request.app.state.auth.audit(access.conn, access.user.user_id, action, target_id,
                                 project_id=project_id, target_version=version)


@contextmanager
def _cascading_revocations(request: Request, access, *, project_id=None,
                          member_user_id=None, agent_id=None):
    # Capture only IDs; implicit revocations need the same audit guarantees as explicit ones.
    filters = [(name, value) for name, value in (("project_id", project_id),
               ("member_user_id", member_user_id), ("agent_id", agent_id)) if value is not None]
    predicate = " AND ".join(f"{name}=?" for name, _ in filters)
    rows = access.conn.execute(
        "SELECT project_id,binding_id FROM agent_bindings WHERE status='active' AND " + predicate,
        tuple(value for _, value in filters)).fetchall()
    yield
    for row in rows:
        binding = access.store.get_binding(row["project_id"], row["binding_id"])
        if binding.status == "revoked":
            _audit(request, access, "binding.revoked", binding.binding_id,
                   binding.version, binding.project_id)
            request.app.state.instances.disable_for_binding(
                access, project_id=binding.project_id, binding_id=binding.binding_id)


_ATTEMPT_VIEW = (
    "attempt_id", "work_item_id", "plan_id", "requirement_id", "revision_id", "number",
    "schema_version", "input_hash", "status", "lease_epoch", "control_epoch",
    "lease_expires_at", "usage_units", "result_hash", "trace_id", "version", "updated_at",
)


def _attempt_view(attempt) -> dict:
    # The frozen package is only ever served from its own endpoint, never inside a status body.
    return {name: getattr(attempt, name) for name in _ATTEMPT_VIEW}


def _artifact_view(artifact) -> dict:
    return {"artifact_id": artifact.artifact_id, "name": artifact.name,
            "media_type": artifact.media_type, "sha256": artifact.sha256,
            "size_bytes": artifact.size_bytes, "href": f"artifacts/{artifact.artifact_id}"}


def _claims(body: MachineCallback) -> dict:
    return {"input_hash": body.input_hash, "control_epoch": body.control_epoch,
            "lease_epoch": body.lease_epoch}


def _call_view(call) -> dict:
    result = {"outcome": call.outcome, "attempt": _attempt_view(call.attempt)}
    if call.artifact is not None:
        result["artifact"] = _artifact_view(call.artifact)
    return result


def _audit_rows(conn, project_id: str | None, limit: int):
    columns = "event_id,project_id,actor_id,action,target_id,target_version,created_at"
    if project_id is None:
        rows = conn.execute(
            f"SELECT {columns} FROM collaboration_audit WHERE project_id IS NULL "
            "ORDER BY created_at DESC,event_id DESC LIMIT ?", (limit,))
    else:
        rows = conn.execute(
            f"SELECT {columns} FROM collaboration_audit WHERE project_id=? "
            "ORDER BY created_at DESC,event_id DESC LIMIT ?", (project_id, limit))
    return [dict(row) for row in rows]


# A lease renewal repeats on a timer and a rejected callback changed nothing: neither is news a
# person wants in a story about the requirement, and the first one would crowd the rest out.
_ACTIVITY_DENIED = ("attempt.lease", "attempt.callback_rejected")


def _activity_rows(conn, project_id: str, requirement_id: str, limit: int):
    """One requirement's story, newest first, read off the audit trail it already wrote.

    The audit table is the only event source here: an action shows up as soon as its target
    belongs to this requirement, so nothing has to be written a second time to become visible.
    Events written in one transaction share a timestamp, so the row id breaks the tie: this table
    is append-only by trigger, which makes it the order the writes actually happened in.
    """
    denied = ",".join("?" * len(_ACTIVITY_DENIED))
    rows = conn.execute(
        f"""
        WITH subject(id, kind, label) AS (
            SELECT ?, 'requirement', '需求本身'
            UNION ALL SELECT revision_id, 'revision', '第 ' || number || ' 版原文'
                FROM requirement_revisions WHERE requirement_id=?
            UNION ALL SELECT plan_id, 'plan', '拆解计划' FROM plans WHERE requirement_id=?
            UNION ALL SELECT w.work_item_id, 'work_item', '工作项 ' || w.key
                FROM work_items w JOIN plans p ON p.plan_id=w.plan_id WHERE p.requirement_id=?
            UNION ALL SELECT a.attempt_id, 'attempt', '第 ' || a.number || ' 次尝试 · ' || w.key
                FROM attempts a JOIN work_items w ON w.work_item_id=a.work_item_id
                WHERE a.requirement_id=?)
        SELECT a.event_id,a.actor_id,a.action,a.created_at,s.kind,s.label
        FROM collaboration_audit a JOIN subject s ON s.id=a.target_id
        WHERE a.project_id=? AND a.action NOT IN ({denied})
        ORDER BY a.created_at DESC,a.rowid DESC LIMIT ?
        """,
        (requirement_id, requirement_id, requirement_id, requirement_id, requirement_id,
         project_id, *_ACTIVITY_DENIED, limit))
    return [dict(row) for row in rows]


def _feed_rows(conn, user_id: str, limit: int):
    """News addressed to one person, across the projects they are active in, newest first.

    Same audit trail and same subject wording as ``_activity_rows`` (the tests keep the two in
    step); what differs is the audience, so this is the half that can arrive unasked: events on a
    requirement the person owns, was named to review, or is assigned to work on. An event they
    caused is not news to them, so their own writes are left out, and the audience is read from
    the durable review rows rather than from ``requirements.reviewer_id`` alone: a decision that
    gets made must not erase the news that somebody was asked to make it.
    """
    denied = ",".join("?" * len(_ACTIVITY_DENIED))
    rows = conn.execute(
        f"""
        WITH mine AS (SELECT project_id FROM project_members
                      WHERE user_id=? AND status='active'),
             subject(id, requirement_id, label) AS (
                SELECT requirement_id, requirement_id, '需求本身' FROM requirements
                    WHERE project_id IN mine
                UNION ALL SELECT revision_id, requirement_id, '第 ' || number || ' 版原文'
                    FROM requirement_revisions WHERE project_id IN mine
                UNION ALL SELECT plan_id, requirement_id, '拆解计划' FROM plans WHERE project_id IN mine
                UNION ALL SELECT w.work_item_id, p.requirement_id, '工作项 ' || w.key
                    FROM work_items w JOIN plans p ON p.plan_id=w.plan_id WHERE w.project_id IN mine
                UNION ALL SELECT a.attempt_id, a.requirement_id, '第 ' || a.number || ' 次尝试 · ' || w.key
                    FROM attempts a JOIN work_items w ON w.work_item_id=a.work_item_id
                    WHERE a.project_id IN mine)
        SELECT e.event_id,e.project_id,e.actor_id,actor.display_name AS actor_name,
               e.action,e.created_at,e.target_id,
               s.requirement_id,r.title AS requirement_title,s.label
        FROM collaboration_audit e
        JOIN subject s ON s.id=e.target_id
        JOIN requirements r ON r.requirement_id=s.requirement_id
        LEFT JOIN users actor ON actor.user_id=e.actor_id
        LEFT JOIN work_items owed_item ON owed_item.work_item_id=s.id
        LEFT JOIN attempts owed_attempt ON owed_attempt.attempt_id=s.id
        WHERE e.project_id IN (SELECT project_id FROM mine) AND e.action NOT IN ({denied})
          AND (e.actor_id IS NULL OR e.actor_id<>?)
          AND (r.requester_id=? OR r.reviewer_id=?
               OR owed_item.assignee_user_id=? OR owed_attempt.assignee_user_id=?
               OR EXISTS (SELECT 1 FROM requirement_reviews named
                          WHERE named.requirement_id=s.requirement_id AND named.reviewer_id=?))
        ORDER BY e.created_at DESC,e.rowid DESC LIMIT ?
        """,
        (user_id, *_ACTIVITY_DENIED, user_id, user_id, user_id, user_id, user_id, user_id, limit))
    return [dict(row) for row in rows]


def _wake(client: httpx.Client, url: str, payload: dict) -> None:
    """Advisory wake-up of a registered worker; failure only delays its next pull."""
    response = client.post(url, json=payload, headers={"x-collab-wake": "1"})
    if response.status_code >= 400:
        raise OSError(f"Wake delivery failed: {response.status_code}")


def create_app(config: AppConfig) -> FastAPI:
    """Construct an application; only its lifespan initializes persistent state."""
    origin, authority, secure = _origin(config.public_origin)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        database = Database(config.data_dir / "metadata.sqlite3")
        database.initialize()
        app.state.auth = AuthService(database, session_ttl=config.session_ttl)
        app.state.instances = InstanceService(database)
        app.state.requirements = RequirementService()
        app.state.budgets = BudgetService()
        app.state.planning = PlanService(database, budgets=app.state.budgets)
        app.state.queue = QueueService(database,
                                       heartbeat_timeout=app.state.instances.heartbeat_timeout)
        # Wake-ups go to the exact worker URL that was registered with this service, direct, the
        # same way the worker's own client reaches us: an ambient proxy from the machine's
        # environment would send those POSTs somewhere else and silently cost us the push.
        waker = httpx.Client(timeout=httpx.Timeout(10.0, connect=3.0), trust_env=False)
        app.state.dispatch = DispatchService(
            database, instances=app.state.instances, lease_ttl=config.lease_ttl,
            push=lambda url, payload: _wake(waker, url, payload))
        app.state.execution = ExecutionService(database, instances=app.state.instances,
                                               lease_ttl=config.lease_ttl,
                                               budgets=app.state.budgets)
        app.state.reviews = ReviewService(budgets=app.state.budgets)
        app.state.inbox = InboxService()
        app.state.scheduler = Scheduler(database, planning=app.state.planning,
                                        queue=app.state.queue, dispatch=app.state.dispatch,
                                        auto_dispatch=config.auto_dispatch)
        try:
            yield
        finally:
            waker.close()

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None,
                  redirect_slashes=False)

    @app.middleware("http")
    async def request_boundary(request: Request, call_next):
        hosts = request.headers.getlist("host")
        origins = request.headers.getlist("origin")
        write = request.method not in ("GET", "HEAD", "OPTIONS")
        if (len(hosts) != 1 or hosts[0].lower() != authority
                or len(origins) > 1 or (origins and origins[0] != origin)
                or (write and not origins)
                or len(request.headers.getlist("x-csrf-token")) > 1
                or len(request.headers.getlist("x-node-token")) > 1
                or len(request.headers.getlist("x-instance-token")) > 1):
            return _error(403)
        try:
            response = await call_next(request)
        except Exception:
            # Do not pass exception text, SQL, request bodies or credentials to logs.
            return _error(500)
        response.headers["Cache-Control"] = "no-store"
        return response

    async def domain_error(request: Request, exc: Exception):
        for kind, status in ((Unauthorized, 401), (Forbidden, 403), (RateLimited, 429),
                             (Unavailable, 503), (NotFound, 404), (Conflict, 409),
                             (InvalidState, 409), (ValidationError, 422),
                             (RequestValidationError, 422), (sqlite3.IntegrityError, 409)):
            if isinstance(exc, kind):
                return _error(status)
        return _error(500)

    for kind in (Unauthorized, Forbidden, RateLimited, Unavailable, NotFound, Conflict,
                 InvalidState, ValidationError, RequestValidationError,
                 sqlite3.DatabaseError):
        app.add_exception_handler(kind, domain_error)

    @app.exception_handler(HTTPException)
    async def http_error(request: Request, exc: HTTPException):
        return _error(exc.status_code)

    router = APIRouter(prefix=PREFIX)

    @router.post("/auth/login")
    def login(body: Login, request: Request, response: Response):
        session = app.state.auth.login(
            body.username, body.password.get_secret_value(),
            peer=request.client.host if request.client else "unknown",
            previous_token=request.cookies.get(COOKIE))
        response.set_cookie(COOKIE, session.session_token, max_age=config.session_ttl,
                            path=PREFIX, secure=secure, httponly=True, samesite="strict")
        return {"user": asdict(session.user), "is_admin": session.is_admin,
                "csrf_token": session.csrf_token, "expires_at": session.expires_at}

    @router.post("/auth/register", status_code=201)
    def register(body: Register):
        return asdict(app.state.auth.register(body.invitation_token.get_secret_value(),
                                             body.password.get_secret_value()))

    @router.post("/auth/logout", status_code=204)
    def logout(request: Request):
        app.state.auth.logout(request.cookies.get(COOKIE), request.headers.get("X-CSRF-Token"))
        response = Response(status_code=204)
        response.delete_cookie(COOKIE, path=PREFIX, secure=secure, httponly=True, samesite="strict")
        return response

    @router.get("/me")
    def me(request: Request):
        with _access(request) as access:
            return {"user": asdict(access.user), "is_admin": access.is_admin,
                    "csrf_token": access.csrf_token, "expires_at": access.expires_at}

    @router.post("/auth/invitations", status_code=201)
    def create_invitation(body: Invitation, request: Request):
        result = app.state.auth.create_invitation(
            request.cookies.get(COOKIE), request.headers.get("X-CSRF-Token"),
            body.username, body.display_name)
        return asdict(result)

    @router.post("/auth/invitations/{iid}/revoke")
    def revoke_invitation(iid: str, body: ExpectedVersion, request: Request):
        return app.state.auth.revoke_invitation(
            request.cookies.get(COOKIE), request.headers.get("X-CSRF-Token"),
            iid, body.expected_version)

    @router.get("/admin/audit")
    def admin_audit(request: Request, limit: AuditLimit = 100):
        with _access(request, admin=True) as access:
            return _audit_rows(access.conn, None, limit)

    @router.patch("/admin/users/{uid}")
    def update_user(uid: str, body: UserStatus, request: Request):
        with _access(request, admin=True, write=True) as access:
            if uid == access.user.user_id and body.status == "disabled":
                raise Forbidden("Cannot disable self")
            previous = access.store.get_user(uid)
            with _cascading_revocations(request, access, member_user_id=uid):
                user = access.store.update_user(uid, display_name=previous.display_name,
                                                **body.model_dump())
                _audit(request, access, "user.status_changed", uid, user.version)
            return asdict(user)

    @router.get("/projects")
    def list_projects(request: Request):
        with _access(request) as access:
            return [asdict(project) for project in access.store.list_projects(access.user.user_id)]

    @router.post("/projects", status_code=201)
    def create_project(body: Named, request: Request):
        with _access(request, write=True) as access:
            project = access.store.create_project(body.name, access.user.user_id)
            _audit(request, access, "project.created", project.project_id, project.version,
                   project.project_id)
            return asdict(project)

    @router.get("/projects/{pid}")
    def get_project(pid: str, request: Request):
        with _access(request, project_id=pid) as access:
            # The caller's own role, so a client can offer exactly the actions this project
            # allows them instead of keeping a second copy of the permission rules.
            member = access.store.get_member(pid, access.user.user_id)
            return {**asdict(access.store.get_project(pid)),
                    "access_role": member.access_role}

    @router.patch("/projects/{pid}")
    def update_project(pid: str, body: ProjectUpdate, request: Request):
        with _access(request, project_id=pid, roles=("owner",), write=True) as access:
            with _cascading_revocations(request, access, project_id=pid):
                project = access.store.update_project(pid, **body.model_dump())
                _audit(request, access, "project.updated", pid, project.version, pid)
            return asdict(project)

    @router.get("/projects/{pid}/members")
    def list_members(pid: str, request: Request):
        with _access(request, project_id=pid) as access:
            members = access.store.list_members(pid)
            names = access.store.display_names([member.user_id for member in members])
            return [{**asdict(member), "display_name": names[member.user_id]}
                    for member in members]

    @router.post("/projects/{pid}/members", status_code=201)
    def add_member(pid: str, body: MemberCreate, request: Request):
        with _access(request, project_id=pid, roles=MANAGERS, write=True) as access:
            actor = access.store.get_member(pid, access.user.user_id)
            if actor.access_role == "manager" and body.access_role not in ("member", "reviewer"):
                raise Forbidden("Cannot grant this role")
            member = access.store.add_member(pid, **body.model_dump())
            _audit(request, access, "member.added", member.user_id, member.version, pid)
            return asdict(member)

    @router.patch("/projects/{pid}/members/{uid}")
    def update_member(pid: str, uid: str, body: MemberUpdate, request: Request):
        with _access(request, project_id=pid, roles=MANAGERS, write=True) as access:
            actor = access.store.get_member(pid, access.user.user_id)
            target = access.store.get_member(pid, uid)
            if actor.access_role == "manager" and (
                    target.access_role in MANAGERS or body.access_role in MANAGERS):
                raise Forbidden("Cannot modify or grant this role")
            with _cascading_revocations(request, access, project_id=pid, member_user_id=uid):
                member = access.store.update_member(pid, uid, **body.model_dump())
                _audit(request, access, "member.updated", uid, member.version, pid)
            return asdict(member)

    @router.get("/agents")
    def list_agents(request: Request):
        with _access(request) as access:
            return [asdict(agent) for agent in access.store.list_agents(access.user.user_id)]

    @router.post("/agents", status_code=201)
    def create_agent(body: Named, request: Request):
        with _access(request, write=True) as access:
            agent = access.store.create_agent(access.user.user_id, body.name)
            _audit(request, access, "agent.created", agent.agent_id, agent.version)
            return asdict(agent)

    @router.patch("/agents/{aid}")
    def update_agent(aid: str, body: AgentUpdate, request: Request):
        with _access(request, write=True) as access:
            with _cascading_revocations(request, access, agent_id=aid):
                agent = access.store.update_agent(aid, access.user.user_id, **body.model_dump())
                _audit(request, access, "agent.updated", aid, agent.version)
            return asdict(agent)

    @router.get("/projects/{pid}/agent-bindings")
    def list_bindings(pid: str, request: Request):
        with _access(request, project_id=pid) as access:
            return [asdict(binding) for binding in access.store.list_bindings(pid)]

    @router.post("/projects/{pid}/agent-bindings", status_code=201)
    def create_binding(pid: str, body: BindingCreate, request: Request):
        with _access(request, project_id=pid, roles=MANAGERS, write=True) as access:
            binding = access.store.create_binding(pid, **body.model_dump())
            _audit(request, access, "binding.created", binding.binding_id, binding.version, pid)
            return asdict(binding)

    @router.get("/projects/{pid}/agent-bindings/{bid}")
    def get_binding(pid: str, bid: str, request: Request):
        with _access(request, project_id=pid) as access:
            return asdict(access.store.get_binding(pid, bid))

    @router.patch("/projects/{pid}/agent-bindings/{bid}")
    def update_binding(pid: str, bid: str, body: BindingUpdate, request: Request):
        with _access(request, project_id=pid, roles=MANAGERS, write=True) as access:
            binding = access.store.update_binding(pid, bid, **body.model_dump())
            _audit(request, access, "binding.updated", bid, binding.version, pid)
            return asdict(binding)

    @router.post("/projects/{pid}/agent-bindings/{bid}/revoke")
    def revoke_binding(pid: str, bid: str, body: ExpectedVersion, request: Request):
        with _access(request, project_id=pid, roles=MANAGERS, write=True) as access:
            binding = access.store.revoke_binding(pid, bid, expected_version=body.expected_version)
            _audit(request, access, "binding.revoked", bid, binding.version, pid)
            app.state.instances.disable_for_binding(access, project_id=pid, binding_id=bid)
            return asdict(binding)

    @router.get("/projects/{pid}/agent-bindings/{bid}/revisions/{version}")
    def get_binding_revision(pid: str, bid: str, version: Annotated[int, PathParameter(ge=1)],
                             request: Request):
        with _access(request, project_id=pid) as access:
            return asdict(access.store.get_binding_revision(pid, bid, version))

    @router.get("/projects/{pid}/agent-bindings/{bid}/instances")
    def list_instances(pid: str, bid: str, request: Request):
        with _access(request, project_id=pid) as access:
            return [asdict(instance) for instance in access.store.list_instances(pid, bid)]

    @router.post("/projects/{pid}/agent-bindings/{bid}/instances", status_code=201)
    def register_instance(pid: str, bid: str, body: InstanceCreate, request: Request):
        with _access(request, project_id=pid, roles=MANAGERS, write=True) as access:
            instance, claim_token = app.state.instances.register_slot(
                access, project_id=pid, binding_id=bid, node_id=body.node_id,
                worker_url=body.worker_url)
            return {**asdict(instance), "claim_token": claim_token}

    @router.post("/projects/{pid}/agent-bindings/{bid}/instances/{iid}/disable")
    def disable_instance(pid: str, bid: str, iid: str, body: ExpectedVersion, request: Request):
        with _access(request, project_id=pid, roles=MANAGERS, write=True) as access:
            return asdict(app.state.instances.disable(
                access, project_id=pid, binding_id=bid, instance_id=iid,
                expected_version=body.expected_version))

    @router.post("/projects/{pid}/agent-bindings/{bid}/instances/{iid}/verify")
    def verify_instance(pid: str, bid: str, iid: str, body: ExpectedVersion, request: Request):
        with _access(request, project_id=pid, roles=("owner",), write=True) as access:
            return asdict(app.state.instances.verify(
                access, project_id=pid, binding_id=bid, instance_id=iid,
                expected_version=body.expected_version))

    @router.post("/machine/instances/claim", status_code=201)
    def machine_claim(body: MachineClaim, request: Request):
        instance, instance_token, binding = app.state.instances.claim(
            request.headers.get("X-Node-Token"), body.claim_token.get_secret_value(),
            protocol_version=body.protocol_version, capacity=body.capacity,
            isolation_mode=body.isolation_mode,
            isolation_attestation=body.isolation_attestation,
            workspace_ref=body.policy.workspace_ref, provider_ref=body.policy.provider_ref,
            allowed_tools=body.policy.allowed_tools, env_allowlist=body.policy.env_allowlist)
        return {**asdict(instance), "instance_token": instance_token,
                "project_id": binding.project_id, "binding_version": binding.version,
                "heartbeat_timeout": app.state.instances.heartbeat_timeout}

    @router.post("/machine/instances/heartbeat")
    def machine_heartbeat(request: Request):
        instance = app.state.instances.heartbeat(request.headers.get("X-Instance-Token"))
        return {"instance_id": instance.instance_id, "status": instance.status,
                "heartbeat_at": instance.heartbeat_at}

    @router.get("/machine/instances/me")
    def machine_me(request: Request):
        instance, binding = app.state.instances.describe(request.headers.get("X-Instance-Token"))
        return {**asdict(instance), "project_id": binding.project_id,
                "binding_version": binding.version}

    @router.get("/machine/instances/me/attempts")
    def machine_attempts(request: Request):
        attempts = app.state.execution.held_attempts(request.headers.get("X-Instance-Token"))
        return [_attempt_view(attempt) for attempt in attempts]

    @router.get("/machine/attempts/{attempt_id}/package")
    def machine_package(attempt_id: str, request: Request):
        payload, attempt = app.state.execution.package(
            request.headers.get("X-Instance-Token"), attempt_id)
        return Response(content=payload, media_type=PACKAGE_MIME,
                        headers={"X-Input-Hash": attempt.input_hash})

    @router.get("/machine/attempts/{attempt_id}/requirement")
    def machine_requirement(attempt_id: str, request: Request):
        payload, attempt = app.state.execution.requirement_text(
            request.headers.get("X-Instance-Token"), attempt_id)
        return Response(content=payload, media_type=REQUIREMENT_MIME, headers={
            "X-Content-Hash": attempt.revision_hash, "X-Revision-Id": attempt.revision_id})

    @router.get("/machine/attempts/{attempt_id}/artifacts")
    def machine_artifacts(attempt_id: str, request: Request):
        listed = app.state.execution.artifacts(request.headers.get("X-Instance-Token"),
                                               attempt_id)
        return [_artifact_view(artifact) for artifact in listed]

    @router.get("/machine/attempts/{attempt_id}/artifacts/{artifact_id}")
    def machine_artifact_download(attempt_id: str, artifact_id: str, request: Request):
        content, artifact = app.state.execution.artifact_content(
            request.headers.get("X-Instance-Token"), attempt_id, artifact_id)
        return Response(content=content, media_type=artifact.media_type,
                        headers={"X-Content-Hash": artifact.sha256})

    @router.post("/machine/attempts/{attempt_id}/ack")
    def machine_ack(attempt_id: str, body: MachineCallback, request: Request):
        call = app.state.execution.ack(request.headers.get("X-Instance-Token"), attempt_id,
                                       **_claims(body))
        return _call_view(call)

    @router.post("/machine/attempts/{attempt_id}/start")
    def machine_start(attempt_id: str, body: MachineStart, request: Request):
        call = app.state.execution.start(request.headers.get("X-Instance-Token"), attempt_id,
                                         trace_id=body.trace_id, **_claims(body))
        return _call_view(call)

    @router.post("/machine/attempts/{attempt_id}/lease")
    def machine_lease(attempt_id: str, body: MachineCallback, request: Request):
        call = app.state.execution.renew_lease(request.headers.get("X-Instance-Token"),
                                               attempt_id, **_claims(body))
        return _call_view(call)

    @router.post("/machine/attempts/{attempt_id}/artifacts", status_code=201)
    def machine_artifact_upload(attempt_id: str, body: MachineArtifact, request: Request):
        call = app.state.execution.put_artifact(
            request.headers.get("X-Instance-Token"), attempt_id, name=body.name,
            media_type=body.media_type, sha256=body.sha256,
            content=decode_artifact(body.content), **_claims(body))
        return _call_view(call)

    @router.post("/machine/attempts/{attempt_id}/result")
    def machine_result(attempt_id: str, body: MachineResult, request: Request):
        call = app.state.execution.report_result(
            request.headers.get("X-Instance-Token"), attempt_id, status=body.status,
            summary=body.summary, artifacts=tuple(entry.model_dump() for entry in body.artifacts),
            usage_units=body.usage_units, trace_id=body.trace_id, **_claims(body))
        return _call_view(call)

    @router.post("/machine/attempts/{attempt_id}/cancel")
    def machine_cancel(attempt_id: str, body: MachineCancel, request: Request):
        call = app.state.execution.cancel(request.headers.get("X-Instance-Token"), attempt_id,
                                          reason=body.reason, **_claims(body))
        return _call_view(call)

    @router.get("/machine/attempts/{attempt_id}/security-reviews")
    def machine_security_reviews(attempt_id: str, request: Request):
        listed = app.state.execution.security_reviews(
            request.headers.get("X-Instance-Token"), attempt_id)
        return [asdict(entry) for entry in listed]

    @router.post("/projects/{pid}/requirements", status_code=201)
    def create_requirement(pid: str, body: RequirementCreate, request: Request):
        with _access(request, project_id=pid, write=True) as access:
            return asdict(app.state.requirements.create_requirement(
                access, project_id=pid, title=body.title, content=body.content,
                acceptance_items=body.acceptance_items))

    @router.get("/projects/{pid}/requirements")
    def list_requirements(pid: str, request: Request):
        with _access(request, project_id=pid) as access:
            return [asdict(requirement) for requirement
                    in app.state.requirements.list_requirements(access, project_id=pid)]

    @router.get("/projects/{pid}/requirements/{rid}")
    def get_requirement(pid: str, rid: str, request: Request):
        with _access(request, project_id=pid) as access:
            return asdict(app.state.requirements.get_requirement(
                access, project_id=pid, requirement_id=rid))

    @router.post("/projects/{pid}/requirements/{rid}/messages", status_code=201)
    def post_requirement_message(pid: str, rid: str, body: RequirementMessageCreate, request: Request):
        with _access(request, project_id=pid, write=True) as access:
            return [asdict(message) for message in app.state.requirements.post_message(
                access, project_id=pid, requirement_id=rid, content=body.content)]

    @router.get("/projects/{pid}/requirements/{rid}/messages")
    def list_requirement_messages(pid: str, rid: str, request: Request):
        with _access(request, project_id=pid) as access:
            return [asdict(message) for message in app.state.requirements.list_messages(
                access, project_id=pid, requirement_id=rid)]

    @router.post("/projects/{pid}/requirements/{rid}/revisions", status_code=201)
    def save_requirement_revision(pid: str, rid: str, body: RequirementRevisionCreate, request: Request):
        with _access(request, project_id=pid, write=True) as access:
            requirement, revision = app.state.requirements.save_revision(
                access, project_id=pid, requirement_id=rid, content=body.content,
                acceptance_items=body.acceptance_items, expected_version=body.expected_version)
            return {"requirement": asdict(requirement), "revision": asdict(revision)}

    @router.get("/projects/{pid}/requirements/{rid}/revisions")
    def list_requirement_revisions(pid: str, rid: str, request: Request):
        with _access(request, project_id=pid) as access:
            return [asdict(revision) for revision in app.state.requirements.list_revisions(
                access, project_id=pid, requirement_id=rid)]

    @router.get("/projects/{pid}/requirements/{rid}/revisions/diff")
    def requirement_revision_diff(pid: str, rid: str, request: Request,
                                  from_revision_id: Annotated[str, Query(min_length=1, max_length=64)],
                                  to_revision_id: Annotated[str, Query(min_length=1, max_length=64)]):
        with _access(request, project_id=pid) as access:
            return app.state.requirements.revision_diff(
                access, project_id=pid, requirement_id=rid,
                from_revision_id=from_revision_id, to_revision_id=to_revision_id)

    @router.get("/projects/{pid}/requirements/{rid}/revisions/{revid}")
    def read_requirement_revision(pid: str, rid: str, revid: str, request: Request):
        with _access(request, project_id=pid) as access:
            revision, content = app.state.requirements.read_revision(
                access, project_id=pid, requirement_id=rid, revision_id=revid)
            return {**asdict(revision), "content": content}

    @router.post("/projects/{pid}/requirements/{rid}/reviews")
    def review_requirement(pid: str, rid: str, body: RequirementReviewCreate, request: Request):
        with _access(request, project_id=pid, write=True) as access:
            if body.action == "request":
                if body.reviewer_user_id is None:
                    raise ValidationError("reviewer_user_id is required")
                requirement, review = app.state.requirements.request_review(
                    access, project_id=pid, requirement_id=rid, revision_id=body.revision_id,
                    reviewer_user_id=body.reviewer_user_id, expected_version=body.expected_version)
            else:
                requirement, review = app.state.requirements.decide_review(
                    access, project_id=pid, requirement_id=rid, revision_id=body.revision_id,
                    decision="approved" if body.action == "approve" else "rejected",
                    reason=body.reason, expected_version=body.expected_version)
            return {"requirement": asdict(requirement), "review": asdict(review)}

    @router.get("/projects/{pid}/requirements/{rid}/reviews")
    def list_requirement_reviews(pid: str, rid: str, request: Request):
        with _access(request, project_id=pid) as access:
            return [asdict(review) for review in app.state.requirements.list_reviews(
                access, project_id=pid, requirement_id=rid)]

    @router.post("/projects/{pid}/requirements/{rid}/actions")
    def requirement_action(pid: str, rid: str, body: RequirementAction, request: Request):
        """Controlled requirement moves: re-derive progress, pause, resume, or cancel."""
        with _access(request, project_id=pid, write=True) as access:
            if body.action == "advance":
                requirement = app.state.reviews.advance_requirement(
                    access, project_id=pid, requirement_id=rid,
                    expected_version=body.expected_version)
            elif body.action == "pause":
                requirement = app.state.reviews.pause_requirement(
                    access, project_id=pid, requirement_id=rid, reason=body.reason,
                    expected_version=body.expected_version)
            elif body.action == "resume":
                requirement = app.state.reviews.resume_requirement(
                    access, project_id=pid, requirement_id=rid,
                    expected_version=body.expected_version)
            else:
                requirement = app.state.reviews.cancel_requirement(
                    access, project_id=pid, requirement_id=rid, reason=body.reason,
                    expected_version=body.expected_version)
            return asdict(requirement)

    @router.get("/projects/{pid}/requirements/{rid}/pauses")
    def list_requirement_pauses(pid: str, rid: str, request: Request):
        """Why this requirement stopped and started again, oldest entry first.

        The reason is what a pause looks like from the outside: without it, "paused" and
        "nobody is working on this" are the same sentence to everyone waiting.
        """
        with _access(request, project_id=pid) as access:
            access.store.get_requirement(pid, rid)
            return [asdict(entry) for entry in access.store.list_requirement_pauses(pid, rid)]

    @router.get("/projects/{pid}/requirements/{rid}/activity")
    def requirement_activity(pid: str, rid: str, request: Request, limit: AuditLimit = 20):
        """What has happened to this requirement, newest first.

        The to-do list only says what a person still owes somebody. This is the other half of the
        same page: what moved while they were not looking. It reads the audit trail the decisions
        already wrote, so it can neither decide anything nor drift from what really happened.
        """
        with _access(request, project_id=pid) as access:
            access.store.get_requirement(pid, rid)
            return _activity_rows(access.conn, pid, rid, limit)

    @router.get("/projects/{pid}/requirements/{rid}/integration")
    def requirement_integration(pid: str, rid: str, request: Request):
        """Which acceptance items the accepted deliveries cover, and who checked this bundle.

        The requester should not be the one to notice that a covered acceptance item went
        uncovered: this is the reading of the bundle a manager makes before it is put in front of
        anybody else, so both halves are shown here rather than only the yes.
        """
        with _access(request, project_id=pid) as access:
            access.store.get_requirement(pid, rid)
            return {"report": app.state.reviews.integration_report(access.store, pid, rid),
                    "verdicts": [asdict(entry) for entry in
                                 access.store.list_requirement_integrations(pid, rid)]}

    @router.post("/projects/{pid}/requirements/{rid}/integration")
    def record_integration(pid: str, rid: str, body: IntegrationDecision, request: Request):
        """A manager says whether this exact bundle is fit to be signed for, or goes back."""
        with _access(request, project_id=pid, write=True) as access:
            requirement = app.state.reviews.record_integration(
                access, project_id=pid, requirement_id=rid, decision=body.decision,
                reason=body.reason, expected_version=body.expected_version)
            return asdict(requirement)

    @router.get("/projects/{pid}/requirements/{rid}/delivery")
    def requirement_delivery(pid: str, rid: str, request: Request):
        """The bundle the requester signs for, plus every verdict already recorded on it."""
        with _access(request, project_id=pid) as access:
            requirement = access.store.get_requirement(pid, rid)
            bundle = app.state.reviews.delivery_bundle(access.store, pid, rid)
            return {"requirement": asdict(requirement), "delivery": bundle,
                    "acceptances": [asdict(entry) for entry in
                                    access.store.list_requirement_acceptances(pid, rid)]}

    @router.post("/projects/{pid}/requirements/{rid}/acceptance")
    def accept_requirement(pid: str, rid: str, body: RequirementAcceptanceCreate,
                           request: Request):
        with _access(request, project_id=pid, write=True) as access:
            requirement = app.state.reviews.accept_requirement(
                access, project_id=pid, requirement_id=rid, decision=body.decision,
                reason=body.reason, expected_version=body.expected_version)
            return asdict(requirement)

    @router.post("/projects/{pid}/requirements/{rid}/plans", status_code=201)
    def generate_plan(pid: str, rid: str, request: Request):
        actor_id = _authorized_actor(request, project_id=pid, roles=MANAGERS)
        plan = app.state.planning.plan(project_id=pid, requirement_id=rid, actor_id=actor_id,
                                       require_approved_only=False)
        store = CollaborationStore(app.state.planning.database)
        return {"plan": plan_document(plan),
                "work_items": [asdict(item) for item in store.list_work_items(pid, plan.plan_id)],
                "dependencies": [asdict(dependency) for dependency in
                                 store.list_work_item_dependencies(pid, plan.plan_id)]}

    @router.get("/projects/{pid}/requirements/{rid}/plans")
    def list_plans(pid: str, rid: str, request: Request):
        with _access(request, project_id=pid) as access:
            return [plan_document(plan) for plan in app.state.planning.list_plans(
                access, project_id=pid, requirement_id=rid)]

    @router.get("/projects/{pid}/requirements/{rid}/plans/{planid}")
    def get_plan(pid: str, rid: str, planid: str, request: Request):
        with _access(request, project_id=pid) as access:
            plan, items, dependencies = app.state.planning.plan_detail(
                access, project_id=pid, requirement_id=rid, plan_id=planid)
            return {"plan": plan_document(plan),
                    "work_items": [asdict(item) for item in items],
                    "dependencies": [asdict(dependency) for dependency in dependencies]}

    @router.post("/projects/{pid}/work-items/{wid}/dispatch")
    def dispatch_work_item(pid: str, wid: str, request: Request):
        """Ask for one attempt of this work item; a repeated call replays the open attempt."""
        # Dispatch opens its own transactions, so authorization must release the write lock.
        actor_id = _authorized_actor(request, project_id=pid, roles=MANAGERS)
        return _attempt_view(app.state.dispatch.dispatch(pid, wid, actor_id=actor_id))

    @router.post("/projects/{pid}/work-items/{wid}/accept")
    def accept_work_item(pid: str, wid: str, body: AcceptanceDecision, request: Request):
        with _access(request, project_id=pid, roles=MANAGERS, write=True) as access:
            item = app.state.reviews.accept(access, project_id=pid, work_item_id=wid,
                                            decision=body.decision, reason=body.reason,
                                            expected_version=body.expected_version)
            return asdict(item)

    @router.post("/projects/{pid}/work-items/{wid}/rework")
    def rework_work_item(pid: str, wid: str, body: ExpectedVersion, request: Request):
        """Re-queue a returned work item so a new attempt may be dispatched for it."""
        with _access(request, project_id=pid, roles=MANAGERS, write=True) as access:
            item = app.state.reviews.rework(access, project_id=pid, work_item_id=wid,
                                            expected_version=body.expected_version)
            return asdict(item)

    @router.post("/projects/{pid}/work-items/{wid}/cancel")
    def cancel_work_item(pid: str, wid: str, body: WorkItemCancel, request: Request):
        with _access(request, project_id=pid, write=True) as access:
            item = app.state.reviews.cancel(access, project_id=pid, work_item_id=wid,
                                           reason=body.reason,
                                           expected_version=body.expected_version)
            return asdict(item)

    @router.post("/projects/{pid}/attempts/{aid}/reviews")
    def confirm_delivery(pid: str, aid: str, body: OwnerDecision, request: Request):
        with _access(request, project_id=pid, write=True) as access:
            attempt, review = app.state.reviews.confirm(access, project_id=pid, attempt_id=aid,
                                                        decision=body.decision, reason=body.reason,
                                                        expected_version=body.expected_version)
            return {"attempt": _attempt_view(attempt), "review": asdict(review)}

    @router.post("/projects/{pid}/attempts/{aid}/reconcile")
    def reconcile_attempt(pid: str, aid: str, body: AttemptReconcile, request: Request):
        """Settle an attempt whose stop was never confirmed, using what a human verified."""
        with _access(request, project_id=pid, write=True) as access:
            attempt = app.state.reviews.reconcile_attempt(
                access, project_id=pid, attempt_id=aid, outcome=body.outcome,
                reason=body.reason, expected_version=body.expected_version)
            return _attempt_view(attempt)

    @router.get("/projects/{pid}/attempts/{aid}/reviews")
    def delivery_view(pid: str, aid: str, request: Request):
        with _access(request, project_id=pid) as access:
            attempt = access.store.get_attempt(pid, aid)
            review = access.store.get_owner_review(aid)
            accepted = access.store.get_acceptance_review(aid)
            problems = app.state.reviews.gate_problems(access.store, attempt,
                                                       now=app.state.reviews.clock())
            return {"attempt": _attempt_view(attempt),
                    "owner_review": None if review is None else asdict(review),
                    "acceptance_review": None if accepted is None else asdict(accepted),
                    "security_reviews": [asdict(entry) for entry in
                                         access.store.list_security_reviews(pid, aid)],
                    "security_problems": problems,
                    "acceptance_items": [{"item_id": item_id, "text": text} for item_id, text in
                                         access.store.list_work_item_acceptance(
                                             attempt.work_item_id)]}

    @router.post("/projects/{pid}/attempts/{aid}/security-reviews", status_code=201)
    def record_security_rule(pid: str, aid: str, body: SecurityRuleCreate, request: Request):
        with _access(request, project_id=pid, roles=MANAGERS, write=True) as access:
            return asdict(app.state.reviews.record_rule(access, project_id=pid, attempt_id=aid,
                                                        rule_key=body.rule_key))

    @router.post("/projects/{pid}/security-reviews/{srid}/decide")
    def decide_security_review(pid: str, srid: str, body: SecurityDecision, request: Request):
        with _access(request, project_id=pid, write=True) as access:
            return asdict(app.state.reviews.decide_security(
                access, project_id=pid, security_review_id=srid, decision=body.decision,
                reason=body.reason, expected_version=body.expected_version))

    @router.get("/projects/{pid}/security-grants")
    def list_security_grants(pid: str, request: Request):
        with _access(request, project_id=pid, roles=MANAGERS) as access:
            return [asdict(grant) for grant in access.store.list_security_grants(pid)]

    @router.put("/projects/{pid}/security-grants/{uid}")
    def set_security_grant(pid: str, uid: str, body: SecurityGrantUpdate, request: Request):
        with _access(request, project_id=pid, roles=("owner",), write=True) as access:
            return asdict(app.state.reviews.set_grant(access, project_id=pid, user_id=uid,
                                                      active=body.active,
                                                      expected_version=body.expected_version))

    @router.post("/projects/{pid}/scheduler/tick")
    def scheduler_tick(pid: str, request: Request):
        _authorized_actor(request, project_id=pid, roles=MANAGERS)
        return app.state.scheduler.tick(project_id=pid)

    @router.get("/projects/{pid}/outbox")
    def list_outbox(pid: str, request: Request, limit: EventLimit = 100):
        with _access(request, project_id=pid, roles=MANAGERS) as access:
            return [{**asdict(event), "payload": json.loads(event.payload)}
                    for event in access.store.list_events(pid, limit=limit)]

    @router.get("/projects/{pid}/budget")
    def get_budget(pid: str, request: Request):
        with _access(request, project_id=pid) as access:
            return app.state.budgets.budget_view(access, project_id=pid)

    @router.put("/projects/{pid}/budget")
    def set_project_budget(pid: str, body: BudgetLimit, request: Request):
        with _access(request, project_id=pid, roles=MANAGERS, write=True) as access:
            return app.state.budgets.set_limit(
                access, scope="project", ref_id=pid, limit_units=body.limit_units,
                expected_version=body.expected_version, project_id=pid)

    @router.put("/projects/{pid}/requirements/{rid}/budget")
    def set_requirement_budget(pid: str, rid: str, body: BudgetLimit, request: Request):
        with _access(request, project_id=pid, roles=MANAGERS, write=True) as access:
            access.store.get_requirement(pid, rid)
            return app.state.budgets.set_limit(
                access, scope="requirement", ref_id=rid, limit_units=body.limit_units,
                expected_version=body.expected_version, project_id=pid)

    @router.put("/admin/budget")
    def set_global_budget(body: BudgetLimit, request: Request):
        with _access(request, admin=True, write=True) as access:
            return app.state.budgets.set_limit(
                access, scope="global", ref_id="", limit_units=body.limit_units,
                expected_version=body.expected_version)

    def _inbox(access) -> list[dict]:
        """Drafting to-dos and execution decisions in one list, newest first, each stamped with
        whether this person has already seen it."""
        merged = app.state.inbox.stamp(
            access, app.state.requirements.inbox(access) + app.state.inbox.pending(access))
        return sorted(merged, key=lambda entry: (-entry["updated_at"], entry["item_type"],
                                                 entry.get("requirement_id") or ""))

    @router.get("/inbox")
    def inbox(request: Request):
        with _access(request) as access:
            return _inbox(access)

    def _stamped_feed(access, limit: int) -> list[dict]:
        """The news list with this reader's unread flag on it; read state, so it may lag."""
        rows = _feed_rows(access.conn, access.user.user_id, limit)
        seen = access.store.inbox_acks(access.user.user_id)
        for row in rows:
            row["key"] = f"activity:{row['event_id']}"
            row["unread"] = row["key"] not in seen
        return rows

    @router.get("/feed")
    def my_feed(request: Request, limit: AuditLimit = 20):
        """News for this person across their projects: what moved, not what they owe.

        The to-do list is a decision with a name on it; this is the same audit trail read from the
        other side, so a requester learns the plan was published without opening every requirement.
        """
        with _access(request) as access:
            return _stamped_feed(access, limit)

    @router.post("/inbox/ack", status_code=204)
    def ack_inbox_todo(body: InboxAck, request: Request):
        """Say "I have seen this" about a to-do or a news item the caller really is owed."""
        with _access(request, write=True) as access:
            known = {entry["key"] for entry in _inbox(access)}
            # Widest window the read view allows: an acknowledgement must not fail because the
            # person was looking at a longer list than this request happens to carry.
            known |= {row["key"] for row in _stamped_feed(access, 200)}
            app.state.inbox.ack(access, item_key=body.item_key, known=known)

    @router.get("/projects/{pid}/audit")
    def project_audit(pid: str, request: Request, limit: AuditLimit = 100):
        with _access(request, project_id=pid, roles=MANAGERS) as access:
            return _audit_rows(access.conn, pid, limit)

    @app.get("/ui", include_in_schema=False)
    def panel_root_redirect():
        return RedirectResponse("/ui/", status_code=307)

    app.include_router(router)
    # The panel is static files: it holds no data and no session, so every read still goes
    # through the same cookie-authenticated JSON API the page calls from the browser.
    app.mount("/ui", StaticFiles(directory=Path(__file__).resolve().parent / "web",
                                 html=True), name="ui")
    return app
