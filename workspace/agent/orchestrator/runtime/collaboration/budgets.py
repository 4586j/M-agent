"""Global, project and requirement cost limits with atomic reservation."""
from __future__ import annotations

import time

from .auth import Access, record_audit
from .models import ValidationError


class BudgetService:
    """Integer cost units; an account without a configured limit is unlimited.

    Reservations are written for every scope so usage stays auditable even where
    no cap exists; the unique entry key makes settle/release idempotent.
    """

    def __init__(self, *, clock=time.time):
        self.clock = clock

    def _scopes(self, project_id: str, requirement_id: str) -> tuple[tuple[str, str], ...]:
        return (("global", ""), ("project", project_id), ("requirement", requirement_id))

    @staticmethod
    def _used(store, scope: str, ref_id: str) -> int:
        return sum(entry.delta_units for entry in store.list_budget_entries(scope, ref_id))

    def limit_problems(self, store, *, project_id: str, requirement_id: str,
                       amount: int) -> list[str]:
        problems = []
        for scope, ref_id in self._scopes(project_id, requirement_id):
            account = store.get_budget_account(scope, ref_id)
            if account is None:
                continue
            if account.limit_units - self._used(store, scope, ref_id) < amount:
                problems.append(f"budget_exceeded:{scope}")
        return problems

    def reserve(self, store, *, project_id: str, requirement_id: str, plan_id: str, amount: int,
                created_at: float) -> None:
        if type(amount) is not int or amount < 0:
            raise ValidationError("Invalid budget reservation")
        if amount == 0:
            return
        for scope, ref_id in self._scopes(project_id, requirement_id):
            store.add_budget_entry(scope, ref_id, ref_type="plan", ref_key=plan_id,
                                   kind="reserve", delta_units=amount, created_at=created_at)

    @classmethod
    def account_payload(cls, store, account) -> dict:
        used = cls._used(store, account.scope, account.ref_id)
        return {"scope": account.scope, "ref_id": account.ref_id,
                "limit_units": account.limit_units, "used_units": used,
                "remaining_units": account.limit_units - used,
                "version": account.version, "updated_at": account.updated_at}

    def set_limit(self, access: Access, *, scope: str, ref_id: str, limit_units: int,
                  expected_version: int | None, project_id: str | None = None) -> dict:
        account = access.store.upsert_budget_account(
            scope, ref_id, limit_units=limit_units, expected_version=expected_version,
            updated_at=self.clock())
        record_audit(access.conn, self.clock(), access.user.user_id, "budget.limit_set",
                     f"{scope}:{ref_id}", project_id=project_id, target_version=account.version)
        return self.account_payload(access.store, account)

    def budget_view(self, access: Access, *, project_id: str) -> dict:
        return {"accounts": [self.account_payload(access.store, account)
                             for account in access.store.list_budget_accounts(project_id=project_id)]}
