"""持久化执行日志（durable run journal）。

单机可靠性的「安全恢复」底座：用标准库 SQLite 在本地磁盘记录任务、执行尝试、
事件、审批裁决与工具副作用，使进程重启后能对账（reconcile）、对准入做饱和控制、
并拒绝重放结果未知的外部副作用。

设计约束（对应可靠性方案 Slice 3）：
- WAL + busy_timeout + schema 版本 + 事务化 compare-and-set 迁移；跨进程/线程安全。
- 时间戳一律存「绝对墙钟 epoch 秒（REAL）」：截止点/审批过期持久化后，重启不会
  重置额度（进程内再用单调时钟换算剩余时间）。
- 本模块是「附加式」底座：默认不接管既有 JSON TaskStore 的权威写入，仅在显式
  rollout 开关下作为本地执行的权威日志；历史 JSON 通过幂等导入投影进来，可回滚。
- 只重放「读」或显式幂等的操作；结果未知的外部副作用标记为 unknown、挂起对账，
  绝不盲目自动重放。

线程模型：单连接（check_same_thread=False）+ 实例级 RLock 串行化所有写路径；
显式 BEGIN IMMEDIATE / COMMIT / ROLLBACK 管理事务，保证「读-判定-写」原子。
"""
from __future__ import annotations

import json
import shutil
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable

# ---- schema 版本：结构演进时递增，_init_schema 校验并拒绝不兼容的旧库 ----
SCHEMA_VERSION = 1

# 任务状态集合（与 JSON TaskStore 语义对齐，另加 interrupted 表示重启对账出的
# 中断态：既非成功也非显式取消/失败，等待重派或人工裁决）。
TASK_STATUSES = ("pending", "running", "awaiting_approval", "dispatching",
                 "done", "failed", "lost", "cancelled", "interrupted")
# 任务终态：不可再被收尾写入覆盖（取消不可覆盖等保证依赖它）。
TASK_TERMINAL = ("done", "failed", "cancelled")

# 执行尝试状态：running=在飞；done/failed/cancelled=终态；interrupted=重启对账出。
ATTEMPT_ACTIVE = ("running",)
ATTEMPT_TERMINAL = ("done", "failed", "cancelled")

# 工具副作用类别：read/idempotent 可安全重放；external 结果未知时挂起对账。
EFFECT_KINDS = ("read", "idempotent", "external")
EFFECT_STATES = ("started", "committed", "unknown", "failed")
# 可安全重放的副作用类别（读、显式幂等）。
REPLAY_SAFE_KINDS = ("read", "idempotent")

# 审批裁决。
APPROVAL_DECISIONS = ("pending", "approved", "denied")


def _new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


def _dumps(obj: Any) -> str:
    """把 dict/list 序列化为 TEXT 存储；None → 空串（读取时还原为 None）。"""
    if obj is None:
        return ""
    return json.dumps(obj, ensure_ascii=False)


def _loads(raw: Any) -> Any:
    """TEXT → dict/list；空串/损坏 → None（幂等导入与旧数据兼容）。"""
    if raw is None or raw == "":
        return None
    try:
        return json.loads(raw)
    except (ValueError, TypeError):
        return None


@dataclass
class ClaimResult:
    """准入判定结果。admitted=False 时 reason 说明拒绝原因（饱和/串行化冲突）。"""
    admitted: bool
    attempt_id: str | None = None
    reason: str = ""


@dataclass
class ImportReport:
    """JSON 幂等导入报告。repeatable：同一批数据导入两次结果一致、无重复。"""
    total: int = 0
    imported: int = 0
    skipped: int = 0
    backup_dir: str = ""
    errors: list = field(default_factory=list)


_SCHEMA_DDL = """
CREATE TABLE IF NOT EXISTS schema_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS tasks (
    task_id       TEXT PRIMARY KEY,
    role          TEXT NOT NULL DEFAULT '',
    prompt        TEXT NOT NULL DEFAULT '',
    workspace_key TEXT NOT NULL DEFAULT '',
    session_key   TEXT NOT NULL DEFAULT '',
    status        TEXT NOT NULL DEFAULT 'pending',
    created_at    REAL NOT NULL,
    updated_at    REAL NOT NULL,
    deadline_at   REAL,
    output        TEXT,
    error         TEXT,
    verdict       TEXT NOT NULL DEFAULT '',
    meta          TEXT NOT NULL DEFAULT '',
    trace_id      TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);

CREATE TABLE IF NOT EXISTS attempts (
    attempt_id  TEXT PRIMARY KEY,
    task_id     TEXT NOT NULL,
    seq         INTEGER NOT NULL DEFAULT 1,
    status      TEXT NOT NULL DEFAULT 'running',
    worker      TEXT NOT NULL DEFAULT '',
    host_pid    INTEGER,
    claimed_at  REAL NOT NULL,
    finished_at REAL,
    result      TEXT NOT NULL DEFAULT '',
    FOREIGN KEY(task_id) REFERENCES tasks(task_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_attempts_task ON attempts(task_id);
CREATE INDEX IF NOT EXISTS idx_attempts_status ON attempts(status);

CREATE TABLE IF NOT EXISTS events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id    TEXT NOT NULL,
    attempt_id TEXT NOT NULL DEFAULT '',
    ts         REAL NOT NULL,
    type       TEXT NOT NULL DEFAULT '',
    payload    TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_events_task ON events(task_id, id);

CREATE TABLE IF NOT EXISTS approvals (
    approval_id TEXT PRIMARY KEY,
    task_id     TEXT NOT NULL DEFAULT '',
    attempt_id  TEXT NOT NULL DEFAULT '',
    action_id   TEXT NOT NULL,
    args_hash   TEXT NOT NULL DEFAULT '',
    resource    TEXT NOT NULL DEFAULT '',
    decision    TEXT NOT NULL DEFAULT 'pending',
    decided_by  TEXT NOT NULL DEFAULT '',
    created_at  REAL NOT NULL,
    decided_at  REAL,
    expires_at  REAL
);
CREATE INDEX IF NOT EXISTS idx_approvals_action ON approvals(action_id, args_hash);

CREATE TABLE IF NOT EXISTS effects (
    effect_id  TEXT PRIMARY KEY,
    task_id    TEXT NOT NULL DEFAULT '',
    attempt_id TEXT NOT NULL DEFAULT '',
    action_id  TEXT NOT NULL,
    kind       TEXT NOT NULL DEFAULT 'external',
    args_hash  TEXT NOT NULL DEFAULT '',
    state      TEXT NOT NULL DEFAULT 'started',
    outcome    TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_effects_task ON effects(task_id, state);
"""


class RunStore:
    """SQLite 持久化执行日志。

    参数：
    - db_path: SQLite 文件路径（默认建议 <work_dir>/runtime.sqlite3）。
    - busy_timeout_ms: 写锁等待上限（跨进程/线程争用时不立即失败）。
    - max_active: 全局并发在飞尝试上限（默认 1，即单活动执行；可配置放宽）。
    - now_fn: 可注入的墙钟（返回 epoch 秒），供测试精确控制截止/过期/对账。
    """

    SCHEMA_VERSION = SCHEMA_VERSION

    def __init__(self, db_path: str | Path, *, busy_timeout_ms: int = 5000,
                 max_active: int = 1,
                 now_fn: Callable[[], float] | None = None):
        self.db_path = str(db_path)
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._busy_timeout_ms = max(0, int(busy_timeout_ms))
        self._max_active = max(1, int(max_active))
        self._now_fn = now_fn or time.time
        self._lock = threading.RLock()
        # isolation_level=None → 自动提交模式，事务由本类显式 BEGIN/COMMIT 管理。
        self._conn = sqlite3.connect(
            self.db_path,
            timeout=self._busy_timeout_ms / 1000.0,
            check_same_thread=False,
            isolation_level=None,
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.execute(f"PRAGMA busy_timeout={self._busy_timeout_ms}")
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        # NORMAL 在 WAL 下兼顾持久性与写性能（断电仅可能丢最后未 checkpoint 的事务）。
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._init_schema()

    # ---- 连接 / 事务 / schema ----

    def _now(self) -> float:
        return float(self._now_fn())

    @contextmanager
    def _txn(self, immediate: bool = True):
        """显式事务上下文：immediate 先取写锁，避免 CAS 的读-写窗口竞态。"""
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
            try:
                yield self._conn
                self._conn.execute("COMMIT")
            except BaseException:
                try:
                    self._conn.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                raise

    def _init_schema(self) -> None:
        with self._lock:
            self._conn.executescript(_SCHEMA_DDL)
            row = self._conn.execute(
                "SELECT value FROM schema_meta WHERE key='schema_version'"
            ).fetchone()
            if row is None:
                self._conn.execute(
                    "INSERT INTO schema_meta(key, value) VALUES('schema_version', ?)",
                    (str(SCHEMA_VERSION),))
            else:
                found = int(row["value"])
                if found > SCHEMA_VERSION:
                    raise RuntimeError(
                        f"run_store schema too new: db={found} code={SCHEMA_VERSION}")

    @property
    def schema_version(self) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT value FROM schema_meta WHERE key='schema_version'"
            ).fetchone()
        return int(row["value"]) if row else 0

    def close(self) -> None:
        with self._lock:
            try:
                self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            except sqlite3.Error:
                pass
            self._conn.close()

    def __enter__(self) -> "RunStore":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # ---- 任务 ----

    def create_task(self, task_id: str, role: str, prompt: str, *,
                    workspace_key: str = "", session_key: str = "",
                    status: str = "pending", deadline_at: float | None = None,
                    trace_id: str = "", meta: dict | None = None,
                    now: float | None = None) -> dict:
        if status not in TASK_STATUSES:
            raise ValueError(f"invalid task status: {status}")
        ts = self._now() if now is None else float(now)
        with self._txn():
            self._conn.execute(
                """INSERT INTO tasks(task_id, role, prompt, workspace_key,
                       session_key, status, created_at, updated_at, deadline_at,
                       meta, trace_id)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (task_id, role, prompt, workspace_key, session_key, status,
                 ts, ts, deadline_at, _dumps(meta), trace_id))
        return self.get_task(task_id)  # type: ignore[return-value]

    def get_task(self, task_id: str) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone()
        return self._row_to_task(row) if row else None

    def list_tasks(self, status: str | None = None) -> list[dict]:
        with self._lock:
            if status is None:
                rows = self._conn.execute(
                    "SELECT * FROM tasks ORDER BY created_at").fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT * FROM tasks WHERE status=? ORDER BY created_at",
                    (status,)).fetchall()
        return [self._row_to_task(r) for r in rows]

    def cas_task_status(self, task_id: str, expected: Iterable[str],
                        to_status: str, **fields) -> bool:
        """原子 compare-and-set：仅当当前状态在 expected 内才迁移并写入 fields。

        「取消不可覆盖」等保证依赖它——把 expected 限定为非终态即可让迟到的收尾
        写入对已 cancelled/done/failed 的任务返回 False。支持同时写
        output/error/verdict/meta/deadline_at/updated_at。
        """
        if to_status not in TASK_STATUSES:
            raise ValueError(f"invalid task status: {to_status}")
        expected = tuple(expected)
        if not expected:
            raise ValueError("expected must be non-empty")
        sets = ["status=?", "updated_at=?"]
        set_vals: list[Any] = [to_status, self._now()]
        for key in ("output", "error", "deadline_at"):
            if key in fields:
                sets.append(f"{key}=?")
                set_vals.append(fields[key])
        if "verdict" in fields:
            sets.append("verdict=?")
            set_vals.append(_dumps(fields["verdict"]))
        if "meta" in fields:
            sets.append("meta=?")
            set_vals.append(_dumps(fields["meta"]))
        placeholders = ",".join("?" * len(expected))
        # 参数顺序：SET 值... , WHERE task_id , status IN (expected...)
        params = set_vals + [task_id] + list(expected)
        with self._txn():
            cur = self._conn.execute(
                f"UPDATE tasks SET {','.join(sets)} "
                f"WHERE task_id=? AND status IN ({placeholders})",
                params)
            return cur.rowcount == 1

    def _row_to_task(self, row: sqlite3.Row) -> dict:
        d = dict(row)
        d["verdict"] = _loads(d.get("verdict"))
        d["meta"] = _loads(d.get("meta")) or {}
        return d

    # ---- 执行尝试 / 准入 ----

    def try_claim(self, task_id: str, *, workspace_key: str | None = None,
                  session_key: str | None = None, worker: str = "",
                  attempt_id: str | None = None,
                  now: float | None = None) -> ClaimResult:
        """事务化「准入 + 认领」：满足全局并发上限与按 workspace/session 串行化时，
        创建一个 running 尝试并把任务置为 running；否则显式拒绝（不静默排队）。

        - 饱和：在飞尝试数 >= max_active → reason='saturated'。
        - 串行化：同 workspace_key / session_key 已有在飞尝试 → reason='busy:*'。
        workspace_key/session_key 缺省时回落到任务自身记录的键。
        """
        ts = self._now() if now is None else float(now)
        aid = attempt_id or _new_id("att")
        with self._txn():
            trow = self._conn.execute(
                "SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone()
            if trow is None:
                return ClaimResult(False, None, "no_task")
            if trow["status"] in TASK_TERMINAL:
                return ClaimResult(False, None, f"terminal:{trow['status']}")
            wkey = trow["workspace_key"] if workspace_key is None else workspace_key
            skey = trow["session_key"] if session_key is None else session_key

            active = self._conn.execute(
                f"SELECT COUNT(*) AS c FROM attempts WHERE status IN "
                f"({','.join('?' * len(ATTEMPT_ACTIVE))})",
                ATTEMPT_ACTIVE).fetchone()["c"]
            if active >= self._max_active:
                return ClaimResult(False, None, "saturated")

            if wkey:
                clash = self._conn.execute(
                    """SELECT a.attempt_id FROM attempts a JOIN tasks t
                       ON a.task_id=t.task_id
                       WHERE a.status IN (%s) AND t.workspace_key=?
                       LIMIT 1""" % ",".join("?" * len(ATTEMPT_ACTIVE)),
                    (*ATTEMPT_ACTIVE, wkey)).fetchone()
                if clash:
                    return ClaimResult(False, None, "busy:workspace")
            if skey:
                clash = self._conn.execute(
                    """SELECT a.attempt_id FROM attempts a JOIN tasks t
                       ON a.task_id=t.task_id
                       WHERE a.status IN (%s) AND t.session_key=?
                       LIMIT 1""" % ",".join("?" * len(ATTEMPT_ACTIVE)),
                    (*ATTEMPT_ACTIVE, skey)).fetchone()
                if clash:
                    return ClaimResult(False, None, "busy:session")

            seq = (self._conn.execute(
                "SELECT COALESCE(MAX(seq),0)+1 AS s FROM attempts WHERE task_id=?",
                (task_id,)).fetchone()["s"])
            self._conn.execute(
                """INSERT INTO attempts(attempt_id, task_id, seq, status, worker,
                       claimed_at)
                   VALUES(?,?,?,?,?,?)""",
                (aid, task_id, seq, "running", worker, ts))
            self._conn.execute(
                "UPDATE tasks SET status='running', updated_at=? WHERE task_id=?",
                (ts, task_id))
        return ClaimResult(True, aid, "")

    def get_attempt(self, attempt_id: str) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM attempts WHERE attempt_id=?", (attempt_id,)).fetchone()
        return self._row_to_attempt(row) if row else None

    def list_attempts(self, task_id: str) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM attempts WHERE task_id=? ORDER BY seq",
                (task_id,)).fetchall()
        return [self._row_to_attempt(r) for r in rows]

    def count_active(self) -> int:
        with self._lock:
            return self._conn.execute(
                f"SELECT COUNT(*) AS c FROM attempts WHERE status IN "
                f"({','.join('?' * len(ATTEMPT_ACTIVE))})",
                ATTEMPT_ACTIVE).fetchone()["c"]

    def set_attempt_host(self, attempt_id: str, host_pid: int | None) -> bool:
        """登记/清除尝试所属的子进程 pid（供 owned-process 清理，只清理自己拥有的）。"""
        with self._txn():
            cur = self._conn.execute(
                "UPDATE attempts SET host_pid=? WHERE attempt_id=?",
                (host_pid, attempt_id))
            return cur.rowcount == 1

    def finish_attempt(self, attempt_id: str, status: str, *,
                       result: Any = None, now: float | None = None) -> bool:
        """把在飞尝试落到终态（CAS：仅当当前为 running/claimed 才写）。返回是否命中。"""
        if status not in ATTEMPT_TERMINAL and status != "interrupted":
            raise ValueError(f"invalid attempt status: {status}")
        ts = self._now() if now is None else float(now)
        with self._txn():
            cur = self._conn.execute(
                """UPDATE attempts SET status=?, finished_at=?, result=?, host_pid=NULL
                   WHERE attempt_id=? AND status IN (%s)"""
                % ",".join("?" * len(ATTEMPT_ACTIVE)),
                (status, ts, _dumps(result), attempt_id, *ATTEMPT_ACTIVE))
            return cur.rowcount == 1

    def reconcile_interrupted(self, *, now: float | None = None) -> list[str]:
        """启动对账：把仍处于在飞态的尝试标记为 interrupted（进程已亡，无主），
        其对应任务若仍 running 则回退到 interrupted（可重派/待裁决），返回受影响 task_id。

        这是「崩溃后重启」的安全基线：不假设在飞尝试还活着，也不自动重放其副作用
        （副作用重放安全由 effects 表的 unknown 挂起机制单独保证）。
        """
        ts = self._now() if now is None else float(now)
        affected: list[str] = []
        with self._txn():
            rows = self._conn.execute(
                f"SELECT attempt_id, task_id FROM attempts WHERE status IN "
                f"({','.join('?' * len(ATTEMPT_ACTIVE))})",
                ATTEMPT_ACTIVE).fetchall()
            for r in rows:
                self._conn.execute(
                    "UPDATE attempts SET status='interrupted', finished_at=?, "
                    "host_pid=NULL WHERE attempt_id=?",
                    (ts, r["attempt_id"]))
                task_id = r["task_id"]
                cur = self._conn.execute(
                    "UPDATE tasks SET status='interrupted', updated_at=? "
                    "WHERE task_id=? AND status='running'",
                    (ts, task_id))
                # 只有「running 状态成功回退」的任务计入（rowcount=1）；
                # 已是终态/取消的任务不视为受影响（清理死条件分支）
                if cur.rowcount == 1 and task_id not in affected:
                    affected.append(task_id)
        return affected

    def _row_to_attempt(self, row: sqlite3.Row) -> dict:
        d = dict(row)
        d["result"] = _loads(d.get("result"))
        return d

    # ---- 事件账本 ----

    def append_event(self, task_id: str, type: str, payload: Any = None, *,
                     attempt_id: str = "", now: float | None = None) -> int:
        ts = self._now() if now is None else float(now)
        with self._txn():
            cur = self._conn.execute(
                "INSERT INTO events(task_id, attempt_id, ts, type, payload) "
                "VALUES(?,?,?,?,?)",
                (task_id, attempt_id, ts, type, _dumps(payload)))
            return int(cur.lastrowid)

    def list_events(self, task_id: str, *, limit: int | None = None) -> list[dict]:
        sql = "SELECT * FROM events WHERE task_id=? ORDER BY id"
        args: tuple = (task_id,)
        if limit is not None:
            sql += " LIMIT ?"
            args = (task_id, int(limit))
        with self._lock:
            rows = self._conn.execute(sql, args).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["payload"] = _loads(d.get("payload"))
            out.append(d)
        return out

    # ---- 审批：绑定动作身份 + 过期 ----

    def record_approval(self, action_id: str, *, args_hash: str = "",
                        resource: str = "", task_id: str = "",
                        attempt_id: str = "", decision: str = "pending",
                        decided_by: str = "", ttl: float | None = None,
                        now: float | None = None) -> dict:
        """登记一条审批（pending）或裁决（approved/denied）。

        审批绑定到 (action_id, args_hash, resource, attempt_id)：只有「同一动作、
        同一归一化参数、同一目标资源、同一尝试」的裁决才可复用；ttl 非空时写入
        expires_at，过期审批不再有效（stale approval 拒绝）。
        """
        if decision not in APPROVAL_DECISIONS:
            raise ValueError(f"invalid decision: {decision}")
        ts = self._now() if now is None else float(now)
        aid = _new_id("apr")
        expires_at = (ts + float(ttl)) if ttl else None
        decided_at = ts if decision in ("approved", "denied") else None
        with self._txn():
            self._conn.execute(
                """INSERT INTO approvals(approval_id, task_id, attempt_id, action_id,
                       args_hash, resource, decision, decided_by, created_at,
                       decided_at, expires_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (aid, task_id, attempt_id, action_id, args_hash, resource,
                 decision, decided_by, ts, decided_at, expires_at))
            row = self._conn.execute(
                "SELECT * FROM approvals WHERE approval_id=?", (aid,)).fetchone()
        return dict(row)

    def get_valid_approval(self, action_id: str, *, args_hash: str = "",
                           resource: str = "", attempt_id: str | None = None,
                           now: float | None = None) -> dict | None:
        """查一条「仍然有效」的 approved 裁决：动作/参数/资源匹配、未过期，
        （给定 attempt_id 时还须同尝试）。无则返回 None（调用方须重新申请审批）。
        """
        ts = self._now() if now is None else float(now)
        sql = ("SELECT * FROM approvals WHERE action_id=? AND args_hash=? "
               "AND resource=? AND decision='approved' "
               "AND (expires_at IS NULL OR expires_at > ?)")
        args: list[Any] = [action_id, args_hash, resource, ts]
        if attempt_id is not None:
            sql += " AND attempt_id=?"
            args.append(attempt_id)
        sql += " ORDER BY created_at DESC LIMIT 1"
        with self._lock:
            row = self._conn.execute(sql, args).fetchone()
        return dict(row) if row else None

    def latest_approval(self, action_id: str, *, args_hash: str | None = None,
                        resource: str | None = None,
                        attempt_id: str | None = None) -> dict | None:
        """返回匹配给定过滤维度（None=不过滤该维度）的最近一条审批，不判断
        decision/是否过期。供策略层区分「从未申请 / 待裁决 / 已拒绝 / 已过期 /
        参数漂移」等精确拒绝原因（get_valid_approval 只回答「是否有有效批准」）。
        """
        sql = "SELECT * FROM approvals WHERE action_id=?"
        args: list[Any] = [action_id]
        if args_hash is not None:
            sql += " AND args_hash=?"
            args.append(args_hash)
        if resource is not None:
            sql += " AND resource=?"
            args.append(resource)
        if attempt_id is not None:
            sql += " AND attempt_id=?"
            args.append(attempt_id)
        sql += " ORDER BY created_at DESC, rowid DESC LIMIT 1"
        with self._lock:
            row = self._conn.execute(sql, args).fetchone()
        return dict(row) if row else None

    # ---- 工具副作用账本：崩溃后不盲目重放 ----

    def record_effect_start(self, action_id: str, kind: str, *,
                            args_hash: str = "", task_id: str = "",
                            attempt_id: str = "",
                            now: float | None = None) -> dict:
        """在执行一个有副作用的动作「之前」登记 started。

        崩溃发生在 started 与 committed 之间时，该记录停留在 started → 重启对账视为
        结果未知（unknown），非 read/idempotent 类别绝不自动重放。
        """
        if kind not in EFFECT_KINDS:
            raise ValueError(f"invalid effect kind: {kind}")
        ts = self._now() if now is None else float(now)
        eid = _new_id("eff")
        with self._txn():
            self._conn.execute(
                """INSERT INTO effects(effect_id, task_id, attempt_id, action_id,
                       kind, args_hash, state, created_at, updated_at)
                   VALUES(?,?,?,?,?,?,'started',?,?)""",
                (eid, task_id, attempt_id, action_id, kind, args_hash, ts, ts))
            row = self._conn.execute(
                "SELECT * FROM effects WHERE effect_id=?", (eid,)).fetchone()
        return dict(row)

    def set_effect_state(self, effect_id: str, state: str, outcome: Any = None,
                         *, now: float | None = None) -> bool:
        if state not in EFFECT_STATES:
            raise ValueError(f"invalid effect state: {state}")
        ts = self._now() if now is None else float(now)
        with self._txn():
            cur = self._conn.execute(
                "UPDATE effects SET state=?, outcome=?, updated_at=? WHERE effect_id=?",
                (state, _dumps(outcome), ts, effect_id))
            return cur.rowcount == 1

    def get_effect(self, effect_id: str) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM effects WHERE effect_id=?", (effect_id,)).fetchone()
        return self._row_to_effect(row) if row else None

    def list_unresolved_effects(self, *, task_id: str | None = None) -> list[dict]:
        """列出结果未知/未提交的副作用（started/unknown）——需人工或对账裁决，
        不可自动重放（除非 kind 属 read/idempotent）。"""
        sql = "SELECT * FROM effects WHERE state IN ('started','unknown')"
        args: tuple = ()
        if task_id is not None:
            sql += " AND task_id=?"
            args = (task_id,)
        sql += " ORDER BY created_at"
        with self._lock:
            rows = self._conn.execute(sql, args).fetchall()
        return [self._row_to_effect(r) for r in rows]

    @staticmethod
    def is_replay_safe(effect: dict) -> bool:
        """副作用是否可安全重放：仅读或显式幂等类别。"""
        return effect.get("kind") in REPLAY_SAFE_KINDS

    def _row_to_effect(self, row: sqlite3.Row) -> dict:
        d = dict(row)
        d["outcome"] = _loads(d.get("outcome"))
        return d

    # ---- 幂等 JSON 导入（历史 TaskStore 投影，可回滚）----

    def import_json_records(self, records: Iterable[dict]) -> ImportReport:
        """把 JSON TaskRecord（asdict 形状）幂等 upsert 进 tasks 表。

        repeatable：同一批数据导入多次结果一致、不产生重复行（按 task_id 冲突更新）。
        已存在的终态任务不被非终态覆盖（避免历史结论被回退）。
        """
        report = ImportReport()
        for rec in records:
            report.total += 1
            try:
                task_id = rec.get("task_id")
                if not task_id:
                    report.skipped += 1
                    report.errors.append("missing task_id")
                    continue
                self._upsert_task_from_json(rec)
                report.imported += 1
            except Exception as e:  # noqa: BLE001 - 单条失败不拖垮整批导入
                report.skipped += 1
                report.errors.append(f"{rec.get('task_id')}: {e}")
        return report

    def _upsert_task_from_json(self, rec: dict) -> None:
        def _to_epoch(v: Any) -> float:
            try:
                return float(v)
            except (TypeError, ValueError):
                return self._now()

        status = rec.get("status") or "pending"
        if status not in TASK_STATUSES:
            status = "interrupted" if status else "pending"
        created = _to_epoch(rec.get("created_at"))
        updated = _to_epoch(rec.get("updated_at"))
        with self._txn():
            existing = self._conn.execute(
                "SELECT status FROM tasks WHERE task_id=?",
                (rec["task_id"],)).fetchone()
            if existing and existing["status"] in TASK_TERMINAL \
                    and status not in TASK_TERMINAL:
                # 历史终态不可被非终态回退（幂等 + 单调）。
                return
            self._conn.execute(
                """INSERT INTO tasks(task_id, role, prompt, workspace_key,
                       session_key, status, created_at, updated_at, output,
                       error, verdict, meta, trace_id)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(task_id) DO UPDATE SET
                       role=excluded.role, prompt=excluded.prompt,
                       status=excluded.status, updated_at=excluded.updated_at,
                       output=excluded.output, error=excluded.error,
                       verdict=excluded.verdict, meta=excluded.meta,
                       trace_id=excluded.trace_id""",
                (rec["task_id"], rec.get("role", ""), rec.get("prompt", ""),
                 rec.get("workspace_key", "") or rec.get("task_id", ""),
                 rec.get("session_key", ""), status, created, updated,
                 rec.get("output"), rec.get("error"),
                 _dumps(rec.get("verdict")), _dumps(rec.get("meta") or {}),
                 rec.get("trace_id", "") or ""))

    def import_json_dir(self, src_dir: str | Path, *, backup: bool = True
                        ) -> ImportReport:
        """读取目录下所有 *.json 任务记录并幂等导入；backup=True 时先整体备份一份。

        备份目录固定为 <db_path>.json-backup（存在则不重复备份），保证「保留历史
        JSON + 可回滚」，且不删除源数据。
        """
        src = Path(src_dir)
        report = ImportReport()
        if not src.is_dir():
            report.errors.append(f"not a directory: {src}")
            return report
        if backup:
            bdir = Path(self.db_path + ".json-backup")
            if not bdir.exists():
                try:
                    shutil.copytree(src, bdir)
                    report.backup_dir = str(bdir)
                except OSError as e:
                    report.errors.append(f"backup failed: {e}")
            else:
                report.backup_dir = str(bdir)
        records = []
        for p in sorted(src.glob("*.json")):
            try:
                records.append(json.loads(p.read_text(encoding="utf-8")))
            except (OSError, ValueError) as e:
                report.errors.append(f"{p.name}: {e}")
        sub = self.import_json_records(records)
        report.total += sub.total
        report.imported += sub.imported
        report.skipped += sub.skipped
        report.errors.extend(sub.errors)
        return report


def default_journal_path(work_dir: str | Path) -> str:
    """方案约定的默认日志路径：<work_dir>/runtime.sqlite3。"""
    return str(Path(work_dir) / "runtime.sqlite3")


def open_journal(cfg, *, work_dir: str | Path | None = None,
                 json_import_dir: str | Path | None = None,
                 now_fn: Callable[[], float] | None = None) -> "RunStore | None":
    """按 rollout 开关装配本地持久化日志（方案 Slice 3「behind an explicit flag」）。

    - cfg.durable_journal_enabled 为假（默认）→ 返回 None：完全走既有 JSON 权威
      路径，零行为变化，不创建任何 SQLite 文件。
    - 为真 → 打开/初始化 RunStore（journal_path 缺省派生自 work_dir/cfg.work_dir），
      启动即对在飞尝试做对账（reconcile_interrupted），并把历史 JSON 幂等投影
      进来（保留 JSON 供回滚/导出，不删源、不做双权威写入）。

    日志打开失败绝不拖垮上层：捕获异常并返回 None（降级回 JSON 路径），
    避免因持久化底座问题阻断任务执行。
    """
    if not getattr(cfg, "durable_journal_enabled", False):
        return None
    try:
        wd = work_dir or getattr(cfg, "work_dir", "") or "."
        db_path = getattr(cfg, "journal_path", "") or default_journal_path(wd)
        max_active = int(getattr(cfg, "max_active_executions", 1) or 1)
        store = RunStore(db_path, max_active=max_active, now_fn=now_fn)
        affected = store.reconcile_interrupted()
        if affected:
            import logging
            logging.getLogger(__name__).info(
                "journal reconcile: %d interrupted task(s): %s",
                len(affected), affected)
        if json_import_dir:
            src = Path(json_import_dir)
            if src.is_dir():
                store.import_json_dir(src)
        return store
    except Exception:  # noqa: BLE001 - 持久化底座故障降级回 JSON，不阻断执行
        import logging
        logging.getLogger(__name__).exception(
            "open_journal failed; falling back to JSON task store")
        return None
