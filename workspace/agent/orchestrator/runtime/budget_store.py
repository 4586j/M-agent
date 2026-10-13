"""Atomic budget reservations and idempotent execution costs.

Legacy budgets.json is imported once into the adjacent .sqlite3 database.
All amounts are integer microdollars; malformed ledgers fail closed.
"""
import json
import math
import sqlite3
import time
import uuid
from contextlib import contextmanager
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path


def _today():
    return datetime.now().strftime("%Y-%m-%d")


def cost_units(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
        raise ValueError("cost must be a finite nonnegative number")
    units = int((Decimal(str(value)) * 1000000).quantize(Decimal(1), rounding=ROUND_HALF_UP))
    if units > 2**63 - 1:
        raise ValueError("cost exceeds ledger range")
    return units


def _limit(value):
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError("budget limit must be finite")
    if value <= 0:
        return None
    units = cost_units(value)
    if not units:
        raise ValueError("minimum positive budget is $0.000001")
    return units


class BudgetExceeded(RuntimeError):
    pass


class BudgetStore:
    def __init__(self, budgets_file, now_fn=time.time):
        self._path = Path(budgets_file)
        self.db_path = Path(str(self._path) + ".sqlite3")
        self._now = now_fn
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._transaction() as conn:
            conn.execute("""CREATE TABLE IF NOT EXISTS account (
                id INTEGER PRIMARY KEY CHECK(id=1), global_limit INTEGER,
                daily_limit INTEGER, spent INTEGER NOT NULL CHECK(typeof(spent)='integer'), updated REAL NOT NULL)""")
            conn.execute("CREATE TABLE IF NOT EXISTS daily (day TEXT PRIMARY KEY, spent INTEGER NOT NULL CHECK(typeof(spent)='integer'))")
            conn.execute("""CREATE TABLE IF NOT EXISTS attempts (
                task_id TEXT PRIMARY KEY, reserved INTEGER NOT NULL,
                cost INTEGER, state TEXT NOT NULL, day TEXT, updated REAL NOT NULL)""")
            if conn.execute("SELECT 1 FROM account WHERE id=1").fetchone() is None:
                raw = json.loads(self._path.read_text(encoding="utf-8")) if self._path.exists() else {}
                if not isinstance(raw, dict):
                    raise ValueError("invalid legacy budget ledger")
                conn.execute("INSERT INTO account VALUES (1, ?, ?, ?, ?)", (
                    _limit(raw.get("global_limit_usd")), _limit(raw.get("daily_limit_usd")),
                    cost_units(raw.get("spent_total_usd", 0)), self._now()))
                for day, amount in raw.get("spent_by_day", {}).items():
                    datetime.strptime(day, "%Y-%m-%d")
                    conn.execute("INSERT INTO daily VALUES (?, ?)", (day, cost_units(amount)))

    @contextmanager
    def _transaction(self, write=True):
        conn = sqlite3.connect(self.db_path, timeout=10, isolation_level=None)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA synchronous=FULL")
            conn.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            yield conn
            conn.execute("COMMIT")
        except BaseException:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()

    @staticmethod
    def _usage(conn):
        acc = conn.execute("SELECT * FROM account WHERE id=1").fetchone()
        daily = conn.execute("SELECT spent FROM daily WHERE day=?", (_today(),)).fetchone()
        reserved = conn.execute("SELECT COALESCE(SUM(reserved),0) FROM attempts WHERE state='reserved'").fetchone()[0]
        # Count all outstanding work against today's cap, including work from yesterday.
        return acc, daily[0] if daily else 0, reserved

    @staticmethod
    def _reason(acc, today, reserved, requested=0):
        for label, limit, spent in (("全局预算", acc["global_limit"], acc["spent"]),
                                   ("当日预算", acc["daily_limit"], today)):
            if limit is not None and (spent + reserved >= limit or spent + reserved + requested > limit):
                return (f"{label}不足：已花 ${spent / 1e6:.6f}，预占 ${reserved / 1e6:.6f}，"
                        f"本次需 ${requested / 1e6:.6f} / 上限 ${limit / 1e6:.6f}")
        return ""

    def set_limits(self, global_limit_usd=None, daily_limit_usd=None):
        with self._transaction() as conn:
            conn.execute("UPDATE account SET global_limit=?, daily_limit=?, updated=? WHERE id=1",
                         (_limit(global_limit_usd), _limit(daily_limit_usd), self._now()))
        return self.snapshot()

    def reserve(self, task_id, amount_usd):
        amount = cost_units(amount_usd)
        if not amount:
            raise ValueError("reservation must be at least $0.000001")
        with self._transaction() as conn:
            old = conn.execute("SELECT * FROM attempts WHERE task_id=?", (task_id,)).fetchone()
            if old:
                if old["state"] != "reserved" or old["reserved"] != amount:
                    raise ValueError("task already has a different or closed reservation")
                return
            reason = self._reason(*self._usage(conn), requested=amount)
            if reason:
                raise BudgetExceeded(reason)
            conn.execute("INSERT INTO attempts VALUES (?, ?, NULL, 'reserved', NULL, ?)",
                         (task_id, amount, self._now()))
            conn.execute("UPDATE account SET updated=? WHERE id=1", (self._now(),))

    def release(self, task_id):
        """Only for a definitive refusal before execution, never for cancellation."""
        with self._transaction() as conn:
            conn.execute("UPDATE attempts SET state='released', updated=? WHERE task_id=? AND state='reserved'",
                         (self._now(), task_id))
            conn.execute("UPDATE account SET updated=? WHERE id=1", (self._now(),))

    def settle(self, task_id, cost_usd, day=None):
        """Charge cumulative cost once; later increases add only the delta."""
        units = cost_units(cost_usd)
        day = day or _today()
        datetime.strptime(day, "%Y-%m-%d")
        with self._transaction() as conn:
            old = conn.execute("SELECT * FROM attempts WHERE task_id=?", (task_id,)).fetchone()
            if old is None:
                raise KeyError(f"unreserved execution: {task_id}")
            if old["cost"] is not None and units <= old["cost"]:
                return False
            delta, charge_day = units - (old["cost"] or 0), old["day"] or day
            self._charge(conn, delta, charge_day)
            conn.execute("UPDATE attempts SET cost=?, state='settled', day=?, updated=? WHERE task_id=?",
                         (units, charge_day, self._now(), task_id))
        return True

    def _charge(self, conn, units, day):
        conn.execute("UPDATE account SET spent=spent+?, updated=? WHERE id=1", (units, self._now()))
        conn.execute("INSERT INTO daily VALUES (?, ?) ON CONFLICT(day) DO UPDATE SET spent=spent+excluded.spent",
                     (day, units))

    def attempt(self, task_id):
        with self._transaction(write=False) as conn:
            row = conn.execute("SELECT * FROM attempts WHERE task_id=?", (task_id,)).fetchone()
            return dict(row) if row else None

    def unsettled_ids(self):
        with self._transaction(write=False) as conn:
            return {r[0] for r in conn.execute("SELECT task_id FROM attempts WHERE state='reserved'")}

    def known_costs(self):
        with self._transaction(write=False) as conn:
            return {r[0]: r[1] for r in conn.execute("SELECT task_id, cost FROM attempts")}

    def reservations(self):
        with self._transaction(write=False) as conn:
            return [{"task_id": r["task_id"], "reserved_usd": r["reserved"] / 1e6,
                     "updated_at": r["updated"]} for r in conn.execute(
                         "SELECT task_id, reserved, updated FROM attempts WHERE state='reserved' ORDER BY updated")]

    def record_spend(self, cost_usd):
        """Compatibility charges without idempotency; managed dispatch uses settle."""
        try:
            units = cost_units(cost_usd)
        except ValueError:
            return
        if not units:
            return
        with self._transaction() as conn:
            day = _today()
            self._charge(conn, units, day)
            conn.execute("INSERT INTO attempts VALUES (?, 0, ?, 'settled', ?, ?)",
                         ("legacy-" + uuid.uuid4().hex, units, day, self._now()))

    def check(self, requested_usd=0):
        with self._transaction(write=False) as conn:
            reason = self._reason(*self._usage(conn), requested=cost_units(requested_usd))
            return not reason, reason

    def snapshot(self):
        with self._transaction(write=False) as conn:
            acc, today, reserved = self._usage(conn)
            g, d, total = acc["global_limit"], acc["daily_limit"], acc["spent"]
            count = conn.execute("SELECT count(*) FROM attempts WHERE state='reserved'").fetchone()[0]
            return {"global_limit_usd": g / 1e6 if g is not None else None,
                    "daily_limit_usd": d / 1e6 if d is not None else None,
                    "spent_total_usd": total / 1e6, "spent_today_usd": today / 1e6,
                    "reserved_usd": reserved / 1e6, "unsettled_count": count,
                    "global_remaining_usd": (g - total - reserved) / 1e6 if g is not None else None,
                    "daily_remaining_usd": (d - today - reserved) / 1e6 if d is not None else None,
                    "updated_at": acc["updated"]}
