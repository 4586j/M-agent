"""What each person owes a decision on, aggregated across their projects.

Every entry repeats the permission the decision service itself enforces, so the inbox can offer
an action without the caller having to discover a refusal later.
"""
from __future__ import annotations

import time

from .auth import Access

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
    """Read-only projection over stored state; it decides nothing and writes nothing."""

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
