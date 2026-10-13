"""Internal dispatch context; never accepted from untrusted request bodies."""
from contextlib import contextmanager
from contextvars import ContextVar

_retry_parent = ContextVar("retry_parent", default=None)
_approved_prompt = ContextVar("approved_prompt", default=None)


class AdmissionRejected(RuntimeError):
    """A definitive refusal before the delegate could execute anything."""


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
    def __init__(self, delegate, budget, security_check, task_store):
        self.delegate = delegate
        self.budget = budget
        self.security_check = security_check
        self.task_store = task_store

    def __getattr__(self, name):
        return getattr(self.delegate, name)

    def check(self, prompt):
        allowed, reason = self.budget.check()
        if not allowed:
            raise AdmissionRejected(reason)
        if _approved_prompt.get() != prompt:
            verdict = self.security_check(prompt)
            if verdict.get("verdict") != "pass":
                raise AdmissionRejected("安全检查未通过，请通过任务审批入口处理")

    def dispatch(self, role, prompt, **kwargs):
        self.check(prompt)
        return self.delegate.dispatch(role=role, prompt=prompt, **kwargs)

    async def dispatch_async(self, role, prompt, **kwargs):
        import asyncio
        await asyncio.to_thread(self.check, prompt)
        return await self.delegate.dispatch_async(role=role, prompt=prompt, **kwargs)

    def redispatch(self, task_id, **kwargs):
        rec = self.task_store.get(task_id)
        if rec is None:
            raise KeyError(f"task not found: {task_id}")
        self.check(rec.prompt)
        return self.delegate.redispatch(task_id, **kwargs)


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
