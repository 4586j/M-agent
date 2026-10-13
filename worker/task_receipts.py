"""Durable, atomic admission for the legacy worker (one worker per work directory)."""
import json
import sqlite3
from contextlib import contextmanager
from dataclasses import asdict
from pathlib import Path


class TaskReceipts:
    def __init__(self, work_dir: str):
        self.path = Path(work_dir) / ".worker-receipts.sqlite3"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as conn:
            conn.execute("""CREATE TABLE IF NOT EXISTS receipts (
                task_id TEXT PRIMARY KEY, input_hash TEXT NOT NULL,
                status TEXT NOT NULL, result TEXT)""")

    def connect(self):
        return sqlite3.connect(self.path, timeout=10)

    @contextmanager
    def connection(self):
        conn = self.connect()
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def recover_interrupted(self) -> int:
        """Boot recovery: each work directory must belong to one live worker."""
        with self.connection() as conn:
            rows = conn.execute("SELECT task_id FROM receipts WHERE status='running'").fetchall()
            for (task_id,) in rows:
                result = {"task_id": task_id, "status": "failed",
                    "error": "worker restarted; execution outcome requires reconciliation",
                    "meta": {"reconciliation_required": True}}
                conn.execute("UPDATE receipts SET status='failed', result=? WHERE task_id=?",
                             (json.dumps(result), task_id))
        return len(rows)

    def claim(self, task_id: str, input_hash: str, capacity: int) -> str:
        conn = self.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT input_hash FROM receipts WHERE task_id=?",
                               (task_id,)).fetchone()
            if row is not None:
                return "duplicate" if row[0] == input_hash else "conflict"
            active = conn.execute("SELECT count(*) FROM receipts WHERE status='running'").fetchone()[0]
            if active >= capacity:
                return "busy"
            conn.execute("INSERT INTO receipts VALUES (?, ?, 'running', NULL)",
                         (task_id, input_hash))
            conn.commit()
            return "accepted"
        finally:
            conn.close()

    def finish(self, result) -> None:
        with self.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            old = conn.execute("SELECT status, result FROM receipts WHERE task_id=?", (result.task_id,)).fetchone()
            if old is None:
                return
            previous = json.loads(old[1]) if old[1] else {}
            if old[0] != "running":
                if old[0] != "cancelled" or (previous.get("meta") or {}).get("execution_finished") is not False:
                    return
                result.status = "cancelled"
            conn.execute("UPDATE receipts SET status=?, result=? WHERE task_id=?",
                         (result.status, json.dumps(asdict(result), ensure_ascii=False), result.task_id))

    def get(self, task_id: str) -> dict | None:
        with self.connection() as conn:
            row = conn.execute("SELECT status, result FROM receipts WHERE task_id=?",
                               (task_id,)).fetchone()
        if row is None:
            return None
        return json.loads(row[1]) if row[1] else {
            "task_id": task_id, "status": row[0], "meta": {"reconciliation_required": True}}
