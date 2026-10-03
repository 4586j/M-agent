"""The resident scheduling host: the round a person triggers by hand, run on a timer.

One round is the existing ``Scheduler.tick`` — deliver due outbox events, recompute which work
items are ready, optionally dispatch them — plus one sweep that re-derives every delivering
requirement's own status from what its work items actually did. That sweep is what lets a finished
delivery reach the requester without anybody clicking, and it writes the same status the panel's
own summary button writes, with no actor, because the host speaks for no person.
"""
from __future__ import annotations

import logging
import time

from .auth import record_audit
from .models import InvalidState
from .reviews import DELIVERING_STATUSES, ReviewService
from .store import CollaborationStore

LOGGER = logging.getLogger("collaboration.host")


class SchedulerHost:
    """Keep running scheduling rounds until the caller asks it to stop."""

    def __init__(self, scheduler, database, *, reviews: ReviewService, clock=time.time):
        self.scheduler = scheduler
        self.database = database
        self.reviews = reviews
        self.clock = clock

    def aggregate(self, project_id: str | None = None) -> int:
        """Move each delivering requirement to the status its work items already justify.

        The status is recomputed inside the same transaction that writes it, so a requirement a
        person just signed off or returned cannot be moved by a stale reading of its items.
        """
        now = self.clock()
        store = CollaborationStore(self.database)
        changed = 0
        for listed in store.delivering_requirements(DELIVERING_STATUSES, project_id=project_id):
            with self.database.transaction() as conn:
                scoped = CollaborationStore(self.database, connection=conn)
                requirement = scoped.get_requirement(listed.project_id, listed.requirement_id)
                if requirement.status not in DELIVERING_STATUSES:
                    continue
                try:
                    target = self.reviews.requirement_target(scoped, requirement.project_id,
                                                             requirement.requirement_id)
                except InvalidState:
                    continue
                if target == requirement.status:
                    continue
                updated = scoped.set_requirement_status(requirement.project_id,
                                                        requirement.requirement_id, status=target,
                                                        expected_version=requirement.version,
                                                        updated_at=now)
                record_audit(conn, now, None, f"requirement.{target}", requirement.requirement_id,
                             project_id=requirement.project_id, target_version=updated.version)
                changed += 1
        return changed

    def tick(self, project_id: str | None = None) -> dict:
        """One round: the scheduler's own work first, then the requirement sweep it unlocks."""
        result = self.scheduler.tick(project_id=project_id)
        result["requirements_changed"] = self.aggregate(project_id=project_id)
        return result

    def run(self, *, interval: float, iterations: int | None = None,
            project_id: str | None = None, sleeper=time.sleep) -> int:
        """Do ``iterations`` rounds, or keep going, sleeping ``interval`` seconds between them.

        A round that raises is logged and skipped rather than taken down with the host: whatever
        it did not get to stays queued, so the next round picks it up. Only the exception type is
        logged, because exception text can carry identifiers or file paths.
        """
        rounds = 0
        while iterations is None or rounds < iterations:
            try:
                self.tick(project_id=project_id)
            except Exception as error:  # noqa: BLE001 - the host outlives any single round
                LOGGER.warning("Scheduling round failed: %s", type(error).__name__)
            rounds += 1
            if iterations is not None and rounds >= iterations:
                break
            sleeper(interval)
        return rounds
