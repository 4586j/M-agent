"""Task-package assembly and durable dispatch of execution attempts.

The brain pushes an advisory wake-up and the worker pulls the package with its own
instance token, so the wake payload is never an authorization: it carries only the
attempt id and the frozen input digest, and every fact the worker acts on is re-read
from the database when the worker asks for it.
"""
from __future__ import annotations

import hashlib
import json
import time

from .auth import record_audit
from .instances import AdmissionDenied, InstanceService
from .models import LEASED_ATTEMPT_STATUSES, Attempt, Conflict, InvalidState, ValidationError
from .store import CollaborationStore

PACKAGE_SCHEMA_VERSION = "collab-package-v1"
TOPIC_ATTEMPT_ASSIGNED = "attempt.assigned"
TOPIC_ATTEMPT_REVOKED = "attempt.revoked"
DISPATCHABLE_STATUSES = ("ready", "waiting_agent")
REPORT_STAGES_ALLOWED = ("ack", "start", "lease", "artifact", "result", "cancel")


class DeliveryVoided(Exception):
    """The thing this event asks for can never happen; retrying it would hide that fact."""


def _canonical(payload: dict) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


class DispatchService:
    """Turns a ready work item plus a live instance into one persisted attempt."""

    def __init__(self, database, *, instances: InstanceService, clock=time.time,
                 lease_ttl: float = 120.0, requires_real_data: bool = True, push=None):
        if type(lease_ttl) not in (int, float) or not 1 <= lease_ttl <= 86400:
            raise ValidationError("Invalid lease lifetime")
        if type(requires_real_data) is not bool:
            raise ValidationError("requires_real_data must be a boolean")
        self.database = database
        self.instances = instances
        self.clock = clock
        self.lease_ttl = float(lease_ttl)
        self.requires_real_data = requires_real_data
        self.push = push

    def _state(self, store, project_id: str, work_item_id: str):
        item = store.get_work_item(project_id, work_item_id)
        plan = store.get_plan(project_id, item.plan_id)
        if plan.status != "published":
            raise InvalidState("The plan is not published")
        requirement = store.get_requirement(project_id, plan.requirement_id)
        if requirement.approved_revision_id != plan.revision_id:
            raise InvalidState("The plan no longer matches the approved revision")
        revision = store.get_revision(project_id, requirement.requirement_id, plan.revision_id)
        binding = store.get_binding_by_id(item.binding_id)
        return item, plan, requirement, revision, binding

    def _dependencies(self, store, project_id: str, plan_id: str, work_item_id: str) -> list[dict]:
        dependencies = []
        edges = [edge for edge in store.list_work_item_dependencies(project_id, plan_id)
                 if edge.work_item_id == work_item_id]
        for edge in edges:
            upstream = store.get_work_item(project_id, edge.depends_on_id)
            accepted = [attempt for attempt in store.list_attempts(project_id, upstream.work_item_id)
                        if attempt.status == "accepted"]
            if upstream.status != "accepted" or not accepted:
                raise AdmissionDenied("dependency_not_accepted")
            latest = max(accepted, key=lambda attempt: attempt.number)
            artifacts = [{"artifact_id": artifact.artifact_id, "name": artifact.name,
                          "media_type": artifact.media_type, "sha256": artifact.sha256,
                          "size_bytes": artifact.size_bytes,
                          "href": f"artifacts/{artifact.artifact_id}"}
                         for artifact in store.list_artifacts(project_id, latest.attempt_id)]
            dependencies.append({"work_item_id": upstream.work_item_id, "key": upstream.key,
                                 "title": upstream.title, "attempt_id": latest.attempt_id,
                                 "artifacts": artifacts})
        return sorted(dependencies, key=lambda entry: entry["key"])

    def _rework_notes(self, store, project_id: str, work_item_id: str,
                      requirement_id: str, item_key: str) -> list[dict]:
        """Every human return that speaks about this item, in the order the rounds actually happened.

        Both the owner's confirmation and the business acceptance can send one delivery back, and
        the requester's verdict on the whole requirement can send every accepted delivery of the
        plan back at once — or only the items it names, now that a return can point. So can a
        manager's integration check, which is the one return that says the bundle as a whole does
        not cover the original text. All four are notes the next attempt has to read, so they share
        one list; a requirement-level note that named other items than this one is not this
        attempt's instruction.
        The attempt number orders the per-delivery notes because a clock can hand two reviewers the
        same second; a requirement verdict has no attempt of its own, so within one second it
        comes after the deliveries it covered.
        """
        notes = []
        for kind, entries in (("owner_confirmation", store.list_returned_reviews(project_id,
                                                                                work_item_id)),
                              ("acceptance", store.list_returned_acceptances(project_id,
                                                                             work_item_id))):
            for entry in entries:
                notes.append({"kind": kind, "attempt_id": entry.attempt_id,
                              "attempt_number": store.get_attempt(project_id,
                                                                   entry.attempt_id).number,
                              "reason": entry.reason, "reviewed_by": entry.reviewer_user_id,
                              "reviewed_at": entry.created_at})
        for entry in store.list_requirement_acceptances(project_id, requirement_id):
            if entry.decision != "changes_requested":
                continue
            if entry.returned_items and item_key not in entry.returned_items:
                continue
            notes.append({"kind": "requirement_acceptance", "attempt_id": None,
                          "attempt_number": None, "reason": entry.reason,
                          "reviewed_by": entry.reviewer_user_id,
                          "reviewed_at": entry.created_at})
        for entry in store.list_requirement_integrations(project_id, requirement_id):
            if entry.decision == "returned" and (not entry.returned_items
                                                 or item_key in entry.returned_items):
                notes.append({"kind": "requirement_integration", "attempt_id": None,
                              "attempt_number": None, "reason": entry.reason,
                              "reviewed_by": entry.actor_user_id,
                              "reviewed_at": entry.created_at})
        return sorted(notes, key=lambda note: (note["reviewed_at"],
                                               float("inf") if note["attempt_number"] is None
                                               else note["attempt_number"], note["kind"]))

    def package_core(self, store, project_id: str, work_item_id: str) -> tuple[str, str]:
        """Freeze the inputs of a dispatch; the digest covers everything but identity."""
        item, plan, requirement, revision, binding = self._state(store, project_id, work_item_id)
        payload = {
            "schema_version": PACKAGE_SCHEMA_VERSION,
            "project_id": project_id,
            "requirement_id": requirement.requirement_id,
            "requirement_title": requirement.title,
            "revision_id": plan.revision_id,
            "revision_hash": plan.revision_hash,
            "plan_id": plan.plan_id,
            "work_item_id": item.work_item_id,
            "documents": [{
                "kind": "requirement_text", "revision_id": plan.revision_id,
                "content_hash": revision.content_hash, "size_bytes": revision.content_size,
                "media_type": "text/markdown;charset=utf-8", "href": "requirement",
                "complete": True,
            }],
            "subtask": {
                "key": item.key, "title": item.title, "objective": item.objective,
                "scope": item.scope, "non_goals": item.non_goals, "duty": item.duty,
                "acceptance": [{"item_id": entry[0], "text": entry[1]}
                               for entry in store.list_work_item_acceptance(item.work_item_id)],
            },
            "dependencies": self._dependencies(store, project_id, plan.plan_id, item.work_item_id),
            # What a human already sent back for this item or for the whole delivery: the next
            # attempt is told which acceptance item missed instead of being asked to guess.
            "rework": self._rework_notes(store, project_id, item.work_item_id,
                                         requirement.requirement_id, item.key),
            "policy": {
                "binding_id": binding.binding_id, "binding_version": binding.version,
                "duty": binding.duty, "persona_version": binding.persona_version,
                "allowed_tools": list(binding.allowed_tools),
                "workspace_ref": binding.workspace_ref, "provider_ref": binding.provider_ref,
                "budget_units": item.estimated_units, "lease_ttl": self.lease_ttl,
                "callback_stages": list(REPORT_STAGES_ALLOWED),
                "documents_are_untrusted_input": True,
            },
        }
        serialized = _canonical(payload)
        return serialized, hashlib.sha256(serialized.encode("utf-8")).hexdigest()

    def _pick_instance(self, store, binding_id: str, *, now: float):
        fresh_since = now - self.instances.heartbeat_timeout
        candidates = [instance for instance in store.live_instances_for_binding(binding_id)
                      if instance.status == "active" and instance.heartbeat_at is not None
                      and instance.heartbeat_at >= fresh_since]
        if not candidates:
            raise AdmissionDenied("no_live_instance")
        return max(candidates, key=lambda instance: instance.heartbeat_at)

    def dispatch(self, project_id: str, work_item_id: str, *, actor_id: str | None = None,
                 requires_real_data: bool | None = None) -> Attempt:
        """Create the single open attempt of a ready work item, or return the existing one."""
        if requires_real_data is None:
            requires_real_data = self.requires_real_data
        now = self.clock()
        with self.database.read() as conn:
            store = CollaborationStore(self.database, connection=conn)
            item, plan, requirement, _revision, binding = self._state(store, project_id,
                                                                      work_item_id)
            package_json, input_hash = self.package_core(store, project_id, work_item_id)
            # A repeated dispatch is the recovery path for a lost wake-up, not a new attempt.
            existing = store.open_attempt(work_item_id)
            if existing is not None:
                if existing.input_hash != input_hash:
                    raise Conflict("Inputs changed while an attempt is open")
                return existing
            if item.status not in DISPATCHABLE_STATUSES:
                raise InvalidState("Work item is not ready for dispatch")
            if requirement.status == "paused":
                # A paused requirement keeps the rights it already handed out and starts nothing
                # new, so an attempt that is already open is still returned to its owner.
                raise InvalidState("Requirement is paused")
            instance = self._pick_instance(store, item.binding_id, now=now)
        self.instances.admit(project_id, item.binding_id, instance.instance_id,
                             requires_real_data=requires_real_data)
        with self.database.transaction() as conn:
            store = CollaborationStore(self.database, connection=conn)
            current = store.get_work_item(project_id, work_item_id)
            if (current.version != item.version or current.status not in DISPATCHABLE_STATUSES
                    or store.get_instance(project_id, instance.instance_id).control_epoch
                    != instance.control_epoch):
                raise Conflict("Work item or instance changed")
            raced = store.open_attempt(work_item_id)
            if raced is not None:
                if raced.input_hash != input_hash:
                    raise Conflict("Inputs changed while an attempt is open")
                return raced
            if store.get_requirement(project_id, plan.requirement_id).status == "paused":
                raise InvalidState("Requirement is paused")
            attempt = store.create_attempt(
                project_id, work_item_id, plan_id=plan.plan_id,
                requirement_id=requirement.requirement_id, revision_id=plan.revision_id,
                revision_hash=plan.revision_hash, instance_id=instance.instance_id,
                binding_id=binding.binding_id, binding_version=binding.version,
                assignee_user_id=current.assignee_user_id,
                number=store.next_attempt_number(work_item_id),
                schema_version=PACKAGE_SCHEMA_VERSION, input_hash=input_hash,
                package_json=package_json, control_epoch=instance.control_epoch,
                lease_expires_at=now + self.lease_ttl, created_at=now)
            store.advance_work_item(work_item_id, status="running",
                                    expected_version=current.version, updated_at=now)
            store.enqueue_event(project_id, TOPIC_ATTEMPT_ASSIGNED, {
                "attempt_id": attempt.attempt_id, "work_item_id": work_item_id,
                "instance_id": instance.instance_id, "input_hash": input_hash,
                "control_epoch": attempt.control_epoch}, now)
            record_audit(conn, now, actor_id, "attempt.assigned", attempt.attempt_id,
                         project_id=project_id, target_version=attempt.version)
            return attempt

    def reclaim_expired_leases(self, project_id: str | None = None) -> dict:
        """Take the reporting right back from a machine that stopped answering; decide nothing.

        A lease is the only proof the service has that a run is still alive. When it runs out, the
        attempt is parked as ``reconciliation_required`` under a revoked control epoch, so a worker
        that answers late can still leave evidence but cannot advance the work item — the same
        shape a human withdrawal already produces, reached here without anyone pressing a button.
        The service deliberately does not claim the outside world was untouched: no reservation is
        given back, no result is written and the work item keeps holding the round, because whether
        it cost something is what the person who checks that machine settles.
        """
        now = self.clock()
        reclaimed = 0
        listed = CollaborationStore(self.database).leased_attempts(now=now, project_id=project_id)
        for entry in listed:
            with self.database.transaction() as conn:
                store = CollaborationStore(self.database, connection=conn)
                attempt = store.get_attempt(entry.project_id, entry.attempt_id)
                if (attempt.status not in LEASED_ATTEMPT_STATUSES
                        or attempt.lease_expires_at > self.clock()):
                    continue
                if store.get_requirement(attempt.project_id,
                                         attempt.requirement_id).status == "paused":
                    # 18.27 的规则不因租约到点而改写：暂停留正在跑的不动。失联提醒照旧给到人，
                    # 人要撤销随时可以撤销；这条轮次只是等恢复之后才由服务端自己接管。
                    continue
                parked = store.advance_attempt(attempt.attempt_id, status="reconciliation_required",
                                               expected_version=attempt.version, updated_at=now)
                parked = store.revoke_attempt_execution_right(parked.attempt_id,
                                                              expected_version=parked.version,
                                                              updated_at=now)
                store.record_attempt_report(
                    parked.attempt_id, stage="cancel", lease_epoch=parked.lease_epoch,
                    control_epoch=parked.control_epoch, input_hash=parked.input_hash,
                    payload=_canonical({"lease_expires_at": attempt.lease_expires_at,
                                        "actor_user_id": None}),
                    accepted=True, reason="execution right reclaimed after lease expiry",
                    created_at=now)
                store.enqueue_event(parked.project_id, TOPIC_ATTEMPT_REVOKED, {
                    "attempt_id": parked.attempt_id, "work_item_id": parked.work_item_id,
                    "instance_id": parked.instance_id, "input_hash": parked.input_hash,
                    "control_epoch": parked.control_epoch}, now)
                record_audit(conn, now, None, "attempt.lease_reclaimed", parked.attempt_id,
                             project_id=parked.project_id, target_version=parked.version)
                reclaimed += 1
        return {"attempts_reclaimed": reclaimed}

    def wake(self, event) -> None:
        """Outbox handler: advisory wake-up of the instance that owns the attempt."""
        if self.push is None:
            raise InvalidState("No dispatch transport is configured")
        payload = json.loads(event.payload)
        with self.database.read() as conn:
            instance = CollaborationStore(self.database, connection=conn).get_instance(
                event.project_id, payload["instance_id"])
        if instance.status != "active":
            # A disabled slot never wakes again: nothing reactivates it and its row is never
            # deleted. So this push is not late, it is moot — and the round it was telling
            # about stays with the person who already owes the reconciliation.
            raise DeliveryVoided("the instance this wake was addressed to was disabled")
        if not instance.worker_url:
            # Not moot yet: the owner can still register where this machine listens.
            raise AdmissionDenied("worker_unreachable")
        self.push(instance.worker_url, {
            "attempt_id": payload["attempt_id"], "work_item_id": payload["work_item_id"],
            "input_hash": payload["input_hash"], "control_epoch": payload["control_epoch"]})
