"""What each person owes a decision on, aggregated across their projects.

Every entry repeats the permission the decision service itself enforces, so the inbox can offer
an action without the caller having to discover a refusal later.
"""
from __future__ import annotations

import time

from .auth import Access

ACTIONS = {
    "delivery_confirmation": ("confirm_delivery",
                              "/projects/{project_id}/attempts/{attempt_id}/reviews"),
    "attempt_reconciliation": ("reconcile_attempt",
                               "/projects/{project_id}/attempts/{attempt_id}/reconcile"),
    "worker_attention": ("inspect_worker",
                         "/projects/{project_id}/agent-bindings/{binding_id}/instances"),
    "business_acceptance": ("accept_work_item",
                            "/projects/{project_id}/work-items/{work_item_id}/accept"),
    "rework_queue": ("rework_work_item",
                     "/projects/{project_id}/work-items/{work_item_id}/rework"),
    "final_acceptance": ("accept_requirement",
                         "/projects/{project_id}/requirements/{requirement_id}/acceptance"),
}


class InboxService:
    """Read-only projection over stored state; it decides nothing and writes nothing."""

    def __init__(self, *, clock=time.time):
        self.clock = clock

    def pending(self, access: Access) -> list[dict]:
        rows = access.store.pending_actions(access.user.user_id, now=self.clock())
        entries = []
        for row in rows:
            action, template = ACTIONS[row["item_type"]]
            entries.append({**row, "action": action, "href": template.format(**row)})
        entries.sort(key=lambda entry: (-entry["updated_at"], entry["item_type"],
                                        entry["project_id"]))
        return entries
