"""Persist cost observations before settlement, independently of task status."""
import logging

from budget_store import _today, cost_units

log = logging.getLogger(__name__)


def capture_cost(dispatcher, task_id: str, meta) -> bool:
    """Return whether this is a managed execution (legacy hooks must then be skipped).

    A missing/invalid cost does not release a reservation. Accounting IO failures
    propagate: a callback can retry and the persisted outbox can be replayed.
    """
    budget = getattr(dispatcher, "budget_store", None)
    if budget is None or budget.attempt(task_id) is None:
        return False
    if not isinstance(meta, dict) or meta.get("execution_finished") is False:
        return True
    try:
        cost_units(meta.get("cost_usd"))
    except ValueError:
        return True
    report = dispatcher.task_store.record_cost(task_id, meta["cost_usd"], _today())
    budget.settle(task_id, report["cost_usd"], report["day"])
    return True


def replay_costs(task_store, budget, *, strict=False) -> int:
    count = 0
    known = budget.known_costs()
    for rec in task_store.list_all():
        if rec.cost_report is None or rec.task_id not in known:
            continue
        if known[rec.task_id] is not None and cost_units(rec.cost_report["cost_usd"]) <= known[rec.task_id]:
            continue
        try:
            count += budget.settle(rec.task_id, rec.cost_report["cost_usd"], rec.cost_report["day"])
        except Exception:
            log.exception("Cost reconciliation failed for task=%s", rec.task_id)
            if strict:
                raise
    return count


def release_refused(dispatcher, task_id: str, status_code: int) -> None:
    # Only statuses that prove the worker refused admission. A 5xx response may
    # follow execution, so conservatively keep that reservation for reconciliation.
    budget = getattr(dispatcher, "budget_store", None)
    if budget is not None and status_code in (400, 401, 403, 404, 409, 422, 429):
        budget.release(task_id)
