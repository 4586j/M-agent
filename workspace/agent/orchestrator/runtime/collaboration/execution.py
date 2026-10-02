"""Machine-facing execution protocol: pull the frozen package, then report progress.

A dispatch wake-up is advisory and carries no authority. Everything a worker claims is
re-read from the database here, so a stale, forged or duplicated callback can leave
evidence but can never advance business state.
"""
from __future__ import annotations

import base64
import binascii
from dataclasses import dataclass
import hashlib
import json
import time
from typing import Any

from .auth import record_audit, token_digest
from .budgets import BudgetService
from .instances import InstanceService
from .models import (
    AgentProjectBinding, Attempt, AttemptArtifact, Conflict, ExecutionInstance, InvalidState,
    NotFound, OPEN_ATTEMPT_STATUSES, SecurityReview, ValidationError,
)
from .store import CollaborationStore, _digest, _epoch, _text

MAX_ARTIFACT_BYTES = 32 * 1024 * 1024
MAX_RESULT_BYTES = 262144
PACKAGE_MIME = "application/json"
REQUIREMENT_MIME = "text/markdown;charset=utf-8"
RESULT_STATUSES = ("submitted", "failed")
WORK_ITEM_ON_RESULT = {"submitted": "awaiting_owner_review", "failed": "failed"}


def _canonical(payload: dict) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _digest_of(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _optional_text(value: str | None, field_name: str, limit: int) -> str | None:
    return None if value is None else _text(value, field_name, limit)


def decode_artifact(encoded: str) -> bytes:
    """Decode a base64 body without ever echoing the rejected characters back."""
    if not isinstance(encoded, str) or len(encoded) > 4 * ((MAX_ARTIFACT_BYTES + 2) // 3) + 4:
        raise ValidationError("Artifact is too large")
    try:
        return base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError):
        raise ValidationError("Artifact content must be base64") from None


@dataclass(frozen=True)
class Callback:
    """What the worker asserts about the attempt it is reporting on."""
    input_hash: str
    control_epoch: int
    lease_epoch: int

    @classmethod
    def verify(cls, input_hash: str, control_epoch: int, lease_epoch: int) -> "Callback":
        return cls(_digest(input_hash, "input_hash"), _epoch(control_epoch, "control_epoch"),
                   _epoch(lease_epoch, "lease_epoch"))


@dataclass(frozen=True)
class Call:
    outcome: str
    attempt: Attempt
    artifact: AttemptArtifact | None = None


@dataclass(frozen=True)
class _Failure:
    kind: type
    message: str
    reason: str


@dataclass(frozen=True)
class _Plan:
    outcome: str = "accepted"
    advance: dict[str, Any] | None = None
    renew_until: float | None = None
    work_item: str | None = None
    artifact: dict[str, Any] | None = None
    record: AttemptArtifact | None = None
    reason: str | None = None


class ExecutionService:
    """Serves one attempt only to the live instance that still holds its execution right."""

    def __init__(self, database, *, instances: InstanceService, clock=time.time,
                 lease_ttl: float = 120.0, budgets: BudgetService | None = None):
        if type(lease_ttl) not in (int, float) or not 1 <= lease_ttl <= 86400:
            raise ValidationError("Invalid lease lifetime")
        self.database = database
        self.instances = instances
        self.clock = clock
        self.lease_ttl = float(lease_ttl)
        self.budgets = budgets if budgets is not None else BudgetService(clock=clock)

    # --- pull paths ------------------------------------------------------------

    def package(self, instance_token: str | None, attempt_id: str) -> tuple[bytes, Attempt]:
        """The exact bytes that were frozen at dispatch, so the worker can check the digest."""
        with self.database.read() as conn:
            store = CollaborationStore(self.database, connection=conn)
            _instance, _binding, attempt = self._owned(store, instance_token, attempt_id)
            payload = attempt.package_json.encode("utf-8")
            if _digest_of(payload) != attempt.input_hash:
                raise Conflict("Task package does not match its frozen digest")
            return payload, attempt

    def requirement_text(self, instance_token: str | None,
                         attempt_id: str) -> tuple[bytes, Attempt]:
        """The complete approved original, never a summary or a truncated slice."""
        with self.database.read() as conn:
            store = CollaborationStore(self.database, connection=conn)
            _instance, _binding, attempt = self._owned(store, instance_token, attempt_id)
            text = store.get_revision_content(attempt.project_id, attempt.requirement_id,
                                              attempt.revision_id)
            payload = text.encode("utf-8")
            if _digest_of(payload) != attempt.revision_hash:
                raise Conflict("Requirement text does not match the frozen revision")
            return payload, attempt

    def artifacts(self, instance_token: str | None,
                  attempt_id: str) -> list[AttemptArtifact]:
        with self.database.read() as conn:
            store = CollaborationStore(self.database, connection=conn)
            _instance, _binding, attempt = self._owned(store, instance_token, attempt_id)
            return store.list_artifacts(attempt.project_id, attempt.attempt_id)

    def artifact_content(self, instance_token: str | None, attempt_id: str,
                         artifact_id: str) -> tuple[bytes, AttemptArtifact]:
        """Own output, or an upstream artifact the frozen package declared by id and digest."""
        with self.database.read() as conn:
            store = CollaborationStore(self.database, connection=conn)
            _instance, _binding, attempt = self._owned(store, instance_token, attempt_id)
            artifact = store.get_artifact(attempt.project_id, artifact_id)
            if artifact.attempt_id != attempt.attempt_id:
                declared = self._dependency_digests(attempt).get(artifact_id)
                if declared != artifact.sha256:
                    raise NotFound("Resource not found")
            content = store.get_artifact_content(attempt.project_id, artifact_id)
            if _digest_of(content) != artifact.sha256 or len(content) != artifact.size_bytes:
                raise Conflict("Artifact content does not match its recorded digest")
            return content, artifact

    def security_reviews(self, instance_token: str | None,
                         attempt_id: str) -> list[SecurityReview]:
        """The gate records of this attempt; a pending hit means the worker must pause."""
        with self.database.read() as conn:
            store = CollaborationStore(self.database, connection=conn)
            _instance, _binding, attempt = self._owned(store, instance_token, attempt_id)
            return store.list_security_reviews(attempt.project_id, attempt.attempt_id)

    def held_attempts(self, instance_token: str | None) -> list[Attempt]:
        """Reconciliation view: what this instance was told to run, newest first."""
        with self.database.read() as conn:
            store = CollaborationStore(self.database, connection=conn)
            instance = self._live(store, instance_token)
            return store.attempts_for_instance(instance.instance_id)

    @staticmethod
    def _dependency_digests(attempt: Attempt) -> dict[str, str]:
        try:
            package = json.loads(attempt.package_json)
        except (TypeError, ValueError):
            raise Conflict("Task package is unreadable") from None
        digests = {}
        entries = package.get("dependencies") if isinstance(package, dict) else None
        for entry in entries if isinstance(entries, list) else ():
            artifacts = entry.get("artifacts") if isinstance(entry, dict) else None
            for artifact in artifacts if isinstance(artifacts, list) else ():
                if isinstance(artifact, dict) and isinstance(artifact.get("artifact_id"), str):
                    digests[artifact["artifact_id"]] = artifact.get("sha256")
        return digests

    def _live(self, store: CollaborationStore, instance_token: str | None) -> ExecutionInstance:
        return self.instances.live_identity(store, token_digest(instance_token))

    def _owned(self, store: CollaborationStore, instance_token: str | None,
               attempt_id: str) -> tuple[ExecutionInstance, AgentProjectBinding, Attempt]:
        instance = self._live(store, instance_token)
        binding = store.get_binding_by_id(instance.binding_id)
        try:
            attempt = store.get_attempt(binding.project_id, attempt_id)
        except NotFound:
            raise NotFound("Resource not found") from None
        if attempt.instance_id != instance.instance_id or attempt.binding_id != binding.binding_id:
            raise NotFound("Resource not found")
        return instance, binding, attempt

    # --- callback plumbing -----------------------------------------------------

    def _callback(self, input_hash: str, control_epoch: int, lease_epoch: int) -> Callback:
        return Callback.verify(input_hash, control_epoch, lease_epoch)

    def _run(self, instance_token: str | None, attempt_id: str, *, stage: str,
             callback: Callback, payload: dict, plan, require_lease: bool = True) -> Call:
        """Verify one call, then persist its transition, report and audit together."""
        now = self.clock()
        failure: _Failure | None = None
        applied: Call | None = None
        with self.database.transaction() as conn:
            store = CollaborationStore(self.database, connection=conn)
            instance, binding, attempt = self._owned(store, instance_token, attempt_id)
            failure = self._verified(attempt, instance, binding, callback,
                                     require_lease=require_lease)
            if failure is None:
                planned = plan(attempt, store, now)
                if isinstance(planned, _Failure):
                    failure = planned
                else:
                    applied = self._apply(conn, store, attempt, stage, callback, payload,
                                          planned, now)
        if failure is not None:
            self._record_rejection(attempt_id, stage, callback, payload, failure)
            raise failure.kind(failure.message)
        assert applied is not None
        return applied

    def _verified(self, attempt: Attempt, instance: ExecutionInstance,
                  binding: AgentProjectBinding, callback: Callback, *,
                  require_lease: bool) -> _Failure | None:
        checks = (
            (callback.input_hash != attempt.input_hash,
             (Conflict, "Input digest mismatch", "input digest mismatch")),
            (callback.control_epoch != attempt.control_epoch,
             (Conflict, "Control epoch changed", "stale control epoch")),
            (attempt.control_epoch != instance.control_epoch,
             (Conflict, "Execution right was revoked", "execution right revoked")),
            (callback.lease_epoch != attempt.lease_epoch,
             (Conflict, "Lease epoch changed", "stale lease epoch")),
            (attempt.binding_version != binding.version,
             (Conflict, "Binding identity changed", "binding version changed")),
            # A terminal attempt has no lease to keep fresh; the plan reports the closure.
            (require_lease and attempt.status in OPEN_ATTEMPT_STATUSES
             and attempt.lease_expires_at <= self.clock(),
             (Conflict, "Lease has expired", "lease expired")),
        )
        for violated, (kind, message, reason) in checks:
            if violated:
                return _Failure(kind, message, reason)
        return None

    def _apply(self, conn, store: CollaborationStore, attempt: Attempt, stage: str,
               callback: Callback, payload: dict, planned: _Plan, now: float) -> Call:
        current = attempt
        if planned.advance is not None:
            current = store.advance_attempt(
                current.attempt_id, expected_version=current.version, updated_at=now,
                lease_epoch=current.lease_epoch, control_epoch=current.control_epoch,
                **planned.advance)
        if planned.renew_until is not None:
            current = store.bump_attempt_lease(current.attempt_id,
                                               control_epoch=current.control_epoch,
                                               lease_expires_at=planned.renew_until,
                                               updated_at=now)
        if planned.work_item is not None:
            item = store.get_work_item(current.project_id, current.work_item_id)
            # Only a work item this attempt is actually running may follow its outcome.
            if item.status == "running":
                store.advance_work_item(item.work_item_id, status=planned.work_item,
                                        expected_version=item.version, updated_at=now)
        if planned.advance is not None and stage in ("result", "cancel"):
            self._settle(store, current, stage, now)
        artifact = planned.record
        if planned.artifact is not None:
            artifact = store.add_attempt_artifact(current.attempt_id, created_at=now,
                                                  **planned.artifact)
        store.record_attempt_report(current.attempt_id, stage=stage,
                                    lease_epoch=callback.lease_epoch,
                                    control_epoch=callback.control_epoch,
                                    input_hash=callback.input_hash,
                                    payload=_canonical(payload), accepted=True,
                                    reason=planned.reason, created_at=now)
        record_audit(conn, now, None, f"attempt.{stage}", current.attempt_id,
                     project_id=current.project_id, target_version=current.version)
        return Call(planned.outcome, current, artifact)

    def _settle(self, store: CollaborationStore, attempt: Attempt, stage: str,
                now: float) -> None:
        """Move one attempt's share of the plan reservation; the attempt key makes it once-only."""
        item = store.get_work_item(attempt.project_id, attempt.work_item_id)
        # The plan reserved each item's estimate once, so only the first attempt spends that share.
        reserved = item.estimated_units if attempt.number == 1 else 0
        if stage == "result":
            self.budgets.settle(store, project_id=attempt.project_id,
                                requirement_id=attempt.requirement_id,
                                attempt_id=attempt.attempt_id, reserved_units=reserved,
                                actual_units=attempt.usage_units, created_at=now)
        elif reserved:
            # The worker confirmed it stopped before reporting any result: give the share back.
            self.budgets.release(store, project_id=attempt.project_id,
                                 requirement_id=attempt.requirement_id,
                                 attempt_id=attempt.attempt_id, reserved_units=reserved,
                                 created_at=now)

    def _record_rejection(self, attempt_id: str, stage: str, callback: Callback,
                          payload: dict, failure: _Failure) -> None:
        now = self.clock()
        with self.database.transaction() as conn:
            store = CollaborationStore(self.database, connection=conn)
            report = store.record_attempt_report(
                attempt_id, stage=stage, lease_epoch=callback.lease_epoch,
                control_epoch=callback.control_epoch, input_hash=callback.input_hash,
                payload=_canonical({**payload, "rejected": failure.reason}), accepted=False,
                reason=failure.reason, created_at=now)
            record_audit(conn, now, None, "attempt.callback_rejected", attempt_id,
                         project_id=report.project_id, target_version=None)

    @staticmethod
    def _closed(attempt: Attempt) -> _Failure:
        return _Failure(InvalidState, "Attempt is no longer open", "attempt is closed")

    # --- progress callbacks ----------------------------------------------------

    def ack(self, instance_token: str | None, attempt_id: str, *, input_hash: str,
            control_epoch: int, lease_epoch: int) -> Call:
        callback = self._callback(input_hash, control_epoch, lease_epoch)

        def plan(attempt: Attempt, store: CollaborationStore, now: float):
            if attempt.status == "assigned":
                return _Plan(advance={"status": "acked"})
            if attempt.status in ("acked", "running", "submitted"):
                return _Plan(outcome="duplicate", reason="duplicate ack")
            return self._closed(attempt)

        return self._run(instance_token, attempt_id, stage="ack", callback=callback,
                         payload={}, plan=plan)

    def start(self, instance_token: str | None, attempt_id: str, *, input_hash: str,
              control_epoch: int, lease_epoch: int, trace_id: str | None = None) -> Call:
        callback = self._callback(input_hash, control_epoch, lease_epoch)
        claimed = _optional_text(trace_id, "trace_id", 128)

        def plan(attempt: Attempt, store: CollaborationStore, now: float):
            if attempt.status in ("assigned", "acked"):
                advance = {"status": "running"}
                if claimed is not None:
                    advance["trace_id"] = claimed
                return _Plan(advance=advance)
            if attempt.status in ("running", "submitted"):
                return _Plan(outcome="duplicate", reason="duplicate start")
            return self._closed(attempt)

        return self._run(instance_token, attempt_id, stage="start", callback=callback,
                         payload={} if claimed is None else {"trace_id": claimed}, plan=plan)

    def renew_lease(self, instance_token: str | None, attempt_id: str, *, input_hash: str,
                    control_epoch: int, lease_epoch: int) -> Call:
        callback = self._callback(input_hash, control_epoch, lease_epoch)

        def plan(attempt: Attempt, store: CollaborationStore, now: float):
            if attempt.status not in OPEN_ATTEMPT_STATUSES:
                return self._closed(attempt)
            return _Plan(renew_until=now + self.lease_ttl)

        return self._run(instance_token, attempt_id, stage="lease", callback=callback,
                         payload={"lease_ttl": self.lease_ttl}, plan=plan, require_lease=False)

    def put_artifact(self, instance_token: str | None, attempt_id: str, *, input_hash: str,
                     control_epoch: int, lease_epoch: int, name: str, media_type: str,
                     sha256: str, content: bytes) -> Call:
        callback = self._callback(input_hash, control_epoch, lease_epoch)
        artifact_name = _text(name, "artifact name", 512)
        artifact_type = _text(media_type, "media_type", 128)
        digest = _digest(sha256, "sha256")
        if not isinstance(content, bytes):
            raise ValidationError("Artifact content must be bytes")
        if len(content) > MAX_ARTIFACT_BYTES:
            raise ValidationError("Artifact exceeds the maximum size")
        if _digest_of(content) != digest:
            raise ValidationError("Artifact digest mismatch")
        summary = {"name": artifact_name, "media_type": artifact_type, "sha256": digest,
                   "size_bytes": len(content)}

        def plan(attempt: Attempt, store: CollaborationStore, now: float):
            existing = store.get_artifact_by_name(attempt.attempt_id, artifact_name)
            if existing is not None:
                if existing.sha256 != digest:
                    return _Failure(Conflict, "Artifact name is already used",
                                    "artifact name already used")
                return _Plan(outcome="duplicate", record=existing, reason="duplicate artifact")
            if attempt.status not in OPEN_ATTEMPT_STATUSES:
                return self._closed(attempt)
            return _Plan(artifact={"name": artifact_name, "media_type": artifact_type,
                                   "content": content, "sha256": digest})

        return self._run(instance_token, attempt_id, stage="artifact", callback=callback,
                         payload=summary, plan=plan)

    def report_result(self, instance_token: str | None, attempt_id: str, *, input_hash: str,
                      control_epoch: int, lease_epoch: int, status: str, summary: str,
                      artifacts: tuple[dict, ...] = (), usage_units: int = 0,
                      trace_id: str | None = None) -> Call:
        callback = self._callback(input_hash, control_epoch, lease_epoch)
        if status not in RESULT_STATUSES:
            raise ValidationError("Invalid result status")
        description = _text(summary, "summary", 8192)
        claimed = _optional_text(trace_id, "trace_id", 128)
        if type(usage_units) is not int or usage_units < 0:
            raise ValidationError("Invalid usage")
        declared = tuple({"name": _text(entry["name"], "artifact name", 512),
                          "sha256": _digest(entry["sha256"], "sha256")} for entry in artifacts)
        if len({entry["name"] for entry in declared}) != len(declared):
            raise ValidationError("Duplicate artifact name")
        result_json = _canonical({"summary": description, "artifacts": list(declared)})
        if len(result_json.encode("utf-8")) > MAX_RESULT_BYTES:
            raise ValidationError("Result is too large")
        result_hash = _digest_of(result_json.encode("utf-8"))

        def plan(attempt: Attempt, store: CollaborationStore, now: float):
            if attempt.status == status and attempt.result_hash == result_hash:
                return _Plan(outcome="duplicate", reason="duplicate result")
            if attempt.status not in OPEN_ATTEMPT_STATUSES:
                return self._closed(attempt)
            for entry in declared:
                stored = store.get_artifact_by_name(attempt.attempt_id, entry["name"])
                if stored is None or stored.sha256 != entry["sha256"]:
                    return _Failure(Conflict, "Declared artifact is missing",
                                    "declared artifact is missing")
            advance = {"status": status, "result_json": result_json,
                       "result_hash": result_hash, "usage_units": usage_units}
            if claimed is not None:
                advance["trace_id"] = claimed
            return _Plan(advance=advance, work_item=WORK_ITEM_ON_RESULT[status])

        payload = {"status": status, "result_hash": result_hash,
                   "usage_units": usage_units, "artifact_count": len(declared)}
        return self._run(instance_token, attempt_id, stage="result", callback=callback,
                         payload=payload, plan=plan)

    def cancel(self, instance_token: str | None, attempt_id: str, *, input_hash: str,
               control_epoch: int, lease_epoch: int, reason: str = "") -> Call:
        """The worker confirms it stopped; the item stays assignable to the same person."""
        callback = self._callback(input_hash, control_epoch, lease_epoch)
        explanation = _text(reason, "reason", 2048) if reason else ""

        def plan(attempt: Attempt, store: CollaborationStore, now: float):
            if attempt.status == "cancelled":
                return _Plan(outcome="duplicate", reason="duplicate cancel")
            if attempt.status not in OPEN_ATTEMPT_STATUSES:
                return self._closed(attempt)
            return _Plan(advance={"status": "cancelled"}, work_item="waiting_agent")

        return self._run(instance_token, attempt_id, stage="cancel", callback=callback,
                         payload={} if not explanation else {"reason": explanation},
                         plan=plan, require_lease=False)
