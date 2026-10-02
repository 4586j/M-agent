"""独立协作执行实例：拉取冻结任务包、校验摘要、持久占位、执行并回传。

信任边界与开发文档第 11.2 节一致：大脑的 wake 提示只是"该看一眼"的信号，本实例行动
前一定重新拉取，摘要、epoch、租约都以服务端返回值为准；任务包与需求原文都是不可信输入，
不携带任何修改权限、转派或跳过安全检查的效力。

旧 worker 的 /task、面板、终端与全局工作区都不在本入口之内，本模块也不导入它们。
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import dataclass
from ipaddress import ip_address
import argparse
import json
import os
from pathlib import Path
import re
import secrets
import threading
import time
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel, ConfigDict, Field
from starlette.exceptions import HTTPException
from starlette.responses import JSONResponse

from collaboration_client import (CollaborationClient, Conflict, Fetched, Missing, Rejected,
                                  Unauthorized, WorkerError, digest_of, validate_base_url)
from collaboration_journal import (PAUSED_GATE, RECONCILING, REFUSED, REPORTED, STARTED, STOPPED,
                                   ExecutionJournal, JournalError)

PACKAGE_SCHEMA_VERSION = "collab-package-v1"
MAX_ARTIFACT_BYTES = 8 * 1024 * 1024
MAX_ARTIFACTS = 64
MAX_SUMMARY_CHARS = 4096
MAX_WAKE_BYTES = 8192
ID_PATTERN = re.compile(r"^[0-9a-f]{32}$")
DIGEST_PATTERN = re.compile(r"^[0-9a-f]{64}$")
NAME_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")
# 本机 HTTP 面向操作者的结果代码，语义与第 11.2 节一致。
ACCEPTED = "accepted"
DUPLICATE = "duplicate"
RUNNING = "running"
FINISHED = "finished"
CLOSED = "closed"
STALE = "stale"
UNKNOWN = "unknown"

# 只有仍处于服务端开放状态的尝试才可以由本机确认停止。
RECONCILABLE = ("assigned", "acked", "running", "reconciliation_required")
CLOSED_STATUSES = ("accepted", "changes_requested", "cancelled", "failed")
TOKEN_ENV = "COLLAB_INSTANCE_TOKEN"


def _name(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not NAME_PATTERN.fullmatch(value):
        raise Rejected(f"Invalid {field_name}")
    return value


def _digest(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not DIGEST_PATTERN.fullmatch(value):
        raise Rejected(f"Invalid {field_name}")
    return value


def _identifier(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not ID_PATTERN.fullmatch(value):
        raise Rejected(f"Invalid {field_name}")
    return value


def _loopback(host: str) -> bool:
    try:
        return ip_address(host).is_loopback
    except ValueError:
        return host == "localhost"


@dataclass(frozen=True)
class Produced:
    """One output file the runner claims to have produced."""
    name: str
    media_type: str
    content: bytes


@dataclass(frozen=True)
class RunRequest:
    attempt_id: str
    work_item_id: str
    input_hash: str
    requirement_hash: str
    package: dict
    requirement_text: str
    work_dir: Path
    allowed_tools: tuple
    cancel: threading.Event


@dataclass(frozen=True)
class RunOutcome:
    status: str
    summary: str
    produced: tuple = ()
    usage_units: int = 0


class FakeRunner:
    """模拟执行接缝：只证明协议与授权可用，不调用模型，也不修改项目。

    真实 SDK 执行属于开发文档第 13 节的 P6/T09。本执行器把"模拟"写进产物与摘要，
    所以任何验收记录都不会把它误当成真实模型产出。
    """

    def __init__(self, *, delay: float = 0.0, usage_units: int = 1):
        if not 0 <= delay <= 60:
            raise Rejected("Invalid delay")
        if type(usage_units) is not int or not 0 <= usage_units <= 10 ** 9:
            raise Rejected("Invalid usage")
        self.delay = delay
        self.usage_units = usage_units

    def __call__(self, request: RunRequest) -> RunOutcome:
        if request.cancel.wait(self.delay):
            return RunOutcome(status="failed", summary="模拟执行被取消")
        subtask = request.package.get("subtask") or {}
        body = "\n".join((
            "simulated: true",
            f"attempt_id: {request.attempt_id}",
            f"input_hash: {request.input_hash}",
            f"requirement_sha256: {request.requirement_hash}",
            f"requirement_characters: {len(request.requirement_text)}",
            f"subtask_key: {subtask.get('key')}",
            f"subtask_objective: {subtask.get('objective')}",
        ))
        return RunOutcome(status="submitted", summary=f"模拟执行完成：{subtask.get('key')}",
                          produced=(Produced(name="simulated-output.md",
                                             media_type="text/markdown",
                                             content=body.encode("utf-8")),),
                          usage_units=self.usage_units)


class IntegrityProblem(WorkerError):
    """The frozen inputs did not agree with each other, so nothing may run."""

    status = 0


class _Claims:
    """The epochs this machine reports with; only the service response may move them."""

    def __init__(self, input_hash: str, control_epoch: int, lease_epoch: int):
        self.input_hash = _digest(input_hash, "input hash")
        self.control_epoch = control_epoch
        self.lease_epoch = lease_epoch
        self._lock = threading.Lock()

    def snapshot(self) -> dict:
        with self._lock:
            return {"input_hash": self.input_hash, "control_epoch": self.control_epoch,
                    "lease_epoch": self.lease_epoch}

    def advance(self, call: dict) -> None:
        attempt = call.get("attempt") if isinstance(call, dict) else None
        if not isinstance(attempt, dict):
            raise Conflict("Callback response is missing its attempt view")
        if attempt.get("input_hash") != self.input_hash:
            raise Conflict("The service reports a different frozen input")
        epochs = {}
        for field_name in ("control_epoch", "lease_epoch"):
            value = attempt.get(field_name)
            if type(value) is not int or not 1 <= value <= 10 ** 12:
                raise Conflict(f"Invalid {field_name} in the service response")
            epochs[field_name] = value
        with self._lock:
            self.control_epoch = epochs["control_epoch"]
            self.lease_epoch = epochs["lease_epoch"]


@dataclass(frozen=True)
class WorkerSettings:
    """Local deployment facts; secrets come from the environment, never from these fields."""

    base_url: str
    state_dir: Path
    listen_host: str = "127.0.0.1"
    listen_port: int = 8400
    wake_path: str = "/wake"
    capacity: int = 1
    lease_interval: float = 30.0
    poll_interval: float = 30.0
    runner: str = "none"
    allow_remote: bool = False

    def __post_init__(self):
        object.__setattr__(self, "base_url", validate_base_url(self.base_url))
        object.__setattr__(self, "state_dir", Path(self.state_dir))
        if not isinstance(self.allow_remote, bool):
            raise Rejected("allow_remote must be a boolean")
        if not _loopback(self.listen_host) and not self.allow_remote:
            raise Rejected("Remote listen hosts require allow_remote")
        if type(self.listen_port) is not int or not 1 <= self.listen_port <= 65535:
            raise Rejected("Invalid listen port")
        if not re.fullmatch(r"/[A-Za-z0-9._~/-]{1,127}", self.wake_path) \
                or self.wake_path in ("/health", "/attempts"):
            raise Rejected("Invalid wake path")
        if type(self.capacity) is not int or not 1 <= self.capacity <= 64:
            raise Rejected("Invalid capacity")
        if not isinstance(self.lease_interval, (int, float)) or not 5 <= self.lease_interval <= 600:
            raise Rejected("Invalid lease interval")
        if not isinstance(self.poll_interval, (int, float)) or not 1 <= self.poll_interval <= 600:
            raise Rejected("Invalid poll interval")
        if self.runner not in ("none", "fake"):
            raise Rejected("Runner must be none or fake")
        self.state_dir.mkdir(parents=True, exist_ok=True)

    @property
    def journal_path(self) -> Path:
        return self.state_dir / "journal.sqlite3"

    @property
    def work_root(self) -> Path:
        return self.state_dir / "work"

    @property
    def token_path(self) -> Path:
        return self.state_dir / "instance-token"


def read_instance_token(settings: WorkerSettings) -> str | None:
    """Prefer the environment so a test or a secret store never has to touch the disk."""
    value = os.environ.get(TOKEN_ENV)
    if value:
        return value if 16 <= len(value) <= 512 else None
    if not settings.token_path.exists():
        return None
    token = settings.token_path.read_text(encoding="utf-8").strip()
    return token if 16 <= len(token) <= 512 else None


def write_instance_token(settings: WorkerSettings, token: str) -> None:
    if not isinstance(token, str) or not re.fullmatch(r"[A-Za-z0-9_-]{16,512}", token):
        raise Rejected("Instance token is not usable")
    descriptor = os.open(settings.token_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(token)
    try:
        os.chmod(settings.token_path, 0o600)
    except OSError:
        pass


class CollaborationWorker:
    """Drive one attempt from an advisory wake-up to a reported result."""

    def __init__(self, client: CollaborationClient, journal: ExecutionJournal, *,
                 settings: WorkerSettings, runner=None, clock=time.time,
                 executor: str | None = None):
        if runner is not None and not callable(runner):
            raise Rejected("Runner must be callable")
        self.client = client
        self.journal = journal
        self.settings = settings
        self.runner = runner
        self.clock = clock
        self.executor = _name(executor or f"worker-{secrets.token_hex(8)}", "executor")
        self._slots = threading.BoundedSemaphore(settings.capacity)
        self._threads: dict[str, threading.Thread] = {}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._background: list[threading.Thread] = []
        settings.work_root.mkdir(parents=True, exist_ok=True)

    # --- what the local HTTP surface and the operator may ask --------------------

    def health(self) -> dict:
        return {"status": "ready" if self.runner is not None else "idle",
                "claimed": self.client.instance_token is not None,
                "capacity": self.settings.capacity, "busy": len(self._active()),
                "runner": self.settings.runner, "simulated": self.settings.runner == "fake",
                "states": self.journal.counts()}

    def attempt_view(self, attempt_id: str) -> dict | None:
        """Local journal view: state names and digests only, never requirement text or tokens."""
        entry = self.journal.get(_identifier(attempt_id, "attempt id"))
        if entry is None:
            return None
        view = entry.view()
        view["events"] = self.journal.events(entry.attempt_id, limit=20)
        return view

    def wake(self, payload: dict) -> str:
        """Handle one advisory wake-up; the answer only ever names an outcome."""
        attempt_id = _identifier(payload["attempt_id"], "attempt id")
        _identifier(payload["work_item_id"], "work item id")
        _digest(payload["input_hash"], "input hash")
        control_epoch = payload["control_epoch"]
        if type(control_epoch) is not int or not 1 <= control_epoch <= 10 ** 12:
            raise Rejected("Invalid control epoch")
        held = self._held(attempt_id)
        if held is None:
            return UNKNOWN
        # Every field of the hint has to agree with the service before this thread acts on it.
        if held["work_item_id"] != payload["work_item_id"] \
                or held["input_hash"] != payload["input_hash"] \
                or held["control_epoch"] != control_epoch:
            return STALE
        return self._drive(held)

    def poll(self) -> list[str]:
        """Reconcile against everything the service currently holds; lost wake-ups land here."""
        return [self._drive(held) for held in self._held_list()]

    def run(self, attempt_id: str) -> str:
        """Execute one attempt inline; background threads and tests share this entry point."""
        return self._execute(_identifier(attempt_id, "attempt id"))

    def wait_idle(self, timeout: float = 10.0) -> bool:
        deadline = self.clock() + timeout
        while True:
            threads = self._active()
            if not threads:
                return True
            if self.clock() >= deadline:
                return False
            for thread in threads:
                thread.join(max(0.001, min(0.05, deadline - self.clock())))

    def start(self) -> None:
        """Take over anything a previous process left mid-flight, then keep polling."""
        self.journal.recover()
        thread = threading.Thread(target=self._serve_loop, name="collab-serve", daemon=True)
        self._background.append(thread)
        thread.start()

    def stop(self) -> None:
        self._stop.set()
        for thread in self._background:
            thread.join(timeout=2.0)

    def close(self) -> None:
        self.stop()
        self.client.close()
        self.journal.close()

    # --- scheduling --------------------------------------------------------------

    def _active(self) -> list[threading.Thread]:
        with self._lock:
            return [thread for thread in self._threads.values() if thread.is_alive()]

    def _drive(self, held: dict) -> str:
        attempt_id = _identifier(held["attempt_id"], "attempt id")
        entry = self.journal.get(attempt_id)
        if entry is not None and entry.settled:
            return FINISHED if entry.state == REPORTED else CLOSED
        if entry is not None and entry.state == STARTED:
            return RUNNING
        try:
            outcome, entry = self.journal.discover(
                attempt_id=attempt_id,
                work_item_id=_identifier(held["work_item_id"], "work item id"),
                input_hash=_digest(held["input_hash"], "input hash"),
                control_epoch=held["control_epoch"])
        except JournalError:
            return UNKNOWN
        if outcome == "conflicting":
            return STALE
        if entry.state == STARTED:
            return RUNNING
        if self._running_here(attempt_id):
            return ACCEPTED
        if not self._slots.acquire(blocking=False):
            # No free slot right now: the row stays drivable and the next poll picks it up.
            return ACCEPTED
        thread = threading.Thread(target=self._guarded, args=(attempt_id,),
                                  name=f"collab-{attempt_id[:8]}", daemon=True)
        with self._lock:
            self._threads[attempt_id] = thread
        thread.start()
        return ACCEPTED

    def _running_here(self, attempt_id: str) -> bool:
        with self._lock:
            thread = self._threads.get(attempt_id)
        return thread is not None and thread.is_alive()

    def _guarded(self, attempt_id: str) -> None:
        try:
            self._execute(attempt_id)
        except Exception as error:
            self._mark(attempt_id, RECONCILING, f"execution stopped: {type(error).__name__}")
        finally:
            with self._lock:
                self._threads.pop(attempt_id, None)
            self._slots.release()

    # --- one attempt -------------------------------------------------------------

    def _execute(self, attempt_id: str) -> str:
        entry = self.journal.get(attempt_id)
        if entry is None:
            return UNKNOWN
        if entry.settled:
            return FINISHED if entry.state == REPORTED else CLOSED
        if entry.state == STARTED:
            return RUNNING
        held = self._held(attempt_id)
        if held is None:
            self._mark(attempt_id, RECONCILING, "the service no longer holds this attempt here")
            return STALE
        status = held["status"]
        if status in CLOSED_STATUSES:
            self._mark(attempt_id, STOPPED, f"the service already closed this attempt as {status}")
            return CLOSED
        if status == "submitted":
            self._mark(attempt_id, REPORTED, "the service already has this result")
            return FINISHED
        if status == "reconciliation_required":
            return self._reconcile(attempt_id, held)
        if self.runner is None:
            self._mark(attempt_id, PAUSED_GATE, "this machine has no execution runner configured")
            return CLOSED
        claims = _Claims(held["input_hash"], held["control_epoch"], held["lease_epoch"])
        try:
            package, requirement_text, requirement_hash = self._verify(attempt_id, held)
        except IntegrityProblem as error:
            self._mark(attempt_id, REFUSED, error.reason)
            return CLOSED
        except WorkerError as error:
            self._mark(attempt_id, RECONCILING, error.reason)
            return STALE
        gate = self._gate(attempt_id, claims, held, package)
        if gate is not None:
            self._mark(attempt_id, gate[0], gate[1])
            return CLOSED
        if not self.journal.claim_execution(attempt_id, executor=self.executor):
            return DUPLICATE
        try:
            return self._run(attempt_id, held, package, requirement_text, requirement_hash, claims)
        except WorkerError as error:
            self.journal.release_execution(attempt_id, executor=self.executor,
                                           state=RECONCILING, reason=error.reason)
            return STALE
        except JournalError as error:
            self.journal.release_execution(
                attempt_id, executor=self.executor, state=RECONCILING,
                reason=f"journal refused the transition: {type(error).__name__}")
            return STALE

    def _run(self, attempt_id: str, held: dict, package: dict, requirement_text: str,
             requirement_hash: str, claims: _Claims) -> str:
        trace_id = f"collab:{attempt_id}"
        claims.advance(self.client.ack(attempt_id, claims.snapshot()))
        claims.advance(self.client.start(attempt_id, claims.snapshot(), trace_id=trace_id))
        cancel, stop, lost = threading.Event(), threading.Event(), []
        renewer = threading.Thread(target=self._keep_lease,
                                   args=(attempt_id, claims, cancel, stop, lost),
                                   name=f"collab-lease-{attempt_id[:8]}", daemon=True)
        renewer.start()
        request = RunRequest(attempt_id=attempt_id, work_item_id=held["work_item_id"],
                             input_hash=claims.input_hash, requirement_hash=requirement_hash,
                             package=package, requirement_text=requirement_text,
                             work_dir=self._attempt_dir(attempt_id),
                             allowed_tools=tuple(package.get("policy", {}).get("allowed_tools", ())
                                                 or ()),
                             cancel=cancel)
        try:
            try:
                outcome = self.runner(request)
                report = self._classify(outcome)
            except Exception as error:
                report = {"status": "failed", "usage_units": 0, "produced": [],
                          "summary": f"execution failed: {type(error).__name__}"}
        finally:
            stop.set()
            renewer.join(timeout=max(1.0, float(self.settings.lease_interval)))
        if cancel.is_set():
            # The lease moved while the runner was busy: stop and reconcile, never report over it.
            return self._reconcile(attempt_id, self._held(attempt_id), holder=True,
                                   reason=lost[0] if lost else "the lease moved during execution")
        try:
            declared = []
            for produced in report["produced"]:
                call = self.client.artifact_upload(attempt_id, claims.snapshot(),
                                                   name=produced["name"],
                                                   media_type=produced["media_type"],
                                                   content=produced["content"])
                claims.advance(call)
                declared.append({"name": produced["name"], "sha256": produced["sha256"]})
            call = self.client.result(attempt_id, claims.snapshot(), status=report["status"],
                                      summary=report["summary"], artifacts=declared,
                                      usage_units=report["usage_units"], trace_id=trace_id)
            claims.advance(call)
        except WorkerError as error:
            self.journal.release_execution(attempt_id, executor=self.executor,
                                           state=RECONCILING, reason=error.reason)
            return STALE
        self.journal.release_execution(
            attempt_id, executor=self.executor, state=REPORTED,
            reason=f"result {call['outcome']} accepted by the service")
        return FINISHED

    def _keep_lease(self, attempt_id: str, claims: _Claims, cancel: threading.Event,
                    stop: threading.Event, lost: list) -> None:
        while not stop.wait(self.settings.lease_interval):
            try:
                claims.advance(self.client.renew_lease(attempt_id, claims.snapshot()))
            except WorkerError as error:
                lost.append(f"lease renewal was rejected: {error.reason}")
                cancel.set()
                return

    def _reconcile(self, attempt_id: str, held: dict | None, *, holder: bool = False,
                   reason: str = "the service asked this attempt to reconcile") -> str:
        """Stop locally, and confirm the stop only while the service still accepts our epochs."""
        if holder:
            self.journal.release_execution(attempt_id, executor=self.executor,
                                           state=RECONCILING, reason=reason)
        else:
            self._mark(attempt_id, RECONCILING, reason)
        if held is None or held["status"] not in RECONCILABLE:
            return STALE
        claims = _Claims(held["input_hash"], held["control_epoch"], held["lease_epoch"])
        try:
            self.client.cancel(attempt_id, claims.snapshot(),
                               reason="this machine stopped executing before confirming")
        except WorkerError as error:
            # The execution right may already be revoked: say so, never claim a confirmed stop.
            self._mark(attempt_id, RECONCILING,
                       f"stop confirmation was not accepted: {error.reason}")
            return STALE
        self._mark(attempt_id, STOPPED, "this machine confirmed it is not executing anymore")
        return CLOSED

    # --- verification and the security gate ---------------------------------------

    def _verify(self, attempt_id: str, held: dict) -> tuple[dict, str, str]:
        """Re-read the frozen package and the complete original, and check every digest claim."""
        package_bytes = self.client.package(attempt_id)
        if package_bytes.digest != held["input_hash"]:
            raise IntegrityProblem("the task package does not match the digest the service holds")
        try:
            package = json.loads(package_bytes.content)
        except ValueError:
            raise IntegrityProblem("the task package is not readable JSON") from None
        if not isinstance(package, dict) \
                or package.get("schema_version") != PACKAGE_SCHEMA_VERSION:
            raise IntegrityProblem("the task package is not the schema this worker speaks")
        if package.get("work_item_id") != held["work_item_id"]:
            raise IntegrityProblem("the task package describes a different work item")
        documents = package.get("documents")
        if not isinstance(documents, list) or len(documents) != 1 \
                or not isinstance(documents[0], dict):
            raise IntegrityProblem("the task package is missing the requirement original")
        document = documents[0]
        if document.get("kind") != "requirement_text" or document.get("complete") is not True \
                or document.get("href") != "requirement" \
                or document.get("revision_id") != held["revision_id"]:
            raise IntegrityProblem("the requirement original is not the complete approved one")
        claimed = document.get("content_hash")
        if not isinstance(claimed, str) or not DIGEST_PATTERN.fullmatch(claimed):
            raise IntegrityProblem("the requirement original has no usable digest")
        original = self.client.requirement(attempt_id)
        if original.digest != claimed:
            raise IntegrityProblem("the requirement original does not match the task package")
        if type(document.get("size_bytes")) is not int \
                or document["size_bytes"] != len(original.content):
            raise IntegrityProblem("the requirement original has an unexpected size")
        try:
            text = original.content.decode("utf-8")
        except UnicodeDecodeError:
            raise IntegrityProblem("the requirement original is not UTF-8") from None
        paths = self._store(attempt_id, held["input_hash"], claimed,
                            (package_bytes, original))
        self.journal.record_inputs(attempt_id, input_hash=held["input_hash"],
                                   requirement_hash=claimed, package_path=paths[0],
                                   requirement_path=paths[1], control_epoch=held["control_epoch"],
                                   lease_epoch=held["lease_epoch"])
        return package, text, claimed

    def _gate(self, attempt_id: str, claims: _Claims, held: dict,
              package: dict) -> tuple[str, str] | None:
        """A pending or rejected hit blocks execution; a pass for another surface never lifts it.

        The service re-checks the same fields when a human confirms, so this local view is
        deliberately fail-closed: anything that does not name this exact input, revision and
        binding version keeps the attempt paused instead of running on a borrowed pass.
        """
        surface = {"input_hash": claims.input_hash, "revision_id": held["revision_id"],
                   "binding_version": package.get("policy", {}).get("binding_version")}
        for review in self.client.security_reviews(attempt_id):
            rule = _digest_free(review.get("rule_key"))
            matches = all(review.get(name) == value for name, value in surface.items())
            if review.get("decision") == "rejected" and matches:
                return REFUSED, f"security rule {rule} was rejected for this input"
            if review.get("decision") != "approved" or not matches:
                return PAUSED_GATE, f"security rule {rule} is not approved for this input"
        return None

    def _attempt_dir(self, attempt_id: str) -> Path:
        return self.settings.work_root / attempt_id

    def _store(self, attempt_id: str, input_hash: str, requirement_hash: str,
               payloads: tuple[Fetched, Fetched]) -> tuple[str, str]:
        """Keep one read-only local copy per digest so the original stays re-checkable."""
        directory = self._attempt_dir(attempt_id)
        directory.mkdir(parents=True, exist_ok=True)
        names = (f"package-{input_hash[:16]}.json", f"requirement-{requirement_hash[:16]}.md")
        stored = []
        for name, payload in zip(names, payloads):
            path = directory / name
            if not path.exists():
                descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o440)
                with os.fdopen(descriptor, "wb") as handle:
                    handle.write(payload.content)
            try:
                os.chmod(path, 0o440)
            except OSError:
                pass
            stored.append(f"{attempt_id}/{name}")
        return tuple(stored)

    def _classify(self, outcome: Any) -> dict:
        """Turn whatever the runner returned into a request the protocol actually allows."""
        invalid = {"status": "failed", "produced": [], "usage_units": 0}
        if not isinstance(outcome, RunOutcome) or outcome.status not in ("submitted", "failed"):
            return dict(invalid, summary="the runner did not return a usable outcome")
        summary = outcome.summary.strip()[:MAX_SUMMARY_CHARS] \
            if isinstance(outcome.summary, str) else ""
        produced = list(outcome.produced or ())
        if len(produced) > MAX_ARTIFACTS:
            return dict(invalid, summary="the runner produced too many artifacts")
        entries, seen = [], set()
        for item in produced:
            if not isinstance(item, Produced) or not isinstance(item.content, bytes) \
                    or not 0 <= len(item.content) <= MAX_ARTIFACT_BYTES:
                return dict(invalid, summary="an artifact is missing or exceeds the local limit")
            if not isinstance(item.name, str) or not NAME_PATTERN.fullmatch(item.name) \
                    or item.name in seen:
                return dict(invalid, summary="an artifact name is not usable")
            seen.add(item.name)
            media_type = item.media_type if isinstance(item.media_type, str) \
                and 1 <= len(item.media_type) <= 128 else "application/octet-stream"
            entries.append({"name": item.name, "media_type": media_type,
                            "sha256": digest_of(item.content), "content": item.content})
        usage = outcome.usage_units if type(outcome.usage_units) is int \
            and 0 <= outcome.usage_units <= 10 ** 12 else 0
        return {"status": outcome.status, "summary": summary or "the runner gave no summary",
                "usage_units": usage, "produced": entries}

    # --- service reads ------------------------------------------------------------

    def _held_list(self) -> list[dict]:
        views = []
        for attempt in self.client.held_attempts():
            if not isinstance(attempt.get("attempt_id"), str) \
                    or not ID_PATTERN.fullmatch(attempt["attempt_id"]) \
                    or not isinstance(attempt.get("input_hash"), str) \
                    or not DIGEST_PATTERN.fullmatch(attempt["input_hash"]) \
                    or not isinstance(attempt.get("status"), str) \
                    or type(attempt.get("control_epoch")) is not int \
                    or type(attempt.get("lease_epoch")) is not int:
                raise Conflict("Held attempt view is incomplete")
            views.append(attempt)
        return views

    def _held(self, attempt_id: str) -> dict | None:
        try:
            attempts = self._held_list()
        except (Unauthorized, Missing):
            return None
        for attempt in attempts:
            if attempt["attempt_id"] == attempt_id:
                return attempt
        return None

    def _mark(self, attempt_id: str, state: str, reason: str) -> None:
        try:
            self.journal.note(attempt_id, state, reason=reason[:512])
        except JournalError:
            pass

    def _serve_loop(self) -> None:
        while not self._stop.wait(self.settings.poll_interval):
            try:
                self.client.heartbeat()
                self.poll()
            except WorkerError:
                continue


def _digest_free(value: Any) -> str:
    """Quote a rule key into a log-safe token without echoing arbitrary server text."""
    return value if isinstance(value, str) and NAME_PATTERN.fullmatch(value) else "unknown"


# --- local HTTP surface -------------------------------------------------------------


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid")


class WakeBody(Input):
    attempt_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    work_item_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    input_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    control_epoch: int = Field(strict=True, ge=1, le=10 ** 12)


_ERRORS = {403: "Forbidden", 404: "Not found", 405: "Method not allowed",
           413: "Request too large", 422: "Invalid request", 500: "Internal server error"}


def _error(status: int) -> JSONResponse:
    return JSONResponse({"detail": _ERRORS.get(status, "Request failed")}, status_code=status,
                        headers={"Cache-Control": "no-store"})


def create_worker_app(worker: CollaborationWorker) -> FastAPI:
    """The worker's own loopback surface: a wake target, health, and a local journal view."""
    wake_path = worker.settings.wake_path

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        worker.journal.recover()
        yield

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None,
                  redirect_slashes=False)

    @app.middleware("http")
    async def request_boundary(request: Request, call_next):
        if request.method in ("POST", "PUT", "PATCH", "DELETE"):
            lengths = request.headers.getlist("content-length")
            if len(lengths) != 1 or not lengths[0].isdigit() or int(lengths[0]) > MAX_WAKE_BYTES:
                return _error(413)
        try:
            response = await call_next(request)
        except Exception:
            # Never pass exception text, request bodies or tokens into a response or a log.
            return _error(500)
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.exception_handler(RequestValidationError)
    async def invalid(request: Request, exc: Exception):
        return _error(422)

    @app.exception_handler(HTTPException)
    async def http(request: Request, exc: HTTPException):
        return _error(exc.status_code if exc.status_code in _ERRORS else 405)

    @app.get("/health")
    def health():
        return worker.health()

    @app.post(wake_path)
    def wake(request: Request, body: WakeBody):
        headers = request.headers.getlist("x-collab-wake")
        if len(headers) != 1 or headers[0] != "1":
            return _error(403)
        try:
            return {"outcome": worker.wake(body.model_dump())}
        except WorkerError:
            # A wake is only a hint: a service problem must not leak detail to the caller.
            return {"outcome": STALE}

    @app.get("/attempts/{attempt_id}")
    def attempt(attempt_id: str):
        try:
            view = worker.attempt_view(attempt_id)
        except Rejected:
            return _error(404)
        return view if view is not None else _error(404)

    return app


# --- command line --------------------------------------------------------------------


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        # argparse otherwise echoes unknown arguments, which can contain a token.
        super().error("Invalid arguments or operation rejected")


def _items(value: str) -> list[str]:
    return [entry.strip() for entry in value.split(",") if entry.strip()]


def _number(value: str, minimum: float, maximum: float) -> float:
    try:
        number = float(value)
    except ValueError:
        raise Rejected("Invalid number") from None
    if not minimum <= number <= maximum:
        raise Rejected("Number out of range")
    return number


def main(argv=None) -> int:
    parser = _Parser(description="Standalone collaboration execution instance")
    commands = parser.add_subparsers(dest="command", required=True)
    claim = commands.add_parser("claim", help="Turn a one-shot claim token into an instance token")
    claim.add_argument("--base-url", required=True)
    claim.add_argument("--state-dir", type=Path, required=True)
    claim.add_argument("--node-token-env", default="COLLAB_NODE_TOKEN")
    claim.add_argument("--claim-token-env", default="COLLAB_CLAIM_TOKEN")
    claim.add_argument("--workspace-ref", required=True)
    claim.add_argument("--provider-ref", required=True)
    claim.add_argument("--allowed-tools", default="")
    claim.add_argument("--env-allowlist", default="")
    claim.add_argument("--isolation-mode", default="none")
    claim.add_argument("--isolation-attestation", default="")
    claim.add_argument("--capacity", type=int, default=1)
    serve = commands.add_parser("serve", help="Serve the collaboration worker entry point")
    serve.add_argument("--base-url", required=True)
    serve.add_argument("--state-dir", type=Path, required=True)
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8400)
    serve.add_argument("--wake-path", default="/wake")
    serve.add_argument("--capacity", type=int, default=1)
    serve.add_argument("--lease-interval", default="30")
    serve.add_argument("--poll-interval", default="30")
    serve.add_argument("--runner", choices=("none", "fake"), default="none")
    serve.add_argument("--allow-remote", action="store_true")
    args = parser.parse_args(argv)

    def report(message: str) -> int:
        print(message)
        return 1

    if args.command == "claim":
        node_token = os.environ.get(args.node_token_env)
        claim_token = os.environ.get(args.claim_token_env)
        if not node_token or not claim_token:
            return report("Node or claim token environment variable is not set")
        client = CollaborationClient(args.base_url)
        try:
            result = client.claim(node_token=node_token, claim_token=claim_token,
                                  capacity=args.capacity, isolation_mode=args.isolation_mode,
                                  isolation_attestation=args.isolation_attestation,
                                  workspace_ref=args.workspace_ref,
                                  provider_ref=args.provider_ref,
                                  allowed_tools=_items(args.allowed_tools),
                                  env_allowlist=_items(args.env_allowlist))
            settings = WorkerSettings(base_url=args.base_url, state_dir=args.state_dir,
                                      capacity=args.capacity)
            write_instance_token(settings, result["instance_token"])
        except (WorkerError, JournalError, Rejected):
            return report("Claim was rejected")
        finally:
            client.close()
        print(result["instance_id"])
        return 0

    try:
        settings = WorkerSettings(
            base_url=args.base_url, state_dir=args.state_dir, listen_host=args.host,
            listen_port=args.port, wake_path=args.wake_path, capacity=args.capacity,
            lease_interval=_number(args.lease_interval, 5, 600),
            poll_interval=_number(args.poll_interval, 1, 600), runner=args.runner,
            allow_remote=args.allow_remote)
        token = read_instance_token(settings)
        if not token:
            return report("No instance token: run claim first or set COLLAB_INSTANCE_TOKEN")
    except (Rejected, OSError):
        return report("Worker configuration was rejected")

    runner = FakeRunner() if settings.runner == "fake" else None
    client = CollaborationClient(settings.base_url, instance_token=token)
    journal = ExecutionJournal(settings.journal_path)
    worker = CollaborationWorker(client, journal, settings=settings, runner=runner)
    try:
        client.heartbeat()
    except WorkerError:
        client.close()
        journal.close()
        return report("The collaboration service rejected this instance token")
    worker.start()
    import uvicorn

    try:
        uvicorn.run(create_worker_app(worker), host=settings.listen_host,
                    port=settings.listen_port, proxy_headers=False)
    finally:
        worker.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
