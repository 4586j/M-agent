"""What each person owes a decision on, aggregated across their projects.

Every entry repeats the permission the decision service itself enforces, so the inbox can offer
an action without the caller having to discover a refusal later.
"""
from __future__ import annotations

import time

from .auth import Access
from .models import ValidationError


def _identity(entry: dict) -> str:
    """One name for one decision owed, so seeing it today means it stays seen after a refresh.

    The most specific resource names it: an attempt, else a work item, else an instance, else the
    requirement. A drafting to-do also carries the revision it asks to be reviewed, because
    reviewing a second revision is a second decision even though the requirement is the same one.
    """
    resource = next(value for value in (entry.get("attempt_id"), entry.get("work_item_id"),
                                        entry.get("instance_id"),
                                        entry.get("requirement_id")) if value)
    parts = [entry["item_type"], entry["project_id"], resource]
    if entry["item_type"].startswith("requirement_"):
        parts.append(entry.get("current_revision_id") or "")
    return ":".join(parts)

# item_type -> (action, href, links). ``href`` is where the owed decision is written and
# ``expected_version`` is the version of that same resource; ``links`` are the reads the decision
# has to be made against, so a client never has to rebuild a path from an action name.
ACTIONS = {
    "delivery_confirmation": ("confirm_delivery",
                              "/projects/{project_id}/attempts/{attempt_id}/reviews", {}),
    "attempt_reconciliation": ("reconcile_attempt",
                               "/projects/{project_id}/attempts/{attempt_id}/reconcile", {}),
    # A machine that stopped renewing its lease has no decision of its own to record. What a
    # person can do is take the execution right back, which then surfaces as a reconciliation.
    "worker_attention": ("cancel_work_item",
                         "/projects/{project_id}/work-items/{work_item_id}/cancel",
                         {"instances":
                          "/projects/{project_id}/agent-bindings/{binding_id}/instances"}),
    # Nothing is in flight and no machine is answering for this binding: the one write a manager
    # owes here is to say whether that machine is still coming back, which is recorded by
    # disabling it. Bringing another one up stays a node-side action, because only a node token
    # can claim a slot, and issuing machine credentials through the browser is a wider door.
    "machine_absent": ("disable_instance",
                       "/projects/{project_id}/agent-bindings/{binding_id}"
                       "/instances/{instance_id}/disable",
                       {"instances":
                        "/projects/{project_id}/agent-bindings/{binding_id}/instances"}),
    "business_acceptance": ("accept_work_item",
                            "/projects/{project_id}/work-items/{work_item_id}/accept", {}),
    "rework_queue": ("rework_work_item",
                     "/projects/{project_id}/work-items/{work_item_id}/rework", {}),
    "final_acceptance": ("accept_requirement",
                         "/projects/{project_id}/requirements/{requirement_id}/acceptance",
                         {"coverage": "/projects/{project_id}/requirements/{requirement_id}"
                                       "/integration",
                          "delivery": "/projects/{project_id}/requirements/{requirement_id}"
                                      "/delivery"}),
    # A delivery that has stopped being somebody's work has to become somebody's check: it is
    # owed to the project's managers, who are the only ones allowed to record the verdict.
    "integration_check": ("record_integration",
                          "/projects/{project_id}/requirements/{requirement_id}/integration",
                          {"coverage": "/projects/{project_id}/requirements/{requirement_id}"
                                        "/integration",
                           "delivery": "/projects/{project_id}/requirements/{requirement_id}"
                                       "/delivery"}),
}

# The drafting to-dos come from a different projection, so they arrive as bare requirement rows
# without an action to take. They get the same promise as the eight above: this is the write that
# clears me, and this is the version of mine that it has to be made against. Both of them are
# decided on the reviews endpoint, and both of them link to the text the decision is made about.
DRAFT_ACTIONS = {
    "requirement_review": ("review_requirement",
                           "/projects/{project_id}/requirements/{requirement_id}/reviews",
                           {"revision": "/projects/{project_id}/requirements/{requirement_id}"
                                          "/revisions/{review_revision_id}",
                            "messages": "/projects/{project_id}/requirements/{requirement_id}"
                                        "/messages"}),
    "requirement_returned": ("request_review",
                             "/projects/{project_id}/requirements/{requirement_id}/reviews",
                             {"revision": "/projects/{project_id}/requirements/{requirement_id}"
                                            "/revisions/{current_revision_id}"}),
}


class InboxService:
    """A projection over stored state, plus one piece of per-user read state.

    It decides nothing about the work itself: a to-do is what a decision service already owes to
    somebody, and an acknowledgement only says that person has seen it.
    """

    def __init__(self, *, clock=time.time, heartbeat_timeout: float = 120):
        self.clock = clock
        # The same window the queue uses to decide a binding has no machine to run on, so the
        # to-do and the ``waiting_agent`` status cannot disagree about who is absent.
        self.heartbeat_timeout = heartbeat_timeout

    def pending(self, access: Access) -> list[dict]:
        now = self.clock()
        rows = access.store.pending_actions(access.user.user_id, now=now,
                                            fresh_since=now - self.heartbeat_timeout)
        entries = []
        for row in rows:
            action, template, links = ACTIONS[row["item_type"]]
            entries.append({**row, "action": action, "href": template.format(**row),
                            "links": {name: path.format(**row)
                                      for name, path in links.items()}})
        entries.sort(key=lambda entry: (-entry["updated_at"], entry["item_type"],
                                        entry["project_id"]))
        return entries

    def stamp(self, access: Access, entries: list[dict]) -> list[dict]:
        """Give every to-do its identity, this reader's unread flag, and an action if it lacks one."""
        acked = access.store.inbox_acks(access.user.user_id)
        stamped = []
        for entry in entries:
            key = _identity(entry)
            enriched = {**entry, "key": key, "unread": key not in acked}
            if entry["item_type"] in DRAFT_ACTIONS:
                action, template, links = DRAFT_ACTIONS[entry["item_type"]]
                enriched.update({"action": action, "href": template.format(**entry),
                                 "expected_version": entry["version"],
                                 "links": {name: path.format(**entry)
                                           for name, path in links.items()}})
            stamped.append(enriched)
        return stamped

    def ack(self, access: Access, *, item_key: str, known: set[str]) -> int:
        """Mark one of the to-dos this person is actually owed as seen; the decision stays open.

        ``known`` is the whole set the read view could currently hand back, and it doubles as the
        boundary for reclamation: an acknowledgement for anything outside it names a decision that
        is no longer owed or news too old to be shown again, so this same round drops it.
        """
        if item_key not in known:
            raise ValidationError("That is not a to-do you currently owe")
        access.store.ack_inbox(access.user.user_id, item_key=item_key, acked_at=self.clock())
        return access.store.prune_inbox_acks(access.user.user_id, keep=known)
