"""Restricted requirement drafting, immutable revisions and versioned human review."""
from __future__ import annotations

import difflib
import hashlib
import time
from dataclasses import asdict

from .auth import Access, Forbidden, record_audit
from .models import (
    Conflict, InvalidState, NotFound, Requirement, RequirementMessage, RequirementReview,
    RequirementRevision, ValidationError,
)

MAX_CONTENT_BYTES = 8 * 1024 * 1024
MAX_MESSAGE_CHARS = 8192
MAX_REASON_CHARS = 2048


def content_digest(content: str, *, limit: int = MAX_CONTENT_BYTES) -> tuple[str, int]:
    if not isinstance(content, str) or not content.strip():
        raise ValidationError("Content must be a non-empty string")
    try:
        data = content.encode("utf-8")
    except UnicodeEncodeError:
        raise ValidationError("Content must be valid Unicode") from None
    if len(data) > limit:
        raise ValidationError("Content exceeds the maximum revision size")
    return hashlib.sha256(data).hexdigest(), len(data)


class RequirementService:
    """Drafting, revisions and reviews run inside the caller's transaction.

    The clarifier seam is a callable ``(requirement, history, question) -> str | None``;
    the default build wires none, so clarification stays offline until a model is
    explicitly connected by a later phase.
    """

    def __init__(self, *, max_content_bytes: int = MAX_CONTENT_BYTES, clock=time.time):
        if type(max_content_bytes) is not int or not 1024 <= max_content_bytes <= 64 * 1024 * 1024:
            raise ValidationError("Invalid maximum content size")
        self.max_content_bytes = max_content_bytes
        self.clock = clock
        self.clarifier = None

    @staticmethod
    def _active_member(access: Access, project_id: str) -> None:
        member = access.store.get_member(project_id, access.user.user_id)
        if member.status != "active":
            raise NotFound("Resource not found")

    @staticmethod
    def _visible(requirement: Requirement, user_id: str) -> None:
        if requirement.status == "approved" or requirement.requester_id == user_id:
            return
        if requirement.status == "in_review" and requirement.reviewer_id == user_id:
            return
        raise NotFound("Resource not found")

    @staticmethod
    def _requester_only(requirement: Requirement, user_id: str) -> None:
        if requirement.requester_id == user_id:
            return
        if requirement.status == "approved":
            raise Forbidden("Only the requester can change this requirement")
        raise NotFound("Resource not found")

    def _get_visible(self, access: Access, project_id: str, requirement_id: str) -> Requirement:
        self._active_member(access, project_id)
        requirement = access.store.get_requirement(project_id, requirement_id)
        self._visible(requirement, access.user.user_id)
        return requirement

    def create_requirement(self, access: Access, *, project_id: str, title: str,
                           content: str | None = None) -> Requirement:
        self._active_member(access, project_id)
        now = self.clock()
        requirement = access.store.create_requirement(project_id, title, access.user.user_id, now)
        record_audit(access.conn, now, access.user.user_id, "requirement.created",
                     requirement.requirement_id, project_id=project_id,
                     target_version=requirement.version)
        if content is not None:
            requirement, _ = self._add_revision(access, requirement, content,
                                                expected_version=requirement.version)
        return requirement

    def _add_revision(self, access: Access, requirement: Requirement, content: str,
                      expected_version: int) -> tuple[Requirement, RequirementRevision]:
        content_hash, content_size = content_digest(content, limit=self.max_content_bytes)
        now = self.clock()
        updated, revision = access.store.add_revision(
            requirement.project_id, requirement.requirement_id, content=content,
            content_hash=content_hash, content_size=content_size, author_id=access.user.user_id,
            expected_version=expected_version, created_at=now)
        record_audit(access.conn, now, access.user.user_id, "requirement.revision_saved",
                     revision.revision_id, project_id=requirement.project_id,
                     target_version=updated.version)
        return updated, revision

    def list_requirements(self, access: Access, *, project_id: str) -> list[Requirement]:
        self._active_member(access, project_id)
        return access.store.list_requirements(project_id, access.user.user_id)

    def get_requirement(self, access: Access, *, project_id: str, requirement_id: str) -> Requirement:
        return self._get_visible(access, project_id, requirement_id)

    def save_revision(self, access: Access, *, project_id: str, requirement_id: str, content: str,
                      expected_version: int) -> tuple[Requirement, RequirementRevision]:
        self._active_member(access, project_id)
        requirement = access.store.get_requirement(project_id, requirement_id)
        self._requester_only(requirement, access.user.user_id)
        return self._add_revision(access, requirement, content, expected_version=expected_version)

    def list_revisions(self, access: Access, *, project_id: str,
                       requirement_id: str) -> list[RequirementRevision]:
        self._get_visible(access, project_id, requirement_id)
        return access.store.list_revisions(project_id, requirement_id)

    def read_revision(self, access: Access, *, project_id: str, requirement_id: str,
                      revision_id: str) -> tuple[RequirementRevision, str]:
        self._get_visible(access, project_id, requirement_id)
        revision = access.store.get_revision(project_id, requirement_id, revision_id)
        content = access.store.get_revision_content(project_id, requirement_id, revision_id)
        return revision, content

    def revision_diff(self, access: Access, *, project_id: str, requirement_id: str,
                      from_revision_id: str, to_revision_id: str) -> dict:
        self._get_visible(access, project_id, requirement_id)
        before = access.store.get_revision_content(project_id, requirement_id, from_revision_id)
        after = access.store.get_revision_content(project_id, requirement_id, to_revision_id)
        diff = "\n".join(difflib.unified_diff(before.splitlines(), after.splitlines(),
                                              fromfile=from_revision_id, tofile=to_revision_id,
                                              lineterm=""))
        return {"requirement_id": requirement_id, "from_revision_id": from_revision_id,
                "to_revision_id": to_revision_id, "diff": diff}

    @staticmethod
    def _message_content(content: str) -> None:
        if not isinstance(content, str) or not content.strip() or len(content) > MAX_MESSAGE_CHARS:
            raise ValidationError("Invalid message content")
        try:
            content.encode("utf-8")
        except UnicodeEncodeError:
            raise ValidationError("Invalid message content") from None

    def post_message(self, access: Access, *, project_id: str, requirement_id: str,
                     content: str) -> list[RequirementMessage]:
        self._active_member(access, project_id)
        requirement = access.store.get_requirement(project_id, requirement_id)
        self._requester_only(requirement, access.user.user_id)
        if requirement.status != "draft":
            raise InvalidState("Clarification is only available while drafting")
        self._message_content(content)
        reply = None
        if self.clarifier is not None:
            history = tuple(access.store.list_messages(project_id, requirement_id))
            reply = self.clarifier(requirement, history, content)
            if reply is not None:
                self._message_content(reply)
        now = self.clock()
        rows = [("user", access.user.user_id, content)]
        if reply is not None:
            rows.append(("assistant", None, reply))
        stored = access.store.add_messages(project_id, requirement_id, rows, created_at=now)
        record_audit(access.conn, now, access.user.user_id, "requirement.message_posted",
                     requirement_id, project_id=project_id, target_version=requirement.version)
        return stored

    def list_messages(self, access: Access, *, project_id: str,
                      requirement_id: str) -> list[RequirementMessage]:
        self._active_member(access, project_id)
        requirement = access.store.get_requirement(project_id, requirement_id)
        user_id = access.user.user_id
        if not (requirement.requester_id == user_id
                or (requirement.status == "in_review" and requirement.reviewer_id == user_id)):
            raise NotFound("Resource not found")
        return access.store.list_messages(project_id, requirement_id)

    def request_review(self, access: Access, *, project_id: str, requirement_id: str,
                       revision_id: str, reviewer_user_id: str,
                       expected_version: int) -> tuple[Requirement, RequirementReview]:
        self._active_member(access, project_id)
        requirement = access.store.get_requirement(project_id, requirement_id)
        self._requester_only(requirement, access.user.user_id)
        if requirement.status == "in_review":
            last = access.store.last_review(project_id, requirement_id)
            if (requirement.reviewer_id == reviewer_user_id
                    and requirement.review_revision_id == revision_id
                    and last is not None and last.decision == "requested"
                    and last.reviewer_id == reviewer_user_id and last.revision_id == revision_id):
                return requirement, last
            raise InvalidState("Requirement is already under review")
        now = self.clock()
        updated, review = access.store.request_requirement_review(
            project_id, requirement_id, revision_id=revision_id, reviewer_id=reviewer_user_id,
            expected_version=expected_version, created_at=now)
        record_audit(access.conn, now, access.user.user_id, "requirement.review_requested",
                     requirement_id, project_id=project_id, target_version=updated.version)
        return updated, review

    def decide_review(self, access: Access, *, project_id: str, requirement_id: str, revision_id: str,
                      decision: str, expected_version: int,
                      reason: str = "") -> tuple[Requirement, RequirementReview]:
        if decision not in ("approved", "rejected"):
            raise ValidationError("Invalid review decision")
        if not isinstance(reason, str) or len(reason) > MAX_REASON_CHARS:
            raise ValidationError("Invalid review reason")
        try:
            reason.encode("utf-8")
        except UnicodeEncodeError:
            raise ValidationError("Invalid review reason") from None
        self._active_member(access, project_id)
        requirement = access.store.get_requirement(project_id, requirement_id)
        user_id = access.user.user_id
        last = access.store.last_review(project_id, requirement_id)
        if (decision == "approved" and requirement.status == "approved"
                and requirement.approved_revision_id == revision_id
                and last is not None and last.decision == "approved"
                and last.reviewer_id == user_id and last.revision_id == revision_id):
            return requirement, last
        if (decision == "rejected" and requirement.status == "draft"
                and last is not None and last.decision == "rejected"
                and last.reviewer_id == user_id and last.revision_id == revision_id):
            return requirement, last
        if requirement.requester_id != user_id and requirement.reviewer_id != user_id:
            if requirement.status == "approved":
                raise Conflict("Requirement is already approved")
            raise NotFound("Resource not found")
        if requirement.status != "in_review":
            raise InvalidState("Requirement is not under review")
        if requirement.reviewer_id != user_id:
            raise Forbidden("Only the designated reviewer can decide")
        if requirement.review_revision_id != revision_id:
            raise Conflict("Review is bound to a different revision")
        now = self.clock()
        updated, review = access.store.decide_requirement_review(
            project_id, requirement_id, revision_id=revision_id, reviewer_id=user_id,
            decision=decision, reason=reason, expected_version=expected_version, created_at=now)
        record_audit(access.conn, now, user_id,
                     "requirement.review_approved" if decision == "approved"
                     else "requirement.review_rejected",
                     requirement_id, project_id=project_id, target_version=updated.version)
        return updated, review

    def list_reviews(self, access: Access, *, project_id: str,
                     requirement_id: str) -> list[RequirementReview]:
        self._get_visible(access, project_id, requirement_id)
        return access.store.list_reviews(project_id, requirement_id)

    def inbox(self, access: Access) -> list[dict]:
        return [{"item_type": item_type, **asdict(requirement)}
                for item_type, requirement in access.store.inbox(access.user.user_id)]
