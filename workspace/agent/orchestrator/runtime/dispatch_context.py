"""Internal dispatch context; never accepted from untrusted request bodies."""
import uuid
from contextlib import contextmanager
from contextvars import ContextVar

from budget_store import BudgetExceeded, cost_units
from cost_accounting import replay_costs

_retry_parent = ContextVar("retry_parent", default=None)
_approved_prompt = ContextVar("approved_prompt", default=None)
_reserved_task_id = ContextVar("reserved_task_id", default=None)


def new_dispatch_id() -> str:
    return _reserved_task_id.get() or "task-" + uuid.uuid4().hex


class AdmissionRejected(RuntimeError):
    """A definitive refusal before the delegate could execute anything."""


class BudgetAdmissionRejected(AdmissionRejected):
    pass


@contextmanager
def approved_dispatch(prompt: str):
    """Only the server's preflight/approval path grants this exact prompt an exemption."""
    token = _approved_prompt.set(prompt)
    try:
        yield
    finally:
        _approved_prompt.reset(token)


class AdmittedDispatcher:
    """One admission boundary for HTTP, schedules, retries and recovery dispatches."""
    def __init__(self, delegate, budget, security_check, task_store, reservation_usd=0.05):
        self.delegate = delegate
        self.budget = budget
        self.security_check = security_check
        self.task_store = task_store
        self.reservation_usd = reservation_usd
        if cost_units(reservation_usd) == 0:
            raise ValueError("budget reservation must be at least $0.000001")
        self.managed = getattr(delegate, "supports_budget_accounting", False)
        if self.managed:
            delegate.budget_store = budget

    def __getattr__(self, name):
        return getattr(self.delegate, name)

    def check(self, prompt):
        # Persisted but uncharged reports must be applied before admitting more work.
        replay_costs(self.task_store, self.budget, strict=True)
        allowed, reason = self.budget.check()
        if not allowed:
            raise BudgetAdmissionRejected(reason)
        if _approved_prompt.get() != prompt:
            verdict = self.security_check(prompt)
            if verdict.get("verdict") != "pass":
                raise AdmissionRejected("安全检查未通过，请通过任务审批入口处理")

    def dispatch(self, role, prompt, **kwargs):
        self.check(prompt)
        with self._reservation():
            return self.delegate.dispatch(role=role, prompt=prompt, **kwargs)

    async def dispatch_async(self, role, prompt, **kwargs):
        import asyncio
        await asyncio.to_thread(self.check, prompt)
        with self._reservation():
            return await self.delegate.dispatch_async(role=role, prompt=prompt, **kwargs)

    def redispatch(self, task_id, **kwargs):
        rec = self.task_store.get(task_id)
        if rec is None:
            raise KeyError(f"task not found: {task_id}")
        self.check(rec.prompt)
        with self._reservation():
            return self.delegate.redispatch(task_id, **kwargs)

    @contextmanager
    def _reservation(self):
        if not self.managed:
            yield
            return
        task_id = "task-" + uuid.uuid4().hex
        try:
            self.budget.reserve(task_id, self.reservation_usd)
        except BudgetExceeded as exc:
            raise BudgetAdmissionRejected(str(exc)) from exc
        token = _reserved_task_id.set(task_id)
        try:
            yield
        finally:
            _reserved_task_id.reset(token)
            # All supported delegates persist a task before starting execution.
            # Existing tasks (including cancelled/unknown outcomes) retain holds.
            if self.task_store.get(task_id) is None:
                self.budget.release(task_id)


@contextmanager
def retry_dispatch(parent_id: str):
    token = _retry_parent.set(parent_id)
    try:
        yield
    finally:
        _retry_parent.reset(token)


def retry_fields(max_retries: int, meta: dict) -> dict:
    parent = _retry_parent.get()
    return {"max_retries": 0 if parent else max_retries,
            "meta": {**meta, **({"retry_parent_id": parent} if parent else {})}}
