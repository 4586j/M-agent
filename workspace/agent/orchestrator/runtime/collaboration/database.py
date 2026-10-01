"""Explicit, file-backed SQLite lifecycle for the collaboration package."""

from contextlib import contextmanager
from pathlib import Path
import sqlite3
from typing import Iterator

from .models import SchemaVersionError


SCHEMA_VERSION = 1

_SCHEMA_STATEMENTS = (
    """
    CREATE TABLE users (
        user_id TEXT NOT NULL PRIMARY KEY CHECK (length(trim(user_id)) > 0),
        display_name TEXT NOT NULL CHECK (length(trim(display_name)) > 0),
        status TEXT NOT NULL DEFAULT 'active'
            CHECK (length(trim(status)) > 0 AND status IN ('active', 'disabled')),
        version INTEGER NOT NULL DEFAULT 1 CHECK (version > 0)
    )
    """,
    """
    CREATE TABLE projects (
        project_id TEXT NOT NULL PRIMARY KEY CHECK (length(trim(project_id)) > 0),
        name TEXT NOT NULL CHECK (length(trim(name)) > 0),
        owner_id TEXT NOT NULL CHECK (length(trim(owner_id)) > 0),
        status TEXT NOT NULL DEFAULT 'active'
            CHECK (length(trim(status)) > 0 AND status IN ('active', 'archived')),
        version INTEGER NOT NULL DEFAULT 1 CHECK (version > 0),
        FOREIGN KEY (owner_id) REFERENCES users (user_id),
        FOREIGN KEY (project_id, owner_id)
            REFERENCES project_members (project_id, user_id)
            DEFERRABLE INITIALLY DEFERRED
    )
    """,
    """
    CREATE TABLE project_members (
        project_id TEXT NOT NULL CHECK (length(trim(project_id)) > 0),
        user_id TEXT NOT NULL CHECK (length(trim(user_id)) > 0),
        access_role TEXT NOT NULL DEFAULT 'member'
            CHECK (length(trim(access_role)) > 0
                   AND access_role IN ('owner', 'manager', 'reviewer', 'member')),
        status TEXT NOT NULL DEFAULT 'active'
            CHECK (length(trim(status)) > 0 AND status IN ('active', 'removed')),
        version INTEGER NOT NULL DEFAULT 1 CHECK (version > 0),
        PRIMARY KEY (project_id, user_id),
        FOREIGN KEY (project_id) REFERENCES projects (project_id),
        FOREIGN KEY (user_id) REFERENCES users (user_id)
    )
    """,
    """
    CREATE TABLE agents (
        agent_id TEXT NOT NULL PRIMARY KEY CHECK (length(trim(agent_id)) > 0),
        owner_user_id TEXT NOT NULL CHECK (length(trim(owner_user_id)) > 0),
        name TEXT NOT NULL CHECK (length(trim(name)) > 0),
        status TEXT NOT NULL DEFAULT 'active'
            CHECK (length(trim(status)) > 0 AND status IN ('active', 'disabled')),
        version INTEGER NOT NULL DEFAULT 1 CHECK (version > 0),
        UNIQUE (agent_id, owner_user_id),
        FOREIGN KEY (owner_user_id) REFERENCES users (user_id)
    )
    """,
    """
    CREATE TABLE agent_bindings (
        binding_id TEXT NOT NULL PRIMARY KEY CHECK (length(trim(binding_id)) > 0),
        project_id TEXT NOT NULL CHECK (length(trim(project_id)) > 0),
        agent_id TEXT NOT NULL CHECK (length(trim(agent_id)) > 0),
        member_user_id TEXT NOT NULL CHECK (length(trim(member_user_id)) > 0),
        duty TEXT NOT NULL CHECK (length(trim(duty)) > 0),
        persona_version TEXT NOT NULL CHECK (length(trim(persona_version)) > 0),
        allowed_tools TEXT NOT NULL CHECK (length(trim(allowed_tools)) > 0),
        workspace_ref TEXT NOT NULL CHECK (length(trim(workspace_ref)) > 0),
        provider_ref TEXT NOT NULL CHECK (length(trim(provider_ref)) > 0),
        status TEXT NOT NULL DEFAULT 'active'
            CHECK (length(trim(status)) > 0 AND status IN ('active', 'revoked')),
        version INTEGER NOT NULL DEFAULT 1 CHECK (version > 0),
        FOREIGN KEY (project_id, member_user_id)
            REFERENCES project_members (project_id, user_id),
        FOREIGN KEY (agent_id, member_user_id)
            REFERENCES agents (agent_id, owner_user_id)
    )
    """,
    """
    CREATE UNIQUE INDEX agent_bindings_active_project_agent
        ON agent_bindings (project_id, agent_id) WHERE status = 'active'
    """,
    """
    CREATE TABLE binding_revisions (
        binding_id TEXT NOT NULL CHECK (length(trim(binding_id)) > 0),
        version INTEGER NOT NULL CHECK (version > 0),
        snapshot TEXT NOT NULL CHECK (length(trim(snapshot)) > 0),
        PRIMARY KEY (binding_id, version),
        FOREIGN KEY (binding_id) REFERENCES agent_bindings (binding_id)
    )
    """,
    """
    CREATE TRIGGER binding_revisions_no_replace
    BEFORE INSERT ON binding_revisions
    WHEN EXISTS (
        SELECT 1 FROM binding_revisions
        WHERE binding_id = NEW.binding_id AND version = NEW.version
    )
    BEGIN
        SELECT RAISE(ABORT, 'binding revisions are immutable');
    END
    """,
    """
    CREATE TRIGGER binding_revisions_no_update
    BEFORE UPDATE ON binding_revisions
    BEGIN
        SELECT RAISE(ABORT, 'binding revisions are immutable');
    END
    """,
    """
    CREATE TRIGGER binding_revisions_no_delete
    BEFORE DELETE ON binding_revisions
    BEGIN
        SELECT RAISE(ABORT, 'binding revisions are immutable');
    END
    """,
)


def _check_schema_version(connection: sqlite3.Connection) -> None:
    version = connection.execute("PRAGMA user_version").fetchone()[0]
    if version != SCHEMA_VERSION:
        raise SchemaVersionError(
            f"Unsupported collaboration schema version {version}; "
            f"expected {SCHEMA_VERSION}"
        )


class Database:
    """Open a fresh connection per context; construction never touches disk."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)

    @contextmanager
    def _connection(self, mode: str) -> Iterator[sqlite3.Connection]:
        uri = self.path.resolve().as_uri() + f"?mode={mode}"
        connection = sqlite3.connect(uri, uri=True, isolation_level=None)
        try:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("PRAGMA busy_timeout = 5000")
            yield connection
        finally:
            connection.close()

    def initialize(self) -> None:
        """Create a fresh schema atomically, or verify an initialized database."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connection("rwc") as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                version = connection.execute("PRAGMA user_version").fetchone()[0]
                if version == 0:
                    existing_object = connection.execute(
                        "SELECT name FROM sqlite_master "
                        "WHERE name NOT GLOB 'sqlite_*' LIMIT 1"
                    ).fetchone()
                    if existing_object is not None:
                        raise SchemaVersionError(
                            "Refusing to initialize an unversioned database "
                            f"containing an existing object: {existing_object['name']}"
                        )
                    for statement in _SCHEMA_STATEMENTS:
                        connection.execute(statement)
                    connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
                else:
                    _check_schema_version(connection)
                connection.commit()
            except BaseException:
                connection.rollback()
                raise

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """Write to an initialized database with an immediate transaction."""
        with self._connection("rw") as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                _check_schema_version(connection)
                yield connection
                connection.commit()
            except BaseException:
                connection.rollback()
                raise

    @contextmanager
    def read(self) -> Iterator[sqlite3.Connection]:
        """Read a consistent snapshot using an OS-level read-only connection."""
        with self._connection("ro") as connection:
            try:
                connection.execute("BEGIN")
                _check_schema_version(connection)
                yield connection
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
