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
from pydantic import BaseModel, ConfigDict, Field, SecretStr
from starlette.exceptions import HTTPException
from starlette.responses import JSONResponse

from .auth import AuthService, Unauthorized, Forbidden, RateLimited
from .budgets import BudgetService
from .database import Database
from .instances import InstanceService
from .models import Conflict, InvalidState, NotFound, Unavailable, ValidationError
from .planning import PlanService, plan_document
from .requirements import RequirementService
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

    def __post_init__(self):
        canonical, _, _ = _origin(self.public_origin)
        if type(self.session_ttl) is not int or not 60 <= self.session_ttl <= 86400:
            raise ValueError("session_ttl must be between 60 and 86400 seconds")
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
        app.state.scheduler = Scheduler(database, planning=app.state.planning,
                                        queue=app.state.queue)
        yield

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
            return asdict(access.store.get_project(pid))

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
            return [asdict(member) for member in access.store.list_members(pid)]

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
                access, project_id=pid, binding_id=bid, node_id=body.node_id)
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

    @router.get("/inbox")
    def inbox(request: Request):
        with _access(request) as access:
            return app.state.requirements.inbox(access)

    @router.get("/projects/{pid}/audit")
    def project_audit(pid: str, request: Request, limit: AuditLimit = 100):
        with _access(request, project_id=pid, roles=MANAGERS) as access:
            return _audit_rows(access.conn, pid, limit)

    app.include_router(router)
    return app
