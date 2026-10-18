"""A verified snapshot of the one file this service keeps its whole durable story in.

Section 15.2 asks a backup to record the schema version and the check results for the original
texts and the delivered files, because a copy nobody verified is a way to lose the data twice.
Requirements, artifact bytes and the audit trail all live in this single SQLite file, so the
archive is one online copy — which is exactly why the copy is read back before it is named.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
from pathlib import Path
import sqlite3
import time

from .database import Database, SCHEMA_VERSION
from .models import InvalidState

_COUNTED = ("projects", "requirements", "requirement_revisions", "planning_proposals", "plans", "work_items",
            "attempts", "attempt_artifacts", "collaboration_audit")


def manifest_path(backup: Path) -> Path:
    return Path(backup).with_name(Path(backup).name + ".manifest.json")


def snapshot(source: Path, target: Path, *, clock=time.time) -> dict:
    """Copy the live database and verify the copy; nothing is promoted until it checks out."""
    source, target = Path(source), Path(target)
    if not source.exists():
        raise InvalidState("There is no database to back up yet")
    if not target.parent.is_dir():
        raise InvalidState("The backup directory does not exist")
    record = manifest_path(target)
    if target.exists() or record.exists():
        # An archive that is quietly replaced is an archive that was never there.
        raise InvalidState("Refusing to overwrite an existing backup")
    working = target.with_name(target.name + ".part")
    if working.exists():
        raise InvalidState("An interrupted backup is still sitting on that name")
    try:
        # A read-only connection that refuses a database this program does not understand: a
        # foreign or half-migrated file must not become an archive anyone trusts later.
        with Database(source).read() as origin:
            destination = sqlite3.connect(working)
            try:
                origin.backup(destination)
            finally:
                destination.close()
        report = _verify(working)
        working.replace(target)
    finally:
        working.unlink(missing_ok=True)
    manifest = {"schema_version": SCHEMA_VERSION, "created_at": clock(),
                "backup": {"name": target.name, "size_bytes": target.stat().st_size,
                           "sha256": _digest(target)},
                **report}
    record.write_text(json.dumps(manifest, sort_keys=True) + "\n", encoding="utf-8")
    return manifest


def _digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _verify(path: Path) -> dict:
    """Re-open the copy and check the facts the archive exists to preserve."""
    with Database(path).read() as conn:
        if conn.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise InvalidState("The backup copy fails SQLite's integrity check")
        counts = {table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                  for table in _COUNTED}
        texts = 0
        for row in conn.execute("SELECT content, content_hash FROM requirement_revisions"):
            if hashlib.sha256(row["content"].encode("utf-8")).hexdigest() != row["content_hash"]:
                raise InvalidState("A stored requirement text no longer matches its digest")
            texts += 1
        files = 0
        for row in conn.execute("SELECT content, sha256, size_bytes FROM attempt_artifacts"):
            content = bytes(row["content"])
            if hashlib.sha256(content).hexdigest() != row["sha256"] \
                    or len(content) != row["size_bytes"]:
                raise InvalidState("A delivered file no longer matches its digest")
            files += 1
    # The guardrails have to come along with the copy, or a restored archive is a place where the
    # audit trail can be rewritten. This probe writes on purpose, then rolls the write back.
    probed = None
    if counts["collaboration_audit"]:
        # `with connection` only commits; it does not close. An open handle would sit on the
        # half-named file and Windows would refuse to promote it.
        probed = False
        with contextlib.closing(sqlite3.connect(path, isolation_level=None)) as conn:
            conn.execute("BEGIN")
            try:
                conn.execute("UPDATE collaboration_audit SET action=action WHERE event_id=("
                             "SELECT event_id FROM collaboration_audit ORDER BY rowid LIMIT 1)")
            except sqlite3.IntegrityError:
                probed = True  # 触发器拒掉了这一行：护栏跟着副本一起回来了。
            finally:
                conn.execute("ROLLBACK")
        if not probed:
            raise InvalidState("The backup copy lost the audit's append-only trigger")
    return {"counts": counts, "verified": {"requirement_texts": texts, "artifacts": files,
                                           "audit_append_only": probed}}
