"""Human decisions over delivered work: confirmation, security gate and withdrawal.

Every method runs inside the caller's transaction through ``access.store`` and
``access.conn``, so a decision, its audit row and any budget movement commit together.
"""
from __future__ import annotations

import hashlib
import json
import time

from .auth import Access, Forbidden, record_audit
from .dispatch import TOPIC_ATTEMPT_REVOKED
from .models import (
    ATTEMPT_TERMINAL_STATUSES, AgentProjectBinding, Attempt, Conflict, InvalidState, NotFound,
    OwnerReview, ProjectMember, SecurityGrant, SecurityReview, ValidationError, WorkItem,
)
from .store import CollaborationStore, _choice, _text, _version

CAPABILITY = "security:approve"
CONFIRM_DECISIONS = ("confirmed", "changes_requested")
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
                 review_ttl: float = DEFAULT_REVIEW_TTL):
        if type(review_ttl) not in (int, float) or not 60 <= review_ttl <= 604800:
            raise ValidationError("Invalid security review lifetime")
        self.clock = clock
        self.passable_rules = tuple(passable_rules)
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
        """Business verdict on a confirmed delivery; only accepted output feeds downstream."""
        verdict = _choice(decision, ("accepted", "changes_requested"), "decision")
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
        updated = access.store.advance_work_item(work_item_id, status=verdict,
                                                 expected_version=expected_version,
                                                 updated_at=now)
        record_audit(access.conn, now, access.user.user_id, f"work_item.{verdict}",
                     work_item_id, project_id=project_id, target_version=updated.version)
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
        now = self.clock()
        revoked = None
        for attempt in access.store.list_attempts(project_id, work_item_id):
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
                revoked = current
            closure = ("delivered result withdrawn by human" if current.status == "cancelled"
                       else "execution right revoked by human")
            access.store.record_attempt_report(
                attempt.attempt_id, stage="cancel", lease_epoch=current.lease_epoch,
                control_epoch=current.control_epoch, input_hash=current.input_hash,
                payload=json.dumps({"reason": explanation, "actor_user_id": access.user.user_id},
                                   ensure_ascii=False, sort_keys=True), accepted=True,
                reason=closure, created_at=now)
        updated = access.store.advance_work_item(work_item_id, status="cancelled",
                                                 expected_version=expected_version,
                                                 updated_at=now)
        if revoked is not None:
            # Advisory only: the worker learns to stop on its next pull, and a lost wake-up
            # cannot advance anything because the attempt's rights are already revoked.
            access.store.enqueue_event(project_id, TOPIC_ATTEMPT_REVOKED, {
                "attempt_id": revoked.attempt_id, "work_item_id": work_item_id,
                "instance_id": revoked.instance_id, "input_hash": revoked.input_hash,
                "control_epoch": revoked.control_epoch}, now)
        record_audit(access.conn, now, access.user.user_id, "work_item.cancelled", work_item_id,
                     project_id=project_id, target_version=updated.version)
        return updated
