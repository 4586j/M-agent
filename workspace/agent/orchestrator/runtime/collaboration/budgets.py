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

    def _entries(self, store, *, project_id: str, requirement_id: str, ref_type: str,
                 ref_key: str, kind: str, delta_units: int, created_at: float) -> None:
        if delta_units == 0:
            return
        for scope, ref_id in self._scopes(project_id, requirement_id):
            store.add_budget_entry(scope, ref_id, ref_type=ref_type, ref_key=ref_key,
                                   kind=kind, delta_units=delta_units, created_at=created_at)

    def reserve(self, store, *, project_id: str, requirement_id: str, plan_id: str, amount: int,
                created_at: float) -> None:
        if type(amount) is not int or amount < 0:
            raise ValidationError("Invalid budget reservation")
        self._entries(store, project_id=project_id, requirement_id=requirement_id,
                      ref_type="plan", ref_key=plan_id, kind="reserve",
                      delta_units=amount, created_at=created_at)

    def reserve_item(self, store, *, project_id: str, requirement_id: str, work_item_id: str,
                     amount: int, created_at: float) -> None:
        """Reserve the share of one work item that joined after the plan was approved.

        The plan-level reservation carries one row per plan, because the plan is the thing a human
        approved as a whole. An item added beside it gets its own key, so the approved total is not
        rewritten and a retried request reserves exactly once.
        """
        if type(amount) is not int or amount < 0:
            raise ValidationError("Invalid budget reservation")
        self._entries(store, project_id=project_id, requirement_id=requirement_id,
                      ref_type="work_item", ref_key=work_item_id, kind="reserve",
                      delta_units=amount, created_at=created_at)

    def settle(self, store, *, project_id: str, requirement_id: str, attempt_id: str,
               reserved_units: int, actual_units: int, created_at: float) -> None:
        """Charge one attempt's real usage against the share already reserved for it.

        The entry is keyed by the attempt id, so repeated or concurrent result
        callbacks adjust the books exactly once.
        """
        for name, value in (("reserved_units", reserved_units), ("actual_units", actual_units)):
            if type(value) is not int or value < 0:
                raise ValidationError(f"Invalid {name}")
        self._entries(store, project_id=project_id, requirement_id=requirement_id,
                      ref_type="attempt", ref_key=attempt_id, kind="settle",
                      delta_units=actual_units - reserved_units, created_at=created_at)

    def release(self, store, *, project_id: str, requirement_id: str, attempt_id: str,
                reserved_units: int, created_at: float) -> None:
        """Give an unspent reservation back after a cancelled attempt, once per attempt."""
        if type(reserved_units) is not int or reserved_units <= 0:
            raise ValidationError("Invalid released amount")
        self._entries(store, project_id=project_id, requirement_id=requirement_id,
                      ref_type="attempt", ref_key=attempt_id, kind="release",
                      delta_units=-reserved_units, created_at=created_at)

    def release_plan(self, store, *, project_id: str, requirement_id: str, plan_id: str,
                     amount: int, created_at: float) -> None:
        """Give back the share of a plan reservation no attempt could ever spend.

        Keyed by the plan, so cancelling the same requirement twice returns nothing twice.
        Items that did run keep their own settle/release entries: this only returns estimates
        of work that was never dispatched.
        """
        if type(amount) is not int or amount < 0:
            raise ValidationError("Invalid plan release amount")
        self._entries(store, project_id=project_id, requirement_id=requirement_id,
                      ref_type="plan", ref_key=plan_id, kind="release",
                      delta_units=-amount, created_at=created_at)

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
