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

    The most specific resource names it: an attempt, else a work item, else the requirement.
    A drafting to-do also carries the revision it asks to be reviewed, because reviewing a second
    revision is a second decision even though the requirement is the same one.
    """
    resource = next(value for value in (entry.get("attempt_id"), entry.get("work_item_id"),
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
    "business_acceptance": ("accept_work_item",
                            "/projects/{project_id}/work-items/{work_item_id}/accept", {}),
    "rework_queue": ("rework_work_item",
                     "/projects/{project_id}/work-items/{work_item_id}/rework", {}),
    "final_acceptance": ("accept_requirement",
                         "/projects/{project_id}/requirements/{requirement_id}/acceptance", {}),
}


class InboxService:
    """A projection over stored state, plus one piece of per-user read state.

    It decides nothing about the work itself: a to-do is what a decision service already owes to
    somebody, and an acknowledgement only says that person has seen it.
    """

    def __init__(self, *, clock=time.time):
        self.clock = clock

    def pending(self, access: Access) -> list[dict]:
        rows = access.store.pending_actions(access.user.user_id, now=self.clock())
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
        """Attach every to-do's identity and whether this person has already seen it."""
        acked = access.store.inbox_acks(access.user.user_id)
        stamped = []
        for entry in entries:
            key = _identity(entry)
            stamped.append({**entry, "key": key, "unread": key not in acked})
        return stamped

    def ack(self, access: Access, *, item_key: str, known: set[str]) -> None:
        """Mark one of the to-dos this person is actually owed as seen; the decision stays open."""
        if item_key not in known:
            raise ValidationError("That is not a to-do you currently owe")
        access.store.ack_inbox(access.user.user_id, item_key=item_key, acked_at=self.clock())
