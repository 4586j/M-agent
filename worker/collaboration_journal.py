"""本地持久执行日志：重复投递只启动一个执行者，重启后先对账而不是重跑。

服务端的 wake 提示不携带授权，所以日志按 attempt_id 记录本机看到的每一次状态迁移。
执行者占位由一条带 ``executor IS NULL`` 条件的 UPDATE 完成，两个并发投递只有一个能启动；
进程重启时仍标记为执行中的尝试会转入对账状态，避免把"重启"当成"重新执行全部任务"。
"""
from __future__ import annotations

from dataclasses import dataclass, fields
import re
import sqlite3
import threading
import time
from pathlib import Path

ID_PATTERN = re.compile(r"^[0-9a-f]{32}$")
DIGEST_PATTERN = re.compile(r"^[0-9a-f]{64}$")
HOLDER_PATTERN = re.compile(r"[A-Za-z0-9._:-]{1,128}")

DISCOVERED = "discovered"
VERIFIED = "verified"
PAUSED_GATE = "paused_gate"
REFUSED = "refused"
STARTED = "started"
REPORTED = "reported"
RECONCILING = "reconciling"
STOPPED = "stopped"
STATES = (DISCOVERED, VERIFIED, PAUSED_GATE, REFUSED, STARTED, REPORTED, RECONCILING, STOPPED)
# States a later wake-up may drive through the pull path again.
DRIVABLE = (DISCOVERED, VERIFIED, PAUSED_GATE, RECONCILING)
# States that already hold this machine's answer for the attempt id.
SETTLED = (REFUSED, REPORTED, STOPPED)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS collab_attempts (
    attempt_id TEXT PRIMARY KEY,
    work_item_id TEXT NOT NULL,
    wake_input_hash TEXT NOT NULL,
    input_hash TEXT,
    requirement_hash TEXT,
    package_path TEXT,
    requirement_path TEXT,
    control_epoch INTEGER NOT NULL DEFAULT 0,
    lease_epoch INTEGER NOT NULL DEFAULT 0,
    state TEXT NOT NULL,
    reason TEXT NOT NULL DEFAULT '',
    executor TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS collab_events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    attempt_id TEXT NOT NULL,
    state TEXT NOT NULL,
    reason TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL,
    FOREIGN KEY (attempt_id) REFERENCES collab_attempts (attempt_id)
);
"""


class JournalError(ValueError):
    """The journal refuses to persist a value it cannot trust later."""


def _identifier(value: str, field_name: str) -> str:
    if not isinstance(value, str) or not ID_PATTERN.fullmatch(value):
        raise JournalError(f"Invalid {field_name}")
    return value


def _digest(value: str, field_name: str) -> str:
    if not isinstance(value, str) or not DIGEST_PATTERN.fullmatch(value):
        raise JournalError(f"Invalid {field_name}")
    return value


def _epoch(value: int, field_name: str) -> int:
    if type(value) is not int or not 0 <= value <= 10 ** 12:
        raise JournalError(f"Invalid {field_name}")
    return value


def _holder(value: str) -> str:
    if not isinstance(value, str) or not HOLDER_PATTERN.fullmatch(value):
        raise JournalError("Invalid executor")
    return value


def _reason(value: str) -> str:
    if not isinstance(value, str) or len(value) > 512:
        raise JournalError("Invalid reason")
    return value


def _stored_path(value: str) -> str:
    """Keep only a relative path: the journal must never hold an absolute local path."""
    if not isinstance(value, str) or not 1 <= len(value) <= 512 or value.startswith(("/", "\\")):
        raise JournalError("Invalid stored path")
    parts = Path(value.replace("\\", "/")).parts
    if not parts or any(part in ("..", ".", "/") for part in parts):
        raise JournalError("Invalid stored path")
    return "/".join(parts)


@dataclass(frozen=True)
class Entry:
    attempt_id: str
    work_item_id: str
    wake_input_hash: str
    input_hash: str | None
    requirement_hash: str | None
    package_path: str | None
    requirement_path: str | None
    control_epoch: int
    lease_epoch: int
    state: str
    reason: str
    executor: str | None
    created_at: float
    updated_at: float

    @property
    def settled(self) -> bool:
        return self.state in SETTLED

    def view(self) -> dict:
        """Local status for the worker's own HTTP surface: no text, no paths, no tokens."""
        return {"attempt_id": self.attempt_id, "work_item_id": self.work_item_id,
                "state": self.state, "reason": self.reason, "input_hash": self.input_hash,
                "requirement_hash": self.requirement_hash, "control_epoch": self.control_epoch,
                "lease_epoch": self.lease_epoch, "updated_at": self.updated_at}


_ENTRY_FIELDS = frozenset(field.name for field in fields(Entry))


class ExecutionJournal:
    """One small SQLite ledger keyed by attempt_id, safe to share across worker threads."""

    def __init__(self, path: Path, *, clock=time.time):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.clock = clock
        self._lock = threading.RLock()
        self._connection = sqlite3.connect(str(self.path), isolation_level=None,
                                           check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA journal_mode = WAL")
        self._connection.execute("PRAGMA busy_timeout = 5000")
        self._connection.execute("PRAGMA foreign_keys = ON")
        with self._lock:
            self._connection.executescript(_SCHEMA)

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    # --- reads -----------------------------------------------------------------

    def get(self, attempt_id: str) -> Entry | None:
        attempt_id = _identifier(attempt_id, "attempt_id")
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM collab_attempts WHERE attempt_id = ?", (attempt_id,)).fetchone()
        return None if row is None else self._entry(row)

    def entries(self) -> list[Entry]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT * FROM collab_attempts ORDER BY created_at DESC, attempt_id").fetchall()
        return [self._entry(row) for row in rows]

    def events(self, attempt_id: str, *, limit: int = 50) -> list[dict]:
        attempt_id = _identifier(attempt_id, "attempt_id")
        with self._lock:
            rows = self._connection.execute(
                "SELECT state,reason,created_at FROM collab_events WHERE attempt_id=? "
                "ORDER BY event_id DESC LIMIT ?", (attempt_id, limit)).fetchall()
        return [dict(row) for row in rows]

    def counts(self) -> dict:
        with self._lock:
            rows = self._connection.execute(
                "SELECT state,COUNT(*) AS total FROM collab_attempts GROUP BY state").fetchall()
        return {row["state"]: row["total"] for row in rows}

    # --- writes ----------------------------------------------------------------

    def discover(self, *, attempt_id: str, work_item_id: str, input_hash: str,
                 control_epoch: int) -> tuple[str, Entry]:
        """Record one advisory wake-up and report what the caller should do next.

        ``new`` means this thread saw the attempt first, ``known`` means the journal already
        holds this wake-up or an answer, and ``conflicting`` means the digest or the work item
        does not fit, so only the pull path may decide what this attempt really is.
        """
        attempt_id = _identifier(attempt_id, "attempt_id")
        work_item_id = _identifier(work_item_id, "work_item_id")
        input_hash = _digest(input_hash, "input_hash")
        control_epoch = _epoch(control_epoch, "control_epoch")
        now = self.clock()
        with self._lock:
            cursor = self._connection.execute(
                "INSERT INTO collab_attempts (attempt_id,work_item_id,wake_input_hash,"
                "control_epoch,state,created_at,updated_at) VALUES (?,?,?,?,?,?,?) "
                "ON CONFLICT (attempt_id) DO NOTHING",
                (attempt_id, work_item_id, input_hash, control_epoch, DISCOVERED, now, now))
            if cursor.rowcount == 1:
                self._event(attempt_id, DISCOVERED, "wake accepted", now)
                return "new", self._row(attempt_id)
            existing = self._row(attempt_id)
            if existing.work_item_id != work_item_id:
                self._event(attempt_id, existing.state, "wake does not match the journal", now)
                return "conflicting", existing
            if existing.settled or existing.wake_input_hash == input_hash:
                return "known", existing
            # A different digest for the same id means the wake is stale or forged: the pull
            # path decides, so the attempt goes back to reconciliation instead of executing.
            self._connection.execute(
                "UPDATE collab_attempts SET wake_input_hash=?,state=?,reason=?,updated_at=?,"
                "executor=NULL WHERE attempt_id=?",
                (input_hash, RECONCILING, "wake digest changed", now, attempt_id))
            self._event(attempt_id, RECONCILING, "wake digest changed", now)
            return "conflicting", self._row(attempt_id)

    def record_inputs(self, attempt_id: str, *, input_hash: str, requirement_hash: str,
                      package_path: str, requirement_path: str,
                      control_epoch: int, lease_epoch: int) -> Entry:
        """Persist the verified digests plus the read-only local copies of the frozen inputs."""
        attempt_id = _identifier(attempt_id, "attempt_id")
        digests = (_digest(input_hash, "input_hash"), _digest(requirement_hash, "requirement_hash"))
        stored = (_stored_path(package_path), _stored_path(requirement_path))
        epochs = (_epoch(control_epoch, "control_epoch"), _epoch(lease_epoch, "lease_epoch"))
        now = self.clock()
        with self._lock:
            cursor = self._connection.execute(
                "UPDATE collab_attempts SET input_hash=?,requirement_hash=?,package_path=?,"
                "requirement_path=?,control_epoch=?,lease_epoch=?,state=?,reason=?,"
                "updated_at=? WHERE attempt_id=? AND executor IS NULL AND state IN (?,?,?,?)",
                (*digests, *stored, *epochs, VERIFIED, "", now, attempt_id, *DRIVABLE))
            if cursor.rowcount != 1:
                raise JournalError("Attempt is not waiting for verification")
            self._event(attempt_id, VERIFIED, "frozen inputs verified", now)
            return self._row(attempt_id)

    def claim_execution(self, attempt_id: str, *, executor: str) -> bool:
        """Take the single persistent placeholder that authorises one executor to start."""
        attempt_id = _identifier(attempt_id, "attempt_id")
        holder = _holder(executor)
        now = self.clock()
        with self._lock:
            cursor = self._connection.execute(
                "UPDATE collab_attempts SET executor=?,state=?,updated_at=? "
                "WHERE attempt_id=? AND executor IS NULL AND state=?",
                (holder, STARTED, now, attempt_id, VERIFIED))
            if cursor.rowcount == 1:
                self._event(attempt_id, STARTED, "executor claimed", now)
                return True
            return False

    def note(self, attempt_id: str, state: str, *, reason: str = "",
             control_epoch: int | None = None, lease_epoch: int | None = None) -> Entry:
        """Record what the service said; only claim_execution may mark an attempt as started."""
        if state not in STATES or state == STARTED:
            raise JournalError("Invalid state")
        attempt_id = _identifier(attempt_id, "attempt_id")
        explanation = _reason(reason)
        now = self.clock()
        assignments = ["state=?", "executor=NULL", "updated_at=?"]
        values: list = [state, now]
        if explanation:
            assignments.append("reason=?")
            values.append(explanation)
        for name, value in (("control_epoch", control_epoch), ("lease_epoch", lease_epoch)):
            if value is not None:
                assignments.append(f"{name}=?")
                values.append(_epoch(value, name))
        values.append(attempt_id)
        with self._lock:
            current = self._row(attempt_id)
            if current.settled and current.state != state:
                raise JournalError("A settled attempt cannot change state again")
            self._connection.execute(
                f"UPDATE collab_attempts SET {','.join(assignments)} WHERE attempt_id=?",
                tuple(values))
            self._event(attempt_id, state, explanation, now)
            return self._row(attempt_id)

    def release_execution(self, attempt_id: str, *, executor: str, state: str,
                          reason: str = "") -> bool:
        """Hand the placeholder back, but only for the executor that holds it."""
        if state not in STATES or state == STARTED:
            raise JournalError("Invalid state")
        attempt_id = _identifier(attempt_id, "attempt_id")
        holder = _holder(executor)
        explanation = _reason(reason)
        now = self.clock()
        with self._lock:
            cursor = self._connection.execute(
                "UPDATE collab_attempts SET executor=NULL,state=?,reason=?,updated_at=? "
                "WHERE attempt_id=? AND executor=?",
                (state, explanation, now, attempt_id, holder))
            if cursor.rowcount == 1:
                self._event(attempt_id, state, explanation, now)
                return True
            return False

    def recover(self) -> list[str]:
        """After a restart an in-flight attempt is unknown, never a licence to re-run it."""
        now = self.clock()
        explanation = "process restarted before the result was recorded"
        with self._lock:
            rows = self._connection.execute(
                "SELECT attempt_id FROM collab_attempts WHERE state=? ORDER BY created_at",
                (STARTED,)).fetchall()
            ids = [row["attempt_id"] for row in rows]
            for attempt_id in ids:
                self._connection.execute(
                    "UPDATE collab_attempts SET executor=NULL,state=?,reason=?,updated_at=? "
                    "WHERE attempt_id=?",
                    (RECONCILING, explanation, now, attempt_id))
                self._event(attempt_id, RECONCILING, explanation, now)
        return ids

    # --- internals ---------------------------------------------------------------

    def _event(self, attempt_id: str, state: str, reason: str, now: float) -> None:
        self._connection.execute(
            "INSERT INTO collab_events (attempt_id,state,reason,created_at) VALUES (?,?,?,?)",
            (attempt_id, state, reason, now))

    def _row(self, attempt_id: str) -> Entry:
        row = self._connection.execute(
            "SELECT * FROM collab_attempts WHERE attempt_id = ?", (attempt_id,)).fetchone()
        if row is None:
            raise JournalError("Attempt is not journaled")
        return self._entry(row)

    @staticmethod
    def _entry(row: sqlite3.Row) -> Entry:
        return Entry(**{name: row[name] for name in row.keys() if name in _ENTRY_FIELDS})
