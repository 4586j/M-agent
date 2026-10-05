"""Human decisions over delivered work: confirmation, security gate and withdrawal.

Every method runs inside the caller's transaction through ``access.store`` and
``access.conn``, so a decision, its audit row and any budget movement commit together.
"""
from __future__ import annotations

import hashlib
import json
import time

from .auth import Access, Forbidden, record_audit
from .budgets import BudgetService
from .dispatch import TOPIC_ATTEMPT_REVOKED, _canonical
from .models import (
    ATTEMPT_TERMINAL_STATUSES, AcceptanceReview, AgentProjectBinding, Attempt, Conflict,
    InvalidState, NotFound, OwnerReview, ProjectMember, Requirement, SecurityGrant,
    SecurityReview, ValidationError, WorkItem,
)
from .store import REQUIREMENT_TRANSITIONS, CollaborationStore, _choice, _text, _version

CAPABILITY = "security:approve"
CONFIRM_DECISIONS = ("confirmed", "changes_requested")
ACCEPT_DECISIONS = ("accepted", "changes_requested")
INTEGRATION_DECISIONS = ("passed", "returned")
# Both of these send a work item back to the queue: a returned delivery carries its note to
# the next attempt, while a failed one is retried with the very same frozen inputs.
REWORKABLE_STATUSES = ("changes_requested", "failed")
# A requirement is still delivering while its own work items can move it. This is exactly the set
# the resident host re-derives on a timer, and the only set a person may advance by hand, so the
# two paths cannot disagree about what "in progress" means. ``integrating`` belongs here: an
# accepted delivery that nobody has checked yet is still the team's business, not the requester's.
DELIVERING_STATUSES = ("executing", "rework", "integrating", "awaiting_acceptance")
# Only these two states hold dispatch authority, so they are the only ones a pause can stop:
# ``planning_blocked`` has nothing to dispatch, and ``awaiting_acceptance`` is waiting on a person
# rather than on work.
PAUSABLE_STATUSES = ("executing", "rework")
SECURITY_DECISIONS = ("approved", "rejected")
MAX_REASON_CHARS = 2048
MAX_RULE_KEY_CHARS = 128
DEFAULT_REVIEW_TTL = 86400.0
MANAGER_ROLES = ("owner", "manager")
# A reviewer may only lift a rule listed here. Identity, project authorization,
# source-text digest, sandbox boundary and budget limits stay un-liftable so no
# decision can widen the authority of the person making it.
PASSABLE_RULES = ("dangerous_command", "external_side_effect")
ATTEMPT_ON_CONFIRMATION = {"confirmed": "accepted", "changes_requested": "changes_requested"}
ITEM_ON_CONFIRMATION = {"confirmed": "submitted", "changes_requested": "changes_requested"}


def tool_policy_digest(binding: AgentProjectBinding) -> str:
    """Digest of the tool surface a security pass is bound to; any change voids the pass."""
    payload = json.dumps({
        "allowed_tools": sorted(binding.allowed_tools), "binding_version": binding.version,
        "duty": binding.duty, "persona_version": binding.persona_version,
        "provider_ref": binding.provider_ref, "workspace_ref": binding.workspace_ref,
    }, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class ReviewService:
    """Owner confirmation, business acceptance, security gate and withdrawal."""

    def __init__(self, *, clock=time.time, passable_rules: tuple[str, ...] = PASSABLE_RULES,
                 budgets: BudgetService | None = None, review_ttl: float = DEFAULT_REVIEW_TTL):
        if type(review_ttl) not in (int, float) or not 60 <= review_ttl <= 604800:
            raise ValidationError("Invalid security review lifetime")
        self.clock = clock
        self.passable_rules = tuple(passable_rules)
        self.budgets = budgets if budgets is not None else BudgetService(clock=clock)
        self.review_ttl = float(review_ttl)

    @staticmethod
    def _reason(reason: str) -> str:
        if not isinstance(reason, str):
            raise ValidationError("Invalid review reason")
        return "" if not reason else _text(reason, "reason", MAX_REASON_CHARS)

    @staticmethod
    def _member(access: Access, project_id: str) -> ProjectMember:
        member = access.store.get_member(project_id, access.user.user_id)
        if member.status != "active":
            raise NotFound("Resource not found")
        return member

    @staticmethod
    def _requires_manager(member: ProjectMember, assignee_user_id: str) -> None:
        if member.access_role in MANAGER_ROLES or member.user_id == assignee_user_id:
            return
        raise Forbidden("Only a project manager or the responsible member can decide")

    def _delivery(self, access: Access, project_id: str,
                  attempt_id: str) -> tuple[Attempt, WorkItem]:
        member = self._member(access, project_id)
        attempt = access.store.get_attempt(project_id, attempt_id)
        item = access.store.get_work_item(project_id, attempt.work_item_id)
        self._requires_manager(member, item.assignee_user_id)
        return attempt, item

    def _item(self, access: Access, project_id: str, work_item_id: str) -> WorkItem:
        member = self._member(access, project_id)
        item = access.store.get_work_item(project_id, work_item_id)
        self._requires_manager(member, item.assignee_user_id)
        return item

    # --- security gate ----------------------------------------------------------

    def has_capability(self, store: CollaborationStore, project_id: str, user_id: str) -> bool:
        if store.get_project(project_id).owner_id == user_id:
            return True
        grant = store.get_security_grant(project_id, user_id, CAPABILITY)
        return grant is not None and grant.status == "active"

    def gate_problems(self, store: CollaborationStore, attempt: Attempt,
                      *, now: float) -> list[str]:
        """Why the gate still blocks this attempt; empty means every hit was passed."""
        try:
            binding = store.get_binding_revision(attempt.project_id, attempt.binding_id,
                                                 attempt.binding_version)
        except NotFound:
            return ["binding-unavailable"]
        expected_policy = tool_policy_digest(binding)
        problems: list[str] = []
        for review in store.list_security_reviews(attempt.project_id, attempt.attempt_id):
            if (review.input_hash != attempt.input_hash
                    or review.revision_id != attempt.revision_id
                    or review.binding_version != attempt.binding_version
                    or review.tool_policy_version != expected_policy):
                problems.append(f"stale:{review.rule_key}")
            elif review.decision == "rejected":
                # A rejection is final and keeps blocking after its window: only a new
                # check on a changed surface can produce a record that lifts it.
                problems.append(f"rejected:{review.rule_key}")
            elif review.expires_at <= now:
                problems.append(f"expired:{review.rule_key}")
            elif review.decision == "pending":
                problems.append(f"pending:{review.rule_key}")
        return problems

    def record_rule(self, access: Access, *, project_id: str, attempt_id: str,
                    rule_key: str) -> SecurityReview:
        """Register a rule hit as a pending gate record; only a reviewer can decide it."""
        key = _text(rule_key, "rule_key", MAX_RULE_KEY_CHARS)
        self._member(access, project_id)
        attempt = access.store.get_attempt(project_id, attempt_id)
        existing = access.store.security_review_by_rule(attempt_id, key)
        if existing is not None:
            return existing
        binding = access.store.get_binding_revision(project_id, attempt.binding_id,
                                                   attempt.binding_version)
        now = self.clock()
        review = access.store.add_security_review(
            attempt_id, rule_key=key, requirement_id=attempt.requirement_id,
            revision_id=attempt.revision_id, binding_version=attempt.binding_version,
            tool_policy_version=tool_policy_digest(binding), expires_at=now + self.review_ttl,
            created_at=now)
        record_audit(access.conn, now, access.user.user_id, "security.review_created",
                     review.security_review_id, project_id=project_id,
                     target_version=review.version)
        return review

    def decide_security(self, access: Access, *, project_id: str, security_review_id: str,
                        decision: str, reason: str,
                        expected_version: int) -> SecurityReview:
        verdict = _choice(decision, SECURITY_DECISIONS, "decision")
        explanation = self._reason(reason)
        if not explanation.strip():
            raise ValidationError("A security decision must carry a reason")
        self._member(access, project_id)
        review = access.store.get_security_review(project_id, security_review_id)
        if not self.has_capability(access.store, project_id, access.user.user_id):
            raise Forbidden(f"{CAPABILITY} is required")
        if verdict == "approved" and review.rule_key not in self.passable_rules:
            raise Forbidden("This security rule cannot be lifted by a human decision")
        now = self.clock()
        if review.expires_at <= now:
            raise InvalidState("Security review has expired")
        _version(review.version, expected_version)
        updated = access.store.decide_security_review(
            security_review_id, decision=verdict, reviewer_user_id=access.user.user_id,
            reason=explanation, expected_version=expected_version, updated_at=now)
        record_audit(access.conn, now, access.user.user_id,
                     "security.review_approved" if verdict == "approved"
                     else "security.review_rejected", security_review_id, project_id=project_id,
                     target_version=updated.version)
        return updated

    def set_grant(self, access: Access, *, project_id: str, user_id: str, active: bool,
                  expected_version: int | None = None) -> SecurityGrant:
        self._member(access, project_id)
        if access.store.get_project(project_id).owner_id != access.user.user_id:
            # The reviewer capability must not be self-issued, so delegation stays with the
            # person who owns the project.
            raise Forbidden("Only the project owner can delegate security approvals")
        target = access.store.get_member(project_id, user_id)
        if target.status != "active":
            raise InvalidState("A security approver must be an active project member")
        grant = access.store.upsert_security_grant(
            project_id, user_id, capability=CAPABILITY, granted_by=access.user.user_id,
            active=active, created_at=self.clock(), expected_version=expected_version)
        record_audit(access.conn, grant.updated_at, access.user.user_id,
                     "security.grant_set" if active else "security.grant_revoked", user_id,
                     project_id=project_id, target_version=grant.version)
        return grant

    # --- delivery decisions -----------------------------------------------------

    def confirm(self, access: Access, *, project_id: str, attempt_id: str, decision: str,
                expected_version: int,
                reason: str = "") -> tuple[Attempt, OwnerReview]:
        """The responsible human's verdict on one delivered result; old results stay intact."""
        verdict = _choice(decision, CONFIRM_DECISIONS, "decision")
        explanation = self._reason(reason)
        if verdict == "changes_requested" and not explanation.strip():
            raise ValidationError("A returned delivery must name the acceptance item it missed")
        attempt, item = self._delivery(access, project_id, attempt_id)
        existing = access.store.get_owner_review(attempt_id)
        if existing is not None:
            if existing.decision != verdict:
                raise Conflict("This delivery already carries a different owner decision")
            return attempt, existing
        if attempt.status != "submitted":
            raise InvalidState("Only a reported result can be confirmed")
        if item.status != "awaiting_owner_review":
            raise InvalidState("Work item is not awaiting owner review")
        if self.gate_problems(access.store, attempt, now=self.clock()):
            raise InvalidState("Security gate is still blocking this delivery")
        _version(attempt.version, expected_version)
        now = self.clock()
        updated = access.store.advance_attempt(
            attempt_id, status=ATTEMPT_ON_CONFIRMATION[verdict], expected_version=expected_version,
            updated_at=now)
        access.store.advance_work_item(item.work_item_id, status=ITEM_ON_CONFIRMATION[verdict],
                                       expected_version=item.version, updated_at=now)
        review = access.store.add_owner_review(attempt_id, reviewer_user_id=access.user.user_id,
                                               decision=verdict, result_hash=updated.result_hash,
                                               reason=explanation, created_at=now)
        record_audit(access.conn, now, access.user.user_id,
                     "attempt.confirmed" if verdict == "confirmed"
                     else "attempt.changes_requested", attempt_id, project_id=project_id,
                     target_version=updated.version)
        return updated, review

    def owner_review(self, access: Access, *, project_id: str,
                     attempt_id: str) -> OwnerReview | None:
        self._delivery(access, project_id, attempt_id)
        return access.store.get_owner_review(attempt_id)

    def accept(self, access: Access, *, project_id: str, work_item_id: str, decision: str,
               expected_version: int, reason: str = "") -> WorkItem:
        """Business verdict on a confirmed delivery; only accepted output feeds downstream.

        The verdict is kept per attempt as immutable evidence, because a returned delivery has
        to tell the next attempt which acceptance item missed, not merely that it changed.
        """
        verdict = _choice(decision, ACCEPT_DECISIONS, "decision")
        explanation = self._reason(reason)
        if verdict == "changes_requested" and not explanation.strip():
            raise ValidationError("A returned delivery must name the acceptance item it missed")
        item = self._item(access, project_id, work_item_id)
        if item.status == verdict:
            return item
        if item.status != "submitted":
            raise InvalidState("Work item is not awaiting acceptance")
        _version(item.version, expected_version)
        now = self.clock()
        delivered = max((entry for entry in access.store.list_attempts(project_id, work_item_id)
                         if entry.status == "accepted"), key=lambda entry: entry.number,
                        default=None)
        updated = access.store.advance_work_item(work_item_id, status=verdict,
                                                 expected_version=expected_version,
                                                 updated_at=now)
        if delivered is not None and delivered.result_hash is not None:
            access.store.add_acceptance_review(
                delivered.attempt_id, reviewer_user_id=access.user.user_id, decision=verdict,
                result_hash=delivered.result_hash, reason=explanation, created_at=now)
        record_audit(access.conn, now, access.user.user_id, f"work_item.{verdict}",
                     work_item_id, project_id=project_id, target_version=updated.version)
        return updated

    def acceptance_review(self, access: Access, *, project_id: str,
                          attempt_id: str) -> AcceptanceReview | None:
        self._delivery(access, project_id, attempt_id)
        return access.store.get_acceptance_review(attempt_id)

    def rework(self, access: Access, *, project_id: str, work_item_id: str,
               expected_version: int) -> WorkItem:
        """Put a returned or failed work item back in the queue; every result stays as evidence.

        Only a manager re-opens execution rights, and only while no attempt of the item is
        still open, so a rework can never run two attempts of one item side by side. A failed
        attempt is retried on unchanged inputs, so its digest and any security pass for it
        stay valid; only a human return adds a note and therefore a new input digest.
        """
        member = self._member(access, project_id)
        if member.access_role not in MANAGER_ROLES:
            raise Forbidden("Only a project manager can re-queue a returned work item")
        item = access.store.get_work_item(project_id, work_item_id)
        if item.status not in REWORKABLE_STATUSES:
            raise InvalidState("Only a returned or failed work item can be re-queued")
        _version(item.version, expected_version)
        if access.store.open_attempt(work_item_id) is not None:
            raise InvalidState("An attempt of this work item is still open")
        now = self.clock()
        updated = access.store.advance_work_item(work_item_id, status="ready",
                                                 expected_version=expected_version,
                                                 updated_at=now)
        record_audit(access.conn, now, access.user.user_id, "work_item.requeued", work_item_id,
                     project_id=project_id, target_version=updated.version)
        return updated

    def cancel(self, access: Access, *, project_id: str, work_item_id: str, expected_version: int,
               reason: str = "") -> WorkItem:
        """Withdraw a work item: take back execution rights, keep every side effect as evidence.

        An in-flight attempt is moved to ``reconciliation_required`` under a revoked control
        epoch rather than to ``cancelled``: no stop confirmation was received, so the record
        must not claim the outside world was untouched, and its reservation stays charged.
        """
        explanation = self._reason(reason)
        item = self._item(access, project_id, work_item_id)
        if item.status == "cancelled":
            return item
        if item.status == "accepted":
            raise InvalidState("Accepted work cannot be cancelled")
        _version(item.version, expected_version)
        updated, _in_flight = self._withdraw(access, project_id, item, explanation=explanation,
                                             now=self.clock())
        return updated

    def _withdraw(self, access: Access, project_id: str, item: WorkItem, *, explanation: str,
                  now: float) -> tuple[WorkItem, int]:
        """Close one item and its unfinished attempts; count the ones still running."""
        in_flight = 0
        for attempt in access.store.list_attempts(project_id, item.work_item_id):
            if attempt.status in ATTEMPT_TERMINAL_STATUSES:
                continue
            if attempt.status == "submitted":
                current = access.store.advance_attempt(attempt.attempt_id, status="cancelled",
                                                       expected_version=attempt.version,
                                                       updated_at=now)
            else:
                current = access.store.advance_attempt(attempt.attempt_id,
                                                       status="reconciliation_required",
                                                       expected_version=attempt.version,
                                                       updated_at=now)
                current = access.store.revoke_attempt_execution_right(
                    current.attempt_id, expected_version=current.version, updated_at=now)
                in_flight += 1
                # Advisory only: the worker learns to stop on its next pull, and a lost wake-up
                # cannot advance anything because the attempt's rights are already revoked.
                access.store.enqueue_event(project_id, TOPIC_ATTEMPT_REVOKED, {
                    "attempt_id": current.attempt_id, "work_item_id": item.work_item_id,
                    "instance_id": current.instance_id, "input_hash": current.input_hash,
                    "control_epoch": current.control_epoch}, now)
            closure = ("delivered result withdrawn by human" if current.status == "cancelled"
                       else "execution right revoked by human")
            access.store.record_attempt_report(
                attempt.attempt_id, stage="cancel", lease_epoch=current.lease_epoch,
                control_epoch=current.control_epoch, input_hash=current.input_hash,
                payload=json.dumps({"reason": explanation, "actor_user_id": access.user.user_id},
                                   ensure_ascii=False, sort_keys=True), accepted=True,
                reason=closure, created_at=now)
        updated = access.store.advance_work_item(item.work_item_id, status="cancelled",
                                                 expected_version=item.version, updated_at=now)
        record_audit(access.conn, now, access.user.user_id, "work_item.cancelled",
                     item.work_item_id, project_id=project_id, target_version=updated.version)
        return updated, in_flight

    def reconcile_attempt(self, access: Access, *, project_id: str, attempt_id: str,
                          outcome: str, expected_version: int, reason: str = "") -> Attempt:
        """Close an attempt a withdrawal or an expired lease left unconfirmed, on what a human checked.

        Revoking execution rights deliberately refuses any further report from the worker, so
        ``reconciliation_required`` can only be settled by the person who looked: ``cancelled``
        gives a given-up-on item's reserved share back, ``failed`` keeps it charged as the cost of
        a run that did touch the outside world. The verdict also decides where the work item goes.
        One the human cancelled comes back only on ``failed``; one still holding a round the
        service reclaimed moves to a manager to re-queue on ``failed``, or back to the queue to
        wait for another machine on ``cancelled``.
        """
        choice = _choice(outcome, ("cancelled", "failed"), "outcome")
        explanation = self._reason(reason)
        if not explanation.strip():
            raise ValidationError("Reconciling an attempt must record what was checked")
        member = self._member(access, project_id)
        attempt = access.store.get_attempt(project_id, attempt_id)
        if (member.access_role not in MANAGER_ROLES
                and attempt.assignee_user_id != access.user.user_id):
            raise Forbidden("Only a project manager or the responsible member can reconcile")
        if attempt.status in ATTEMPT_TERMINAL_STATUSES:
            raise InvalidState("Attempt is already settled")
        _version(attempt.version, expected_version)
        now = self.clock()
        item = access.store.get_work_item(project_id, attempt.work_item_id)
        updated = access.store.advance_attempt(attempt.attempt_id, status=choice,
                                              expected_version=attempt.version, updated_at=now)
        access.store.record_attempt_report(
            attempt.attempt_id, stage="cancel", lease_epoch=updated.lease_epoch,
            control_epoch=updated.control_epoch, input_hash=updated.input_hash,
            payload=json.dumps({"reason": explanation, "actor_user_id": access.user.user_id,
                                "outcome": choice}, ensure_ascii=False, sort_keys=True),
            accepted=True, reason="reconciled by human", created_at=now)
        if (choice == "cancelled" and item.status == "cancelled"
                and updated.number == 1 and item.estimated_units):
            # The share only goes back when the item itself was given up on. A round the service
            # reclaimed after the lease ran out leaves the item standing, and the next attempt is
            # expected to spend that same reservation.
            self.budgets.release(access.store, project_id=project_id,
                                 requirement_id=attempt.requirement_id,
                                 attempt_id=attempt.attempt_id,
                                 reserved_units=item.estimated_units, created_at=now)
        target = None
        if item.status == "cancelled" and choice == "failed":
            # The withdrawal wrote ``cancelled`` as the human's abandonment; a run that left
            # traces behind was not abandoned, so the item returns to the re-queueable set.
            target = "failed"
        elif item.status == "running":
            # A lease ran out under this item and the service took the reporting right back, so
            # the round is lost rather than given up on: ``failed`` puts it in front of a manager
            # to re-queue, ``cancelled`` hands it back to the queue to wait for a machine.
            target = "failed" if choice == "failed" else "waiting_agent"
        if target is not None:
            moved = access.store.advance_work_item(item.work_item_id, status=target,
                                                   expected_version=item.version, updated_at=now)
            record_audit(access.conn, now, access.user.user_id, f"work_item.{target}",
                         item.work_item_id, project_id=project_id, target_version=moved.version)
        record_audit(access.conn, now, access.user.user_id, f"attempt.reconciled.{choice}",
                     attempt.attempt_id, project_id=project_id, target_version=updated.version)
        return updated

    def cancel_requirement(self, access: Access, *, project_id: str, requirement_id: str,
                           expected_version: int, reason: str = "") -> Requirement:
        """Stop a whole requirement: withdraw every unfinished item and return what was never spent.

        The requester or a project manager cancels. Delivered evidence stays as it is: accepted
        work, confirmations and settlements are not rewritten, because abandoning a requirement
        does not undo the rounds that already happened. If any execution is still unconfirmed,
        the requirement parks in ``cancelling`` instead of claiming a clean stop; calling this
        again after that attempt reconciles moves it on to ``cancelled``.
        """
        explanation = self._reason(reason)
        member = self._member(access, project_id)
        requirement = access.store.get_requirement(project_id, requirement_id)
        if (member.access_role not in MANAGER_ROLES
                and requirement.requester_id != access.user.user_id):
            raise Forbidden("Only the requester or a project manager can cancel a requirement")
        if requirement.status == "cancelled":
            return requirement
        if requirement.status not in REQUIREMENT_TRANSITIONS["cancelled"]:
            raise InvalidState("This requirement cannot be cancelled")
        _version(requirement.version, expected_version)
        now = self.clock()
        plan = access.store.published_plan(project_id, requirement_id)
        in_flight = 0
        unspent_units = 0
        if plan is not None:
            for item in access.store.list_work_items(project_id, plan.plan_id):
                if item.status in ("accepted", "cancelled"):
                    if not access.store.list_attempts(project_id, item.work_item_id):
                        unspent_units += item.estimated_units
                    continue
                _item, running = self._withdraw(access, project_id, item,
                                                explanation=explanation, now=now)
                in_flight += running
                if not access.store.list_attempts(project_id, item.work_item_id):
                    unspent_units += item.estimated_units
            self.budgets.release_plan(access.store, project_id=project_id,
                                      requirement_id=requirement_id, plan_id=plan.plan_id,
                                      amount=unspent_units, created_at=now)
        status = "cancelling" if in_flight else "cancelled"
        updated = access.store.set_requirement_status(project_id, requirement_id, status=status,
                                                      expected_version=requirement.version,
                                                      updated_at=now)
        record_audit(access.conn, now, access.user.user_id, f"requirement.{status}",
                     requirement_id, project_id=project_id, target_version=updated.version)
        return updated

    def pause_requirement(self, access: Access, *, project_id: str, requirement_id: str,
                          expected_version: int, reason: str = "") -> Requirement:
        """Stop this requirement from starting anything new; leave running work alone.

        A pause is not a cancellation. Execution rights already handed out stay handed out,
        because taking them back would have to claim something about an outside world nobody
        checked. What stops is the next attempt: ``DispatchService`` refuses to create one while
        the requirement is paused, and the scheduler host skips it the same way. The reason is
        required because a paused requirement looks exactly like a stalled one to the people
        waiting on it.
        """
        explanation = self._reason(reason)
        if not explanation.strip():
            raise ValidationError("Pausing a requirement must say why work is stopping")
        member = self._member(access, project_id)
        requirement = access.store.get_requirement(project_id, requirement_id)
        if (member.access_role not in MANAGER_ROLES
                and requirement.requester_id != access.user.user_id):
            raise Forbidden("Only the requester or a project manager can pause a requirement")
        if requirement.status == "paused":
            return requirement
        if requirement.status not in PAUSABLE_STATUSES:
            raise InvalidState("This requirement is not delivering work that can be paused")
        _version(requirement.version, expected_version)
        now = self.clock()
        updated = access.store.set_requirement_status(project_id, requirement_id, status="paused",
                                                      expected_version=requirement.version,
                                                      updated_at=now)
        access.store.add_requirement_pause(project_id, requirement_id,
                                           actor_user_id=access.user.user_id, action="paused",
                                           reason=explanation, created_at=now)
        record_audit(access.conn, now, access.user.user_id, "requirement.paused", requirement_id,
                     project_id=project_id, target_version=updated.version)
        return updated

    def resume_requirement(self, access: Access, *, project_id: str, requirement_id: str,
                           expected_version: int) -> Requirement:
        """Let a paused requirement start work again, at whatever its items now justify.

        The target is re-derived instead of restored from a remembered "previous state": a person
        may have confirmed, accepted or returned deliveries while the requirement was paused, and
        the work items are the only record of what is true now.
        """
        member = self._member(access, project_id)
        requirement = access.store.get_requirement(project_id, requirement_id)
        if (member.access_role not in MANAGER_ROLES
                and requirement.requester_id != access.user.user_id):
            raise Forbidden("Only the requester or a project manager can resume a requirement")
        if requirement.status != "paused":
            raise InvalidState("Requirement is not paused")
        _version(requirement.version, expected_version)
        target = self.requirement_target(access.store, project_id, requirement_id)
        now = self.clock()
        updated = access.store.set_requirement_status(project_id, requirement_id, status=target,
                                                      expected_version=requirement.version,
                                                      updated_at=now)
        access.store.add_requirement_pause(project_id, requirement_id,
                                           actor_user_id=access.user.user_id, action="resumed",
                                           reason="", created_at=now)
        record_audit(access.conn, now, access.user.user_id, "requirement.resumed", requirement_id,
                     project_id=project_id, target_version=updated.version)
        return updated

    def delivery_bundle(self, store: CollaborationStore, project_id: str,
                        requirement_id: str) -> dict:
        """What the requester is asked to sign for: each item with the attempt and files behind it.

        The digest covers every accepted attempt, its result and its artifact hashes, so a
        signature is bound to one concrete bundle instead of to the word "done".
        """
        plan = store.published_plan(project_id, requirement_id)
        if plan is None:
            raise InvalidState("Requirement has no published plan")
        entries = []
        for item in store.list_work_items(project_id, plan.plan_id):
            delivered = max((entry for entry in store.list_attempts(project_id, item.work_item_id)
                             if entry.status == "accepted"), key=lambda entry: entry.number,
                            default=None)
            entries.append({
                "key": item.key, "work_item_id": item.work_item_id, "title": item.title,
                "status": item.status,
                "acceptance": [{"item_id": item_id, "text": text} for item_id, text in
                               store.list_work_item_acceptance(item.work_item_id)],
                "attempt_id": None if delivered is None else delivered.attempt_id,
                "attempt_number": None if delivered is None else delivered.number,
                "result_hash": None if delivered is None else delivered.result_hash,
                "artifacts": [] if delivered is None else sorted(
                    [{"name": artifact.name, "sha256": artifact.sha256,
                      "size_bytes": artifact.size_bytes}
                     for artifact in store.list_artifacts(project_id, delivered.attempt_id)],
                    key=lambda entry: (entry["name"], entry["sha256"])),
            })
        entries.sort(key=lambda entry: entry["key"])
        return {"plan_id": plan.plan_id, "revision_id": plan.revision_id,
                "evidence_hash": hashlib.sha256(
                    _canonical({"items": entries}).encode("utf-8")).hexdigest(),
                "items": entries}

    def _coverage(self, store: CollaborationStore, project_id: str, requirement_id: str,
                  bundle: dict) -> dict:
        """Which acceptance items the accepted deliveries cover, and which nobody covers.

        A published plan covers every acceptance item of its revision, so while a requirement
        really sits in ``integrating`` this list comes out fully covered — the value of reading it
        is that the person signing sees the mapping instead of taking it on faith. It starts
        showing gaps as soon as a covering delivery stops being accepted, which is what a return
        or a cancellation does, and that is what the requester should not have to notice by
        reading the original text against the bundle themselves.
        """
        plan = store.published_plan(project_id, requirement_id)
        covered: dict[str, list[str]] = {}
        for entry in bundle["items"]:
            if entry["status"] != "accepted":
                continue
            for item in entry["acceptance"]:
                covered.setdefault(item["item_id"], []).append(entry["key"])
        acceptance = [{"item_id": item.item_id, "text": item.text,
                       "covered_by": sorted(covered.get(item.item_id, []))}
                      for item in store.list_acceptance_items(project_id, requirement_id,
                                                              plan.revision_id)]
        return {"evidence_hash": bundle["evidence_hash"], "revision_id": plan.revision_id,
                "acceptance": acceptance,
                "uncovered": [entry["item_id"] for entry in acceptance if not entry["covered_by"]]}

    def integration_report(self, store: CollaborationStore, project_id: str,
                           requirement_id: str) -> dict:
        return self._coverage(store, project_id, requirement_id,
                              self.delivery_bundle(store, project_id, requirement_id))

    def requirement_target(self, store: CollaborationStore, project_id: str,
                           requirement_id: str) -> str:
        """What this requirement's own work items say right now.

        One rule serves both the person who asks from the panel and the host that asks on a
        timer, so an automatic round can never disagree with the manual one. ``delivery_bundle``
        refuses a requirement without a published plan, so only delivered work answers here.
        Everything accepted is not yet the requester's turn: it stops at ``integrating`` until a
        manager has put a signature against this exact bundle digest.
        """
        bundle = self.delivery_bundle(store, project_id, requirement_id)
        statuses = {entry["status"] for entry in bundle["items"]}
        if statuses == {"accepted"}:
            verdict = store.latest_requirement_integration(project_id, requirement_id)
            if (verdict is not None and verdict.decision == "passed"
                    and verdict.evidence_hash == bundle["evidence_hash"]):
                return "awaiting_acceptance"
            return "integrating"
        if statuses & set(REWORKABLE_STATUSES):
            return "rework"
        if self._pending_integration(store, project_id, bundle["plan_id"]) is not None:
            return "integrating"
        return "executing"

    def pending_integration(self, store: CollaborationStore, project_id: str,
                            requirement_id: str) -> WorkItem | None:
        """The merge round this version still owes, read without needing a published plan.

        A manager is refused a ``passed`` while this row is open, so the check view has to be able
        to name the round standing in the way instead of leaving a person to click into a Conflict.
        """
        plan = store.published_plan(project_id, requirement_id)
        if plan is None:
            return None
        return self._pending_integration(store, project_id, plan.plan_id)

    def _pending_integration(self, store: CollaborationStore, project_id: str,
                             plan_id: str) -> WorkItem | None:
        """The merge-and-test round, while it still has work to deliver.

        A requirement that has one open integration item is not awaiting a signature: somebody is
        still putting this version together. It is also not back in ordinary execution — the
        business deliveries are all accepted, and calling that state "executing" would hide which
        round is actually running.
        """
        return next((entry for entry in store.list_work_items(project_id, plan_id)
                     if entry.kind == "integration" and entry.status != "accepted"), None)

    def integration_item(self, access: Access, *, project_id: str, requirement_id: str,
                         binding_id: str, estimated_units: int,
                         expected_version: int) -> WorkItem:
        """A manager points the merge-and-test round at one binding this project authorized.

        The plan a human approved stays as it was: this item is added beside it, depends on every
        accepted delivery it has to put together, and reserves its own share of the budget. The
        assignee is not chosen freely either — it is the member that binding belongs to, because
        the binding is what carries the duty, the persona and the tool policy. Repeating the request
        returns the item that already exists instead of opening a second merge round.
        """
        member = self._member(access, project_id)
        if member.access_role not in MANAGER_ROLES:
            raise Forbidden("Only a project manager can open the integration round")
        if type(estimated_units) is not int or not 1 <= estimated_units <= 10 ** 9:
            raise ValidationError("Invalid estimated_units")
        requirement = access.store.get_requirement(project_id, requirement_id)
        plan = access.store.published_plan(project_id, requirement_id)
        if plan is None:
            raise InvalidState("Requirement has no published plan")
        items = access.store.list_work_items(project_id, plan.plan_id)
        existing = next((entry for entry in items if entry.kind == "integration"), None)
        if existing is not None:
            return existing
        if requirement.status != "integrating":
            raise InvalidState("The integration round opens once every work item is accepted")
        _version(requirement.version, expected_version)
        business = [entry for entry in items if entry.kind == "business"]
        if any(entry.status != "accepted" for entry in business):
            raise InvalidState("Every work item must be accepted before the merge round opens")
        binding = access.store.get_binding(project_id, binding_id)
        if binding.status != "active":
            raise InvalidState("That execution identity is not active")
        if self.budgets.limit_problems(access.store, project_id=project_id,
                                       requirement_id=requirement_id, amount=estimated_units):
            raise Conflict("This requirement's budget has no room for the integration round")
        now = self.clock()
        item = access.store.add_work_item(
            project_id, plan.plan_id, kind="integration",
            raw={"key": "integration", "title": "集成这一版交付并跑测试",
                 "objective": "把已验收的交付合到一起，在独立目录跑一遍项目测试并回报结果",
                 "scope": "合并这些已验收工作项的产物、解决冲突、运行项目测试",
                 "non_goals": "不新增功能，不改需求原文，不替提出人签字",
                 "duty": binding.duty, "assignee_user_id": binding.member_user_id,
                 "binding_id": binding.binding_id, "estimated_units": estimated_units},
            depends_on_ids=[entry.work_item_id for entry in business], created_at=now)
        self.budgets.reserve_item(access.store, project_id=project_id,
                                  requirement_id=requirement_id, work_item_id=item.work_item_id,
                                  amount=estimated_units, created_at=now)
        record_audit(access.conn, now, access.user.user_id, "work_item.integration_created",
                     item.work_item_id, project_id=project_id, target_version=item.version)
        return item

    def advance_requirement(self, access: Access, *, project_id: str, requirement_id: str,
                            expected_version: int) -> Requirement:
        """Re-derive the requirement's own status from what its work items actually did."""
        self._member(access, project_id)
        requirement = access.store.get_requirement(project_id, requirement_id)
        _version(requirement.version, expected_version)
        if requirement.status not in DELIVERING_STATUSES:
            raise InvalidState("Requirement is not delivering work")
        target = self.requirement_target(access.store, project_id, requirement_id)
        if target == requirement.status:
            return requirement
        now = self.clock()
        updated = access.store.set_requirement_status(project_id, requirement_id, status=target,
                                                     expected_version=requirement.version,
                                                     updated_at=now)
        record_audit(access.conn, now, access.user.user_id, f"requirement.{target}",
                     requirement_id, project_id=project_id, target_version=updated.version)
        return updated

    def record_integration(self, access: Access, *, project_id: str, requirement_id: str,
                           decision: str, expected_version: int, reason: str = "",
                           returned_items: list[str] | tuple[str, ...] = ()) -> Requirement:
        """A manager says whether this bundle is fit to be put in front of the requester.

        The verdict is bound to the bundle digest it was made against, so an earlier sign-off
        cannot carry a later round: new accepted work moves the digest and the requirement comes
        back here. A return re-opens the accepted items it names — all of them when it names
        none — the same way the requester's own return does, because the published plan already
        carries the next round and the original text is not rewritten. Which acceptance item the
        bundle covers is a fact the plan already guarantees here — a published plan covers every
        one of them and this step is only reached once all of them are accepted — so the check is
        shown by ``integration_report`` rather than enforced by this write.
        """
        verdict = _choice(decision, INTEGRATION_DECISIONS, "decision")
        explanation = self._reason(reason)
        if verdict == "returned" and not explanation.strip():
            raise ValidationError("A returned integration must say what the delivery missed")
        if verdict != "returned" and returned_items:
            raise ValidationError("Only a return can name the work items it refuses")
        member = self._member(access, project_id)
        if member.access_role not in MANAGER_ROLES:
            raise Forbidden("Only a project manager can record the integration check")
        requirement = access.store.get_requirement(project_id, requirement_id)
        bundle = self.delivery_bundle(access.store, project_id, requirement_id)
        refused = self._refused(bundle, returned_items) if verdict == "returned" else []
        if verdict == "passed":
            # 说「可以签字」之前，那一版交付得先真的被合起来测过：核对不能替一次没跑的合并放行。
            pending = self._pending_integration(access.store, project_id, bundle["plan_id"])
            if pending is not None:
                raise InvalidState("The integration round has not delivered this version yet")
        if self._integrated_already(access, requirement, verdict, bundle["evidence_hash"], refused):
            return requirement
        if requirement.status != "integrating":
            raise InvalidState("Requirement is not awaiting an integration check")
        _version(requirement.version, expected_version)
        now = self.clock()
        if verdict == "returned":
            self._reopen(access, project_id=project_id, refused=refused, bundle=bundle, now=now)
        target = "awaiting_acceptance" if verdict == "passed" else "rework"
        updated = access.store.set_requirement_status(project_id, requirement_id, status=target,
                                                      expected_version=requirement.version,
                                                      updated_at=now)
        access.store.add_requirement_integration(
            project_id, requirement_id, actor_user_id=access.user.user_id, decision=verdict,
            evidence_hash=bundle["evidence_hash"], reason=explanation, created_at=now,
            returned_items=refused)
        record_audit(access.conn, now, access.user.user_id, f"integration.{verdict}",
                     requirement_id, project_id=project_id, target_version=updated.version)
        return updated

    def _refused(self, bundle: dict, returned_items) -> list[str]:
        """The work items a return names, checked against the delivery it is refusing.

        Naming something that is not part of this bundle is refused instead of being read as
        "everything": the next attempt takes these rows as its instructions, so a key that matches
        no work item would sign a statement about work nobody is pointing at.
        """
        keys = self._keys(returned_items)
        missing = [key for key in keys if key not in {entry["key"] for entry in bundle["items"]}]
        if missing:
            raise ValidationError("Only a work item of this delivery can be returned: "
                                  + ", ".join(sorted(missing)))
        return keys

    @staticmethod
    def _keys(returned_items) -> list[str]:
        keys = list(returned_items)
        if len(keys) > 64:
            raise ValidationError("Too many returned work items")
        for key in keys:
            if not isinstance(key, str) or not key.strip() or len(key) > 128:
                raise ValidationError("Invalid returned work item")
        if len({key for key in keys}) != len(keys):
            raise ValidationError("Returned work items must not repeat")
        return sorted(keys)

    def _reopen(self, access: Access, *, project_id: str, refused: list[str], bundle: dict,
                now: float) -> None:
        """Send back exactly the deliveries this return names, and only those still accepted.

        An empty ``refused`` list is the older, wider meaning: the whole bundle went back.
        """
        wanted = set(refused)
        for entry in bundle["items"]:
            if wanted and entry["key"] not in wanted:
                continue
            if entry["status"] != "accepted":
                continue
            stored = access.store.get_work_item(project_id, entry["work_item_id"])
            reopened = access.store.advance_work_item(entry["work_item_id"],
                                                      status="changes_requested",
                                                      expected_version=stored.version,
                                                      updated_at=now)
            record_audit(access.conn, now, access.user.user_id, "work_item.changes_requested",
                         entry["work_item_id"], project_id=project_id,
                         target_version=reopened.version)

    def _integrated_already(self, access: Access, requirement: Requirement, verdict: str,
                            evidence_hash: str, refused: list[str]) -> bool:
        """Is this the verdict this bundle already got? Then the retry writes nothing: a second
        click must not reopen work that is already back in the queue."""
        if requirement.status != ("awaiting_acceptance" if verdict == "passed" else "rework"):
            return False
        latest = access.store.latest_requirement_integration(requirement.project_id,
                                                             requirement.requirement_id)
        return (latest is not None and latest.decision == verdict
                and latest.evidence_hash == evidence_hash
                and latest.returned_items == tuple(refused))

    def accept_requirement(self, access: Access, *, project_id: str, requirement_id: str,
                           decision: str, expected_version: int, reason: str = "",
                           returned_items: list[str] | tuple[str, ...] = ()) -> Requirement:
        """The requester signs for the whole requirement; nobody else can speak for them.

        A return re-opens work items instead of refusing, because the published plan already
        carries the second round: no new plan is made and the original text is not rewritten. It
        names the items it refuses, and names none when the whole delivery goes back. Their
        ``changes_requested`` status is what puts a re-queue to-do in front of a manager, and the
        verdict row keeps which bundle was refused and which items of it. A per-attempt acceptance
        row is deliberately not written here: one attempt holds one business verdict, and a
        requirement-level verdict spans the attempts it names, so ``requirement_acceptance`` is the
        record the next attempt reads.
        """
        verdict = _choice(decision, ACCEPT_DECISIONS, "decision")
        explanation = self._reason(reason)
        if verdict == "changes_requested" and not explanation.strip():
            raise ValidationError("A returned requirement must say what the delivery missed")
        if verdict != "changes_requested" and returned_items:
            raise ValidationError("Only a return can name the work items it refuses")
        self._member(access, project_id)
        requirement = access.store.get_requirement(project_id, requirement_id)
        if requirement.requester_id != access.user.user_id:
            raise Forbidden("Only the requester can accept or return a requirement")
        if requirement.status == "completed":
            if verdict == "accepted":
                return requirement
            raise InvalidState("A completed requirement cannot be returned")
        if requirement.status != "awaiting_acceptance":
            if self._returned_already(access, requirement, verdict, returned_items):
                return requirement
            raise InvalidState("Requirement is not awaiting acceptance")
        _version(requirement.version, expected_version)
        bundle = self.delivery_bundle(access.store, project_id, requirement_id)
        if any(entry["status"] != "accepted" for entry in bundle["items"]):
            raise InvalidState("Every work item must be accepted before the requirement is")
        now = self.clock()
        target = "rework" if verdict == "changes_requested" else "completed"
        refused = (self._refused(bundle, returned_items)
                   if verdict == "changes_requested" else [])
        if verdict == "changes_requested":
            self._reopen(access, project_id=project_id, refused=refused, bundle=bundle, now=now)
        updated = access.store.set_requirement_status(project_id, requirement_id, status=target,
                                                      expected_version=requirement.version,
                                                      updated_at=now)
        access.store.add_requirement_acceptance(
            project_id, requirement_id, reviewer_user_id=access.user.user_id, decision=verdict,
            evidence_hash=bundle["evidence_hash"], reason=explanation, created_at=now,
            returned_items=refused)
        record_audit(access.conn, now, access.user.user_id,
                     "requirement.returned" if verdict == "changes_requested"
                     else "requirement.completed", requirement_id,
                     project_id=project_id, target_version=updated.version)
        return updated

    def _returned_already(self, access: Access, requirement: Requirement, verdict: str,
                          returned_items) -> bool:
        """Is this the very return the requester already signed? Then the second click changes
        nothing, so a retried request cannot reopen work that is already back in the queue.
        Naming other items this time is a different statement, and is not a replay."""
        if verdict != "changes_requested" or requirement.status != "rework":
            return False
        keys = self._keys(returned_items)
        rows = access.store.list_requirement_acceptances(requirement.project_id,
                                                         requirement.requirement_id)
        return bool(rows) and rows[-1].decision == "changes_requested" \
            and rows[-1].returned_items == tuple(keys)
