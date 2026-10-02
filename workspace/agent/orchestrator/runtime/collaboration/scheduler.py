"""Durable outbox delivery and the work-item readiness queue."""
from __future__ import annotations

import json
import time

from .auth import record_audit
from .dispatch import DISPATCHABLE_STATUSES, TOPIC_ATTEMPT_ASSIGNED
from .instances import AdmissionDenied
from .models import Conflict, InvalidState, NotFound
from .store import CollaborationStore, WORK_ITEM_QUEUE_STATUSES

TOPIC_REQUIREMENT_APPROVED = "requirement.approved"
TOPIC_PLAN_PUBLISHED = "plan.published"


class _NoHandler(Exception):
    """No handler is registered for the event topic."""


class OutboxDispatcher:
    """Claim-based, at-least-once delivery: handlers always run outside the claim transaction.

    A lost lease (another dispatcher already re-claimed the event) makes the guarded
    mark/defer updates no-ops, so a crashed claim is retried once the lease expires.
    """

    def __init__(self, database, *, clock=time.time, lease_ttl: int = 300, batch_size: int = 32,
                 base_delay: float = 5.0, max_delay: float = 3600.0):
        self.database = database
        self.clock = clock
        self.lease_ttl = lease_ttl
        self.batch_size = batch_size
        self.base_delay = base_delay
        self.max_delay = max_delay
        self.handlers: dict = {}

    def register(self, topic: str, handler) -> None:
        self.handlers[topic] = handler

    def _backoff(self, attempts: int) -> float:
        return min(self.base_delay * (2 ** attempts), self.max_delay)

    def dispatch_once(self, project_id: str | None = None) -> dict:
        now = self.clock()
        store = CollaborationStore(self.database)
        events = store.claim_due_events(now=now, lease_ttl=self.lease_ttl,
                                        batch_size=self.batch_size, project_id=project_id)
        delivered = deferred = 0
        for event in events:
            handler = self.handlers.get(event.topic)
            try:
                if handler is None:
                    raise _NoHandler(event.topic)
                handler(event)
            except Exception as exc:
                changed = store.defer_event(
                    event.event_id, claim_expires_at=event.claim_expires_at,
                    next_attempt_at=now + self._backoff(event.attempts),
                    last_error=type(exc).__name__[:128])
                deferred += 1 if changed else 0
            else:
                changed = store.mark_event_delivered(
                    event.event_id, claim_expires_at=event.claim_expires_at,
                    delivered_at=self.clock())
                delivered += 1 if changed else 0
        return {"claimed": len(events), "delivered": delivered, "deferred": deferred}


class QueueService:
    """Recomputes the queue status of each pending work item from current facts.

    Failed bindings, members, agents or dependencies move an item to ``blocked``,
    which is sticky: it leaves the pending set until an explicit unblock action.
    """

    def __init__(self, database, *, heartbeat_timeout: float = 120, clock=time.time):
        self.database = database
        self.heartbeat_timeout = heartbeat_timeout
        self.clock = clock

    @staticmethod
    def _broken(store, item) -> bool:
        try:
            binding = store.get_binding(item.project_id, item.binding_id)
            if binding.status != "active" or binding.member_user_id != item.assignee_user_id:
                return True
            member = store.get_member(item.project_id, item.assignee_user_id)
            if member.status != "active":
                return True
            agent = store.get_agent(binding.agent_id, binding.member_user_id)
        except NotFound:
            return True
        return agent.status != "active"

    def _target(self, store, item, fresh_since: float) -> str:
        if store.dependency_gap_count(item.work_item_id) > 0:
            return "pending_dependency"
        if self._broken(store, item):
            return "blocked"
        if store.binding_has_live_instance(item.binding_id, fresh_since=fresh_since):
            return "ready"
        return "waiting_agent"

    def advance(self, project_id: str | None = None) -> dict:
        now = self.clock()
        fresh_since = now - self.heartbeat_timeout
        changed = 0
        listed = CollaborationStore(self.database).pending_work_items(project_id)
        for item in listed:
            with self.database.transaction() as conn:
                store = CollaborationStore(self.database, connection=conn)
                current = store.get_work_item(item.project_id, item.work_item_id)
                if current.status not in WORK_ITEM_QUEUE_STATUSES:
                    continue
                target = self._target(store, current, fresh_since)
                if target == current.status:
                    continue
                updated = store.advance_work_item(
                    current.work_item_id, status=target, expected_version=current.version,
                    updated_at=now)
                record_audit(conn, now, None, f"work_item.{target}", current.work_item_id,
                             project_id=current.project_id, target_version=updated.version)
                changed += 1
        return {"work_items_changed": changed}


class Scheduler:
    """Connects design events to actions: approval plans, publication queues, dispatch."""

    def __init__(self, database, *, planning, queue, dispatch=None, auto_dispatch: bool = False,
                 dispatcher=None, clock=time.time):
        self.database = database
        self.planning = planning
        self.queue = queue
        self.dispatch = dispatch
        self.auto_dispatch = auto_dispatch
        self.clock = clock
        self.dispatcher = dispatcher if dispatcher is not None else OutboxDispatcher(
            database, clock=clock)
        self.dispatcher.register(TOPIC_REQUIREMENT_APPROVED, self._on_approved)
        self.dispatcher.register(TOPIC_PLAN_PUBLISHED, self._on_published)
        if dispatch is not None:
            self.dispatcher.register(TOPIC_ATTEMPT_ASSIGNED, dispatch.wake)

    def _on_approved(self, event) -> None:
        payload = json.loads(event.payload)
        self.planning.plan(project_id=event.project_id,
                           requirement_id=payload["requirement_id"], actor_id=None,
                           require_approved_only=True,
                           expected_revision_id=payload["revision_id"])

    def _on_published(self, event) -> None:
        # Readiness is recomputed by tick() right after delivery; parsing here only
        # rejects a corrupt payload so it defers with a recorded error.
        json.loads(event.payload)

    def _dispatch_ready(self, project_id: str | None) -> int:
        """Push every dispatchable item once; refusals stay queued for the next tick."""
        dispatched = 0
        listed = CollaborationStore(self.database).pending_work_items(project_id)
        for item in listed:
            if item.status not in DISPATCHABLE_STATUSES:
                continue
            try:
                self.dispatch.dispatch(item.project_id, item.work_item_id)
            except (AdmissionDenied, Conflict, InvalidState):
                continue
            dispatched += 1
        return dispatched

    def tick(self, project_id: str | None = None) -> dict:
        delivery = self.dispatcher.dispatch_once(project_id=project_id)
        queue = self.queue.advance(project_id=project_id)
        if self.dispatch is not None and self.auto_dispatch:
            queue["attempts_dispatched"] = self._dispatch_ready(project_id)
        return {**delivery, **queue}
