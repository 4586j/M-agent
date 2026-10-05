"""The fence an operator raises after putting a restored archive back into service.

Section 15.3 treats a restore as neither a process restart nor a rollback of trust: whatever the
archive carried — machine leases, task tokens, signed-in people — was authorized at a moment that
has since passed. This takes those credentials back and leaves the data alone. It decides nothing
about the work itself: no reservation is given back, no result is written, and no attempt is
declared clean.
"""
from __future__ import annotations

import time

from .auth import record_audit
from .store import CollaborationStore


def fence_after_restore(database, *, instances, auth, clock=time.time) -> dict:
    """Disable every node and revoke every sign-in a restored database still carries.

    A disabled instance is not merely refused: its machine token stops identifying anything, so a
    callback from the machine that ran the archived round never reaches that round's ledger. The
    attempt is left exactly as the archive had it — disabling also advances the instance's control
    epoch, so a round re-dispatched elsewhere cannot be confused with the old one — and once its
    lease runs out the round is the assignee's to reconcile. Taking credentials back is not the same
    as judging the work. With no live instance left the queue can only mark items ``waiting_agent``,
    which is what "dispatch is frozen" means here: no new flag exists to forget to clear, and a
    half-finished fence is safe to re-run because the second pass finds nothing left to take back.
    """
    now = clock()
    store = CollaborationStore(database)
    nodes = 0
    disabled = 0
    for node in store.list_nodes():
        if node.status != "active":
            continue
        _, live = instances.disable_node(node.node_id)
        nodes += 1
        disabled += len(live)
    sessions = auth.revoke_all_sessions()
    with database.transaction() as conn:
        record_audit(conn, now, None, "restore.fenced", None)
    return {"nodes_disabled": nodes, "instances_disabled": disabled,
            "sessions_revoked": sessions}
