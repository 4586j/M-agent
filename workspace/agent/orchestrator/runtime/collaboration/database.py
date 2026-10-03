"""Explicit, file-backed SQLite lifecycle for the collaboration package."""

from contextlib import contextmanager
from pathlib import Path
import sqlite3
from typing import Iterator

from .models import SchemaVersionError


SCHEMA_VERSION = 9

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

_AUTH_SCHEMA = (
    """
    CREATE TABLE auth_accounts (
        user_id TEXT NOT NULL PRIMARY KEY REFERENCES users(user_id),
        username TEXT NOT NULL UNIQUE,
        password_hash TEXT NOT NULL,
        is_admin INTEGER NOT NULL DEFAULT 0 CHECK(is_admin IN (0,1))
    )
    """,
    """
    CREATE TABLE auth_sessions (
        token_hash TEXT NOT NULL PRIMARY KEY CHECK(length(token_hash)=64),
        user_id TEXT NOT NULL REFERENCES users(user_id),
        user_version INTEGER NOT NULL CHECK(user_version>0),
        csrf_token TEXT NOT NULL,
        created_at REAL NOT NULL,
        expires_at REAL NOT NULL CHECK(expires_at>created_at),
        revoked INTEGER NOT NULL DEFAULT 0 CHECK(revoked IN (0,1)),
        version INTEGER NOT NULL DEFAULT 1 CHECK(version>0)
    )
    """,
    """
    CREATE TABLE auth_invitations (
        invitation_id TEXT NOT NULL PRIMARY KEY,
        token_hash TEXT NOT NULL UNIQUE CHECK(length(token_hash)=64),
        username TEXT NOT NULL,
        display_name TEXT NOT NULL,
        created_by TEXT NOT NULL REFERENCES users(user_id),
        created_by_version INTEGER NOT NULL CHECK(created_by_version>0),
        created_at REAL NOT NULL,
        expires_at REAL NOT NULL CHECK(expires_at>created_at),
        status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','accepted','revoked')),
        accepted_user_id TEXT REFERENCES users(user_id),
        version INTEGER NOT NULL DEFAULT 1 CHECK(version>0)
    )
    """,
    """
    CREATE TABLE auth_login_limits (
        peer_hash TEXT NOT NULL PRIMARY KEY,
        window_start REAL NOT NULL,
        attempts INTEGER NOT NULL CHECK(attempts>0)
    )
    """,
    """
    CREATE TABLE collaboration_audit (
        event_id TEXT NOT NULL PRIMARY KEY,
        project_id TEXT REFERENCES projects(project_id),
        actor_id TEXT REFERENCES users(user_id),
        action TEXT NOT NULL,
        target_id TEXT,
        target_version INTEGER,
        created_at REAL NOT NULL
    )
    """,
    "CREATE INDEX collaboration_audit_project_time ON collaboration_audit(project_id,created_at,event_id)",
    """
    CREATE TRIGGER collaboration_audit_no_replace
    BEFORE INSERT ON collaboration_audit
    WHEN EXISTS (SELECT 1 FROM collaboration_audit WHERE event_id=NEW.event_id)
    BEGIN
        SELECT RAISE(ABORT, 'audit events are immutable');
    END
    """,
    """
    CREATE TRIGGER collaboration_audit_no_update
    BEFORE UPDATE ON collaboration_audit
    BEGIN
        SELECT RAISE(ABORT, 'audit events are immutable');
    END
    """,
    """
    CREATE TRIGGER collaboration_audit_no_delete
    BEFORE DELETE ON collaboration_audit
    BEGIN
        SELECT RAISE(ABORT, 'audit events are immutable');
    END
    """,
)

_INSTANCE_SCHEMA = (
    """
    CREATE TABLE nodes (
        node_id TEXT NOT NULL PRIMARY KEY,
        name TEXT NOT NULL CHECK(length(trim(name))>0),
        credential_hash TEXT NOT NULL UNIQUE CHECK(length(credential_hash)=64),
        status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','disabled')),
        version INTEGER NOT NULL DEFAULT 1 CHECK(version>0)
    )
    """,
    """
    CREATE TABLE execution_instances (
        instance_id TEXT NOT NULL PRIMARY KEY,
        binding_id TEXT NOT NULL REFERENCES agent_bindings(binding_id),
        node_id TEXT NOT NULL REFERENCES nodes(node_id),
        status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','active','disabled')),
        version INTEGER NOT NULL DEFAULT 1 CHECK(version>0),
        claim_token_hash TEXT NOT NULL UNIQUE CHECK(length(claim_token_hash)=64),
        claim_expires_at REAL NOT NULL,
        instance_token_hash TEXT UNIQUE CHECK(instance_token_hash IS NULL OR length(instance_token_hash)=64),
        protocol_version TEXT,
        capacity INTEGER CHECK(capacity IS NULL OR (capacity BETWEEN 1 AND 64)),
        isolation_mode TEXT CHECK(isolation_mode IS NULL OR isolation_mode IN ('none','os_account','container','vm')),
        isolation_attestation TEXT,
        isolation_verified INTEGER NOT NULL DEFAULT 0 CHECK(isolation_verified IN (0,1)),
        verified_at REAL,
        workspace_ref TEXT,
        provider_ref TEXT,
        allowed_tools TEXT,
        env_allowlist TEXT,
        created_by TEXT NOT NULL REFERENCES users(user_id),
        created_at REAL NOT NULL,
        claimed_at REAL,
        heartbeat_at REAL,
        CHECK(NOT (isolation_verified=1 AND (isolation_mode IS NULL OR isolation_mode='none')))
    )
    """,
    """
    CREATE UNIQUE INDEX execution_instances_live_slot
        ON execution_instances (binding_id, node_id) WHERE status IN ('pending','active')
    """,
    "CREATE INDEX execution_instances_node ON execution_instances(node_id)",
)

# Revision binding columns (current/review/approved) use composite foreign keys, so a
# requirement row can never point at a revision that belongs to another requirement.
_REQUIREMENT_SCHEMA = (
    """
    CREATE TABLE requirements (
        requirement_id TEXT NOT NULL PRIMARY KEY,
        project_id TEXT NOT NULL REFERENCES projects(project_id),
        title TEXT NOT NULL CHECK(length(trim(title))>0),
        requester_id TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'draft' CHECK(status IN ('draft','in_review','approved')),
        reviewer_id TEXT,
        current_revision_id TEXT,
        review_revision_id TEXT,
        review_revision_hash TEXT CHECK(review_revision_hash IS NULL OR length(review_revision_hash)=64),
        approved_revision_id TEXT,
        approved_revision_hash TEXT CHECK(approved_revision_hash IS NULL OR length(approved_revision_hash)=64),
        version INTEGER NOT NULL DEFAULT 1 CHECK(version>0),
        created_at REAL NOT NULL,
        updated_at REAL NOT NULL,
        UNIQUE (requirement_id, project_id),
        FOREIGN KEY (project_id, requester_id) REFERENCES project_members(project_id, user_id),
        FOREIGN KEY (project_id, reviewer_id) REFERENCES project_members(project_id, user_id),
        FOREIGN KEY (current_revision_id, requirement_id)
            REFERENCES requirement_revisions(revision_id, requirement_id),
        FOREIGN KEY (review_revision_id, requirement_id)
            REFERENCES requirement_revisions(revision_id, requirement_id),
        FOREIGN KEY (approved_revision_id, requirement_id)
            REFERENCES requirement_revisions(revision_id, requirement_id),
        CHECK(status='in_review' OR
              (reviewer_id IS NULL AND review_revision_id IS NULL AND review_revision_hash IS NULL)),
        CHECK(status!='in_review' OR
              (reviewer_id IS NOT NULL AND review_revision_id IS NOT NULL
               AND review_revision_hash IS NOT NULL)),
        CHECK(status!='approved' OR
              (approved_revision_id IS NOT NULL AND approved_revision_hash IS NOT NULL)),
        CHECK((approved_revision_id IS NULL)=(approved_revision_hash IS NULL))
    )
    """,
    """
    CREATE TABLE requirement_revisions (
        revision_id TEXT NOT NULL PRIMARY KEY,
        project_id TEXT NOT NULL,
        requirement_id TEXT NOT NULL,
        number INTEGER NOT NULL CHECK(number>0),
        content TEXT NOT NULL CHECK(length(trim(content))>0),
        content_hash TEXT NOT NULL CHECK(length(content_hash)=64),
        content_size INTEGER NOT NULL CHECK(content_size>0),
        author_id TEXT NOT NULL,
        created_at REAL NOT NULL,
        UNIQUE (requirement_id, number),
        UNIQUE (revision_id, requirement_id),
        FOREIGN KEY (requirement_id, project_id) REFERENCES requirements(requirement_id, project_id),
        FOREIGN KEY (project_id, author_id) REFERENCES project_members(project_id, user_id)
    )
    """,
    """
    CREATE TRIGGER requirement_revisions_no_replace
    BEFORE INSERT ON requirement_revisions
    WHEN EXISTS (
        SELECT 1 FROM requirement_revisions
        WHERE revision_id = NEW.revision_id OR (requirement_id = NEW.requirement_id AND number = NEW.number)
    )
    BEGIN
        SELECT RAISE(ABORT, 'requirement revisions are immutable');
    END
    """,
    """
    CREATE TRIGGER requirement_revisions_no_update
    BEFORE UPDATE ON requirement_revisions
    BEGIN
        SELECT RAISE(ABORT, 'requirement revisions are immutable');
    END
    """,
    """
    CREATE TRIGGER requirement_revisions_no_delete
    BEFORE DELETE ON requirement_revisions
    BEGIN
        SELECT RAISE(ABORT, 'requirement revisions are immutable');
    END
    """,
    """
    CREATE TABLE requirement_messages (
        message_id TEXT NOT NULL PRIMARY KEY,
        project_id TEXT NOT NULL,
        requirement_id TEXT NOT NULL,
        role TEXT NOT NULL CHECK(role IN ('user','assistant')),
        content TEXT NOT NULL CHECK(length(trim(content))>0),
        author_id TEXT,
        created_at REAL NOT NULL,
        FOREIGN KEY (requirement_id, project_id) REFERENCES requirements(requirement_id, project_id),
        FOREIGN KEY (project_id, author_id) REFERENCES project_members(project_id, user_id),
        CHECK(role='user' OR author_id IS NULL),
        CHECK(role='assistant' OR author_id IS NOT NULL)
    )
    """,
    """
    CREATE TABLE requirement_reviews (
        review_id TEXT NOT NULL PRIMARY KEY,
        project_id TEXT NOT NULL,
        requirement_id TEXT NOT NULL,
        revision_id TEXT NOT NULL,
        revision_hash TEXT NOT NULL CHECK(length(revision_hash)=64),
        target_version INTEGER NOT NULL CHECK(target_version>0),
        reviewer_id TEXT NOT NULL,
        decision TEXT NOT NULL CHECK(decision IN ('requested','approved','rejected')),
        reason TEXT NOT NULL DEFAULT '',
        created_at REAL NOT NULL,
        FOREIGN KEY (requirement_id, project_id) REFERENCES requirements(requirement_id, project_id),
        FOREIGN KEY (revision_id, requirement_id) REFERENCES requirement_revisions(revision_id, requirement_id),
        FOREIGN KEY (project_id, reviewer_id) REFERENCES project_members(project_id, user_id)
    )
    """,
    "CREATE INDEX requirement_reviews_chain ON requirement_reviews(requirement_id,created_at,review_id)",
    "CREATE INDEX requirement_messages_stream ON requirement_messages(requirement_id,created_at,message_id)",
    """
    CREATE TRIGGER requirement_reviews_no_replace
    BEFORE INSERT ON requirement_reviews
    WHEN EXISTS (SELECT 1 FROM requirement_reviews WHERE review_id = NEW.review_id)
    BEGIN
        SELECT RAISE(ABORT, 'requirement reviews are immutable');
    END
    """,
    """
    CREATE TRIGGER requirement_reviews_no_update
    BEFORE UPDATE ON requirement_reviews
    BEGIN
        SELECT RAISE(ABORT, 'requirement reviews are immutable');
    END
    """,
    """
    CREATE TRIGGER requirement_reviews_no_delete
    BEFORE DELETE ON requirement_reviews
    BEGIN
        SELECT RAISE(ABORT, 'requirement reviews are immutable');
    END
    """,
)

_PLANNING_SCHEMA = (
    # Rebuilds `requirements` while child tables keep foreign keys to it; initialize()
    # disables FK enforcement for this migration transaction and uses foreign_key_check.
    """
    CREATE TABLE requirements_v5 (
        requirement_id TEXT NOT NULL PRIMARY KEY,
        project_id TEXT NOT NULL REFERENCES projects(project_id),
        title TEXT NOT NULL CHECK(length(trim(title))>0),
        requester_id TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'draft'
            CHECK(status IN ('draft','in_review','approved','planning','planning_blocked',
                             'executing','integrating','awaiting_acceptance','rework','paused',
                             'cancelling','cancelled','completed')),
        reviewer_id TEXT,
        current_revision_id TEXT,
        review_revision_id TEXT,
        review_revision_hash TEXT CHECK(review_revision_hash IS NULL OR length(review_revision_hash)=64),
        approved_revision_id TEXT,
        approved_revision_hash TEXT CHECK(approved_revision_hash IS NULL OR length(approved_revision_hash)=64),
        version INTEGER NOT NULL DEFAULT 1 CHECK(version>0),
        created_at REAL NOT NULL,
        updated_at REAL NOT NULL,
        UNIQUE (requirement_id, project_id),
        FOREIGN KEY (project_id, requester_id) REFERENCES project_members(project_id, user_id),
        FOREIGN KEY (project_id, reviewer_id) REFERENCES project_members(project_id, user_id),
        FOREIGN KEY (current_revision_id, requirement_id)
            REFERENCES requirement_revisions(revision_id, requirement_id),
        FOREIGN KEY (review_revision_id, requirement_id)
            REFERENCES requirement_revisions(revision_id, requirement_id),
        FOREIGN KEY (approved_revision_id, requirement_id)
            REFERENCES requirement_revisions(revision_id, requirement_id),
        CHECK(status='in_review' OR
              (reviewer_id IS NULL AND review_revision_id IS NULL AND review_revision_hash IS NULL)),
        CHECK(status!='in_review' OR
              (reviewer_id IS NOT NULL AND review_revision_id IS NOT NULL
               AND review_revision_hash IS NOT NULL)),
        CHECK(status!='approved' OR
              (approved_revision_id IS NOT NULL AND approved_revision_hash IS NOT NULL)),
        CHECK((approved_revision_id IS NULL)=(approved_revision_hash IS NULL))
    )
    """,
    """
    INSERT INTO requirements_v5
        (requirement_id,project_id,title,requester_id,status,reviewer_id,current_revision_id,
         review_revision_id,review_revision_hash,approved_revision_id,approved_revision_hash,
         version,created_at,updated_at)
    SELECT requirement_id,project_id,title,requester_id,status,reviewer_id,current_revision_id,
           review_revision_id,review_revision_hash,approved_revision_id,approved_revision_hash,
           version,created_at,updated_at
    FROM requirements
    """,
    "DROP TABLE requirements",
    "ALTER TABLE requirements_v5 RENAME TO requirements",
    "CREATE UNIQUE INDEX agent_bindings_scope ON agent_bindings (binding_id, project_id)",
    """
    CREATE TABLE plans (
        plan_id TEXT NOT NULL PRIMARY KEY,
        project_id TEXT NOT NULL,
        requirement_id TEXT NOT NULL,
        revision_id TEXT NOT NULL,
        revision_hash TEXT NOT NULL CHECK(length(revision_hash)=64),
        status TEXT NOT NULL CHECK(status IN ('blocked','published')),
        report TEXT NOT NULL CHECK(length(trim(report))>0),
        total_units INTEGER NOT NULL DEFAULT 0 CHECK(total_units>=0),
        created_by TEXT,
        created_at REAL NOT NULL,
        published_at REAL,
        version INTEGER NOT NULL DEFAULT 1 CHECK(version>0),
        UNIQUE (plan_id, project_id),
        FOREIGN KEY (requirement_id, project_id)
            REFERENCES requirements(requirement_id, project_id),
        FOREIGN KEY (revision_id, requirement_id)
            REFERENCES requirement_revisions(revision_id, requirement_id),
        CHECK((status='published')=(published_at IS NOT NULL))
    )
    """,
    "CREATE UNIQUE INDEX plans_one_published ON plans(requirement_id) WHERE status='published'",
    """
    CREATE TABLE work_items (
        work_item_id TEXT NOT NULL PRIMARY KEY,
        plan_id TEXT NOT NULL,
        project_id TEXT NOT NULL,
        key TEXT NOT NULL CHECK(length(trim(key))>0),
        title TEXT NOT NULL CHECK(length(trim(title))>0),
        objective TEXT NOT NULL CHECK(length(trim(objective))>0),
        scope TEXT NOT NULL CHECK(length(trim(scope))>0),
        non_goals TEXT NOT NULL CHECK(length(trim(non_goals))>0),
        duty TEXT NOT NULL CHECK(length(trim(duty))>0),
        assignee_user_id TEXT NOT NULL,
        binding_id TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'pending_dependency'
            CHECK(status IN ('pending_dependency','ready','waiting_agent','running',
                             'awaiting_owner_review','submitted','accepted','changes_requested',
                             'blocked','failed','cancelled')),
        estimated_units INTEGER NOT NULL DEFAULT 0 CHECK(estimated_units>=0),
        version INTEGER NOT NULL DEFAULT 1 CHECK(version>0),
        created_at REAL NOT NULL,
        updated_at REAL NOT NULL,
        UNIQUE (plan_id, key),
        UNIQUE (work_item_id, plan_id, project_id),
        FOREIGN KEY (plan_id, project_id) REFERENCES plans(plan_id, project_id),
        FOREIGN KEY (project_id, assignee_user_id) REFERENCES project_members(project_id, user_id),
        FOREIGN KEY (binding_id, project_id) REFERENCES agent_bindings(binding_id, project_id)
    )
    """,
    "CREATE INDEX work_items_status ON work_items(status)",
    """
    CREATE TABLE work_item_dependencies (
        work_item_id TEXT NOT NULL,
        depends_on_id TEXT NOT NULL,
        plan_id TEXT NOT NULL,
        project_id TEXT NOT NULL,
        PRIMARY KEY (work_item_id, depends_on_id),
        CHECK (work_item_id <> depends_on_id),
        FOREIGN KEY (work_item_id, plan_id, project_id)
            REFERENCES work_items(work_item_id, plan_id, project_id),
        FOREIGN KEY (depends_on_id, plan_id, project_id)
            REFERENCES work_items(work_item_id, plan_id, project_id)
    )
    """,
    "CREATE INDEX work_item_dependencies_reverse ON work_item_dependencies(depends_on_id)",
    """
    CREATE TABLE revision_acceptance_items (
        revision_id TEXT NOT NULL,
        requirement_id TEXT NOT NULL,
        item_id TEXT NOT NULL CHECK(length(trim(item_id))>0),
        text TEXT NOT NULL CHECK(length(trim(text))>0),
        position INTEGER NOT NULL CHECK(position>=0),
        PRIMARY KEY (revision_id, item_id),
        FOREIGN KEY (revision_id, requirement_id)
            REFERENCES requirement_revisions(revision_id, requirement_id)
    )
    """,
    """
    CREATE TRIGGER revision_acceptance_items_no_update
    BEFORE UPDATE ON revision_acceptance_items
    BEGIN
        SELECT RAISE(ABORT, 'acceptance items are immutable');
    END
    """,
    """
    CREATE TRIGGER revision_acceptance_items_no_delete
    BEFORE DELETE ON revision_acceptance_items
    BEGIN
        SELECT RAISE(ABORT, 'acceptance items are immutable');
    END
    """,
    """
    CREATE TABLE outbox_events (
        event_id TEXT NOT NULL PRIMARY KEY,
        project_id TEXT NOT NULL REFERENCES projects(project_id),
        topic TEXT NOT NULL CHECK(length(trim(topic))>0),
        payload TEXT NOT NULL,
        created_at REAL NOT NULL,
        next_attempt_at REAL NOT NULL,
        attempts INTEGER NOT NULL DEFAULT 0 CHECK(attempts>=0),
        claim_expires_at REAL,
        delivered_at REAL,
        last_error TEXT
    )
    """,
    "CREATE INDEX outbox_due ON outbox_events(delivered_at,next_attempt_at)",
    """
    CREATE TRIGGER outbox_events_identity_immutable
    BEFORE UPDATE ON outbox_events
    WHEN NEW.event_id<>OLD.event_id OR NEW.project_id<>OLD.project_id
         OR NEW.topic<>OLD.topic OR NEW.payload<>OLD.payload OR NEW.created_at<>OLD.created_at
    BEGIN
        SELECT RAISE(ABORT, 'outbox event identity is immutable');
    END
    """,
    """
    CREATE TABLE budget_accounts (
        scope TEXT NOT NULL CHECK(scope IN ('global','project','requirement')),
        ref_id TEXT NOT NULL,
        limit_units INTEGER NOT NULL CHECK(limit_units>=0),
        version INTEGER NOT NULL DEFAULT 1 CHECK(version>0),
        updated_at REAL NOT NULL,
        PRIMARY KEY (scope, ref_id)
    )
    """,
    """
    CREATE TABLE budget_entries (
        entry_id TEXT NOT NULL PRIMARY KEY,
        scope TEXT NOT NULL CHECK(scope IN ('global','project','requirement')),
        ref_id TEXT NOT NULL,
        ref_type TEXT NOT NULL CHECK(ref_type IN ('plan','work_item','attempt','adjustment')),
        ref_key TEXT NOT NULL CHECK(length(trim(ref_key))>0),
        kind TEXT NOT NULL CHECK(kind IN ('reserve','settle','release','adjustment')),
        delta_units INTEGER NOT NULL,
        created_at REAL NOT NULL,
        UNIQUE (scope, ref_id, ref_type, ref_key, kind)
    )
    """,
)

_EXECUTION_SCHEMA = (
    """
    ALTER TABLE execution_instances ADD COLUMN worker_url TEXT
        CHECK(worker_url IS NULL OR (length(worker_url)<=512
              AND (worker_url LIKE 'http://%' OR worker_url LIKE 'https://%')))
    """,
    """
    ALTER TABLE execution_instances ADD COLUMN control_epoch INTEGER NOT NULL DEFAULT 1
        CHECK(control_epoch>=1)
    """,
    """
    CREATE TABLE attempts (
        attempt_id TEXT NOT NULL PRIMARY KEY,
        project_id TEXT NOT NULL,
        work_item_id TEXT NOT NULL,
        plan_id TEXT NOT NULL,
        requirement_id TEXT NOT NULL,
        revision_id TEXT NOT NULL,
        revision_hash TEXT NOT NULL CHECK(length(revision_hash)=64),
        instance_id TEXT NOT NULL,
        binding_id TEXT NOT NULL,
        binding_version INTEGER NOT NULL CHECK(binding_version>0),
        assignee_user_id TEXT NOT NULL,
        number INTEGER NOT NULL CHECK(number>=1),
        schema_version TEXT NOT NULL CHECK(length(trim(schema_version))>0),
        input_hash TEXT NOT NULL CHECK(length(input_hash)=64),
        package_json TEXT NOT NULL CHECK(length(trim(package_json))>0),
        status TEXT NOT NULL DEFAULT 'assigned'
            CHECK(status IN ('assigned','acked','running','submitted','accepted',
                             'changes_requested','failed','cancelled',
                             'reconciliation_required')),
        lease_epoch INTEGER NOT NULL DEFAULT 1 CHECK(lease_epoch>=1),
        control_epoch INTEGER NOT NULL DEFAULT 1 CHECK(control_epoch>=1),
        lease_expires_at REAL NOT NULL,
        usage_units INTEGER NOT NULL DEFAULT 0 CHECK(usage_units>=0),
        result_json TEXT,
        result_hash TEXT CHECK(result_hash IS NULL OR length(result_hash)=64),
        trace_id TEXT,
        version INTEGER NOT NULL DEFAULT 1 CHECK(version>0),
        created_at REAL NOT NULL,
        updated_at REAL NOT NULL,
        UNIQUE (attempt_id, project_id),
        UNIQUE (work_item_id, number),
        FOREIGN KEY (work_item_id, plan_id, project_id)
            REFERENCES work_items(work_item_id, plan_id, project_id),
        FOREIGN KEY (requirement_id, project_id)
            REFERENCES requirements(requirement_id, project_id),
        FOREIGN KEY (revision_id, requirement_id)
            REFERENCES requirement_revisions(revision_id, requirement_id),
        FOREIGN KEY (project_id, assignee_user_id) REFERENCES project_members(project_id, user_id),
        FOREIGN KEY (binding_id, project_id) REFERENCES agent_bindings(binding_id, project_id),
        FOREIGN KEY (instance_id) REFERENCES execution_instances(instance_id)
    )
    """,
    """
    CREATE UNIQUE INDEX attempts_open_per_work_item ON attempts(work_item_id)
        WHERE status IN ('assigned','acked','running','reconciliation_required')
    """,
    "CREATE INDEX attempts_work_item ON attempts(work_item_id)",
    "CREATE INDEX attempts_instance ON attempts(instance_id,status)",
    """
    CREATE TRIGGER attempts_identity_immutable
    BEFORE UPDATE ON attempts
    WHEN NEW.attempt_id<>OLD.attempt_id OR NEW.project_id<>OLD.project_id
         OR NEW.work_item_id<>OLD.work_item_id OR NEW.plan_id<>OLD.plan_id
         OR NEW.requirement_id<>OLD.requirement_id OR NEW.revision_id<>OLD.revision_id
         OR NEW.revision_hash<>OLD.revision_hash OR NEW.instance_id<>OLD.instance_id
         OR NEW.binding_id<>OLD.binding_id OR NEW.binding_version<>OLD.binding_version
         OR NEW.assignee_user_id<>OLD.assignee_user_id OR NEW.number<>OLD.number
         OR NEW.schema_version<>OLD.schema_version OR NEW.input_hash<>OLD.input_hash
         OR NEW.package_json<>OLD.package_json OR NEW.created_at<>OLD.created_at
    BEGIN
        SELECT RAISE(ABORT, 'attempt identity is immutable');
    END
    """,
    """
    CREATE TABLE attempt_reports (
        report_id TEXT NOT NULL PRIMARY KEY,
        attempt_id TEXT NOT NULL,
        project_id TEXT NOT NULL,
        stage TEXT NOT NULL CHECK(stage IN ('ack','start','lease','artifact','result','cancel')),
        lease_epoch INTEGER NOT NULL DEFAULT 0 CHECK(lease_epoch>=0),
        control_epoch INTEGER NOT NULL DEFAULT 0 CHECK(control_epoch>=0),
        input_hash TEXT NOT NULL CHECK(length(input_hash)=64),
        payload TEXT NOT NULL,
        accepted INTEGER NOT NULL CHECK(accepted IN (0,1)),
        reason TEXT,
        created_at REAL NOT NULL,
        FOREIGN KEY (attempt_id, project_id) REFERENCES attempts(attempt_id, project_id)
    )
    """,
    "CREATE INDEX attempt_reports_attempt ON attempt_reports(attempt_id)",
    """
    CREATE TRIGGER attempt_reports_immutable
    BEFORE UPDATE ON attempt_reports
    BEGIN
        SELECT RAISE(ABORT, 'attempt reports are immutable');
    END
    """,
    """
    CREATE TRIGGER attempt_reports_no_delete
    BEFORE DELETE ON attempt_reports
    BEGIN
        SELECT RAISE(ABORT, 'attempt reports are immutable');
    END
    """,
    """
    CREATE TABLE attempt_artifacts (
        artifact_id TEXT NOT NULL PRIMARY KEY,
        attempt_id TEXT NOT NULL,
        project_id TEXT NOT NULL,
        work_item_id TEXT NOT NULL,
        name TEXT NOT NULL CHECK(length(trim(name))>0 AND length(name)<=512),
        media_type TEXT NOT NULL CHECK(length(trim(media_type))>0 AND length(media_type)<=128),
        size_bytes INTEGER NOT NULL CHECK(size_bytes>=0),
        sha256 TEXT NOT NULL CHECK(length(sha256)=64),
        content BLOB NOT NULL,
        created_at REAL NOT NULL,
        UNIQUE (attempt_id, name),
        CHECK (length(content)=size_bytes),
        FOREIGN KEY (attempt_id, project_id) REFERENCES attempts(attempt_id, project_id),
        FOREIGN KEY (work_item_id) REFERENCES work_items(work_item_id)
    )
    """,
    "CREATE INDEX attempt_artifacts_work_item ON attempt_artifacts(work_item_id)",
    """
    CREATE TRIGGER attempt_artifacts_immutable
    BEFORE UPDATE ON attempt_artifacts
    BEGIN
        SELECT RAISE(ABORT, 'attempt artifacts are immutable');
    END
    """,
    """
    CREATE TRIGGER attempt_artifacts_no_delete
    BEFORE DELETE ON attempt_artifacts
    BEGIN
        SELECT RAISE(ABORT, 'attempt artifacts are immutable');
    END
    """,
    """
    CREATE TABLE work_item_acceptance_items (
        work_item_id TEXT NOT NULL,
        item_id TEXT NOT NULL CHECK(length(trim(item_id))>0),
        position INTEGER NOT NULL CHECK(position>=0),
        PRIMARY KEY (work_item_id, item_id),
        FOREIGN KEY (work_item_id) REFERENCES work_items(work_item_id)
    )
    """,
    """
    CREATE TABLE owner_reviews (
        review_id TEXT NOT NULL PRIMARY KEY,
        attempt_id TEXT NOT NULL,
        project_id TEXT NOT NULL,
        work_item_id TEXT NOT NULL,
        reviewer_user_id TEXT NOT NULL,
        decision TEXT NOT NULL CHECK(decision IN ('confirmed','changes_requested')),
        result_hash TEXT NOT NULL CHECK(length(result_hash)=64),
        reason TEXT NOT NULL DEFAULT '',
        created_at REAL NOT NULL,
        UNIQUE (attempt_id),
        FOREIGN KEY (attempt_id, project_id) REFERENCES attempts(attempt_id, project_id),
        FOREIGN KEY (work_item_id) REFERENCES work_items(work_item_id),
        FOREIGN KEY (project_id, reviewer_user_id) REFERENCES project_members(project_id, user_id)
    )
    """,
    """
    CREATE TRIGGER owner_reviews_immutable
    BEFORE UPDATE ON owner_reviews
    BEGIN
        SELECT RAISE(ABORT, 'owner reviews are immutable');
    END
    """,
    """
    CREATE TRIGGER owner_reviews_no_delete
    BEFORE DELETE ON owner_reviews
    BEGIN
        SELECT RAISE(ABORT, 'owner reviews are immutable');
    END
    """,
    """
    CREATE TABLE security_reviews (
        security_review_id TEXT NOT NULL PRIMARY KEY,
        attempt_id TEXT NOT NULL,
        project_id TEXT NOT NULL,
        rule_key TEXT NOT NULL CHECK(length(trim(rule_key))>0 AND length(rule_key)<=128),
        input_hash TEXT NOT NULL CHECK(length(input_hash)=64),
        requirement_id TEXT NOT NULL,
        revision_id TEXT NOT NULL,
        binding_version INTEGER NOT NULL CHECK(binding_version>0),
        tool_policy_version TEXT NOT NULL CHECK(length(tool_policy_version)=64),
        decision TEXT NOT NULL DEFAULT 'pending'
            CHECK(decision IN ('pending','approved','rejected')),
        reviewer_user_id TEXT,
        reason TEXT NOT NULL DEFAULT '',
        expires_at REAL NOT NULL CHECK(expires_at>0),
        version INTEGER NOT NULL DEFAULT 1 CHECK(version>0),
        created_at REAL NOT NULL,
        updated_at REAL NOT NULL,
        UNIQUE (attempt_id, rule_key),
        CHECK (decision='pending' OR reviewer_user_id IS NOT NULL),
        FOREIGN KEY (attempt_id, project_id) REFERENCES attempts(attempt_id, project_id),
        FOREIGN KEY (revision_id, requirement_id)
            REFERENCES requirement_revisions(revision_id, requirement_id)
    )
    """,
    "CREATE INDEX security_reviews_attempt ON security_reviews(attempt_id)",
    """
    CREATE TRIGGER security_reviews_identity_immutable
    BEFORE UPDATE ON security_reviews
    WHEN NEW.security_review_id<>OLD.security_review_id OR NEW.attempt_id<>OLD.attempt_id
         OR NEW.project_id<>OLD.project_id OR NEW.rule_key<>OLD.rule_key
         OR NEW.input_hash<>OLD.input_hash OR NEW.revision_id<>OLD.revision_id
         OR NEW.binding_version<>OLD.binding_version
         OR NEW.tool_policy_version<>OLD.tool_policy_version OR NEW.created_at<>OLD.created_at
    BEGIN
        SELECT RAISE(ABORT, 'security review binding is immutable');
    END
    """,
    """
    CREATE TRIGGER security_reviews_decided_once
    BEFORE UPDATE OF decision ON security_reviews
    WHEN OLD.decision<>'pending' AND NEW.decision<>OLD.decision
    BEGIN
        SELECT RAISE(ABORT, 'security review decision is final');
    END
    """,
    """
    CREATE TRIGGER security_reviews_no_delete
    BEFORE DELETE ON security_reviews
    BEGIN
        SELECT RAISE(ABORT, 'security reviews are immutable');
    END
    """,
    """
    CREATE TABLE security_grants (
        project_id TEXT NOT NULL,
        user_id TEXT NOT NULL,
        capability TEXT NOT NULL CHECK(capability='security:approve'),
        status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','revoked')),
        granted_by TEXT NOT NULL,
        version INTEGER NOT NULL DEFAULT 1 CHECK(version>0),
        created_at REAL NOT NULL,
        updated_at REAL NOT NULL,
        PRIMARY KEY (project_id, user_id, capability),
        FOREIGN KEY (project_id, user_id) REFERENCES project_members(project_id, user_id)
    )
    """,
)

# The owner's confirmation ("this delivery is worth reading") and the reviewer's business
# acceptance ("this delivery meets the acceptance items") are two judgements on one result.
# Acceptance used to survive only as an audit action, so its reason could not be handed to the
# next attempt; it now gets its own immutable row beside the confirmation.
_DELIVERY_SCHEMA = (
    """
    CREATE TABLE acceptance_reviews (
        review_id TEXT NOT NULL PRIMARY KEY,
        attempt_id TEXT NOT NULL,
        project_id TEXT NOT NULL,
        work_item_id TEXT NOT NULL,
        reviewer_user_id TEXT NOT NULL,
        decision TEXT NOT NULL CHECK(decision IN ('accepted','changes_requested')),
        result_hash TEXT NOT NULL CHECK(length(result_hash)=64),
        reason TEXT NOT NULL DEFAULT '',
        created_at REAL NOT NULL,
        UNIQUE (attempt_id),
        FOREIGN KEY (attempt_id, project_id) REFERENCES attempts(attempt_id, project_id),
        FOREIGN KEY (work_item_id) REFERENCES work_items(work_item_id),
        FOREIGN KEY (project_id, reviewer_user_id) REFERENCES project_members(project_id, user_id)
    )
    """,
    "CREATE INDEX acceptance_reviews_work_item ON acceptance_reviews(work_item_id)",
    """
    CREATE TRIGGER acceptance_reviews_immutable
    BEFORE UPDATE ON acceptance_reviews
    BEGIN
        SELECT RAISE(ABORT, 'acceptance reviews are immutable');
    END
    """,
    """
    CREATE TRIGGER acceptance_reviews_no_delete
    BEFORE DELETE ON acceptance_reviews
    BEGIN
        SELECT RAISE(ABORT, 'acceptance reviews are immutable');
    END
    """,
)

# The requester's own verdict on the whole requirement is a fourth decision, distinct from the
# drafting review, the owner's confirmation and the business acceptance of one item. It binds to
# a digest over the accepted attempts, so "accepted" can never mean "the workers said done".
_FINAL_ACCEPTANCE_SCHEMA = (
    """
    CREATE TABLE requirement_acceptance (
        acceptance_id TEXT NOT NULL PRIMARY KEY,
        project_id TEXT NOT NULL,
        requirement_id TEXT NOT NULL,
        reviewer_user_id TEXT NOT NULL,
        decision TEXT NOT NULL CHECK(decision IN ('accepted','changes_requested')),
        evidence_hash TEXT NOT NULL CHECK(length(evidence_hash)=64),
        reason TEXT NOT NULL DEFAULT '',
        created_at REAL NOT NULL,
        FOREIGN KEY (requirement_id, project_id) REFERENCES requirements(requirement_id, project_id),
        FOREIGN KEY (project_id, reviewer_user_id) REFERENCES project_members(project_id, user_id)
    )
    """,
    "CREATE INDEX requirement_acceptance_requirement ON requirement_acceptance(requirement_id,"
    "created_at)",
    """
    CREATE TRIGGER requirement_acceptance_immutable
    BEFORE UPDATE ON requirement_acceptance
    BEGIN
        SELECT RAISE(ABORT, 'requirement acceptance is immutable');
    END
    """,
    """
    CREATE TRIGGER requirement_acceptance_no_delete
    BEFORE DELETE ON requirement_acceptance
    BEGIN
        SELECT RAISE(ABORT, 'requirement acceptance is immutable');
    END
    """,
)

# A to-do is derived from live state, so "unread" cannot be a column on it. It is one small
# acknowledgement row per person per decision owed: read state, not evidence, so it is mutable
# and deliberately has no immutability trigger.
_INBOX_ACK_SCHEMA = (
    """
    CREATE TABLE inbox_acks (
        user_id TEXT NOT NULL REFERENCES users(user_id),
        item_key TEXT NOT NULL CHECK (length(item_key) BETWEEN 1 AND 200),
        acked_at REAL NOT NULL,
        PRIMARY KEY (user_id, item_key)
    )
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
                pending = connection.execute("PRAGMA user_version").fetchone()[0]
                if pending == 4:
                    # v4 -> v5 rebuilds `requirements`, which several tables reference.
                    # DROP TABLE would trip those foreign keys, so enforcement is off for
                    # this transaction and `PRAGMA foreign_key_check` verifies the result
                    # before the commit instead. The pragma only applies before BEGIN.
                    connection.execute("PRAGMA foreign_keys = OFF")
                connection.execute("BEGIN IMMEDIATE")
                version = connection.execute("PRAGMA user_version").fetchone()[0]
                migrated = False
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
                    version = 1
                    migrated = True
                if version == 1:
                    for statement in _AUTH_SCHEMA:
                        connection.execute(statement)
                    version = 2
                    migrated = True
                if version == 2:
                    for statement in _INSTANCE_SCHEMA:
                        connection.execute(statement)
                    version = 3
                    migrated = True
                if version == 3:
                    for statement in _REQUIREMENT_SCHEMA:
                        connection.execute(statement)
                    version = 4
                    migrated = True
                if version == 4:
                    for statement in _PLANNING_SCHEMA:
                        connection.execute(statement)
                    version = 5
                    migrated = True
                if version == 5:
                    for statement in _EXECUTION_SCHEMA:
                        connection.execute(statement)
                    version = 6
                    migrated = True
                if version == 6:
                    for statement in _DELIVERY_SCHEMA:
                        connection.execute(statement)
                    version = 7
                    migrated = True
                if version == 7:
                    for statement in _FINAL_ACCEPTANCE_SCHEMA:
                        connection.execute(statement)
                    version = 8
                    migrated = True
                if version == 8:
                    for statement in _INBOX_ACK_SCHEMA:
                        connection.execute(statement)
                    version = 9
                    migrated = True
                if migrated:
                    connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
                _check_schema_version(connection)
                if pending == 4:
                    violations = connection.execute("PRAGMA foreign_key_check").fetchall()
                    if violations:
                        raise SchemaVersionError(
                            "Migration left foreign key violations in: "
                            + ", ".join(sorted({row[0] for row in violations})))
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
