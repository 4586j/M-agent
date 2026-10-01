from __future__ import annotations

import hashlib
import hmac
import re
import secrets
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass
from uuid import uuid4

from .database import Database
from .models import CollaborationError, Conflict, NotFound, User, ValidationError
from .store import CollaborationStore, _text, _version

PASSWORD_ITERATIONS = 600_000
LOGIN_WINDOW = 300
LOGIN_LIMIT = 10
_DUMMY_HASH = f"pbkdf2_sha256${PASSWORD_ITERATIONS}${'00' * 16}${'00' * 32}"


class Unauthorized(CollaborationError):
    pass


class Forbidden(CollaborationError):
    pass


class RateLimited(CollaborationError):
    pass


def normalize_username(username: str) -> str:
    if not isinstance(username, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{2,63}", username):
        raise ValidationError("Invalid username")
    return username.lower()


def hash_password(password: str) -> str:
    if not isinstance(password, str) or not 12 <= len(password) <= 1024:
        raise ValidationError("Password must contain 12 to 1024 characters")
    try:
        password_bytes = password.encode("utf-8")
    except UnicodeEncodeError:
        raise ValidationError("Password must be valid Unicode") from None
    salt = secrets.token_bytes(16)
    value = hashlib.pbkdf2_hmac("sha256", password_bytes, salt, PASSWORD_ITERATIONS)
    return f"pbkdf2_sha256${PASSWORD_ITERATIONS}${salt.hex()}${value.hex()}"


def verify_password(password: str, encoded: str) -> bool:
    if not isinstance(password, str) or not 12 <= len(password) <= 1024:
        return False
    try:
        algorithm, iterations, salt, expected = encoded.split("$")
        if algorithm != "pbkdf2_sha256" or int(iterations) != PASSWORD_ITERATIONS:
            return False
        salt_bytes, expected_bytes = bytes.fromhex(salt), bytes.fromhex(expected)
        if len(salt_bytes) != 16 or len(expected_bytes) != 32:
            return False
        password_bytes = password.encode("utf-8")
    except (ValueError, AttributeError):
        return False
    actual = hashlib.pbkdf2_hmac("sha256", password_bytes, salt_bytes, PASSWORD_ITERATIONS)
    return hmac.compare_digest(actual, expected_bytes)


def token_digest(token: str | None) -> str:
    if not isinstance(token, str) or not re.fullmatch(r"[A-Za-z0-9_-]{43}", token):
        raise Unauthorized("Authentication required")
    return hashlib.sha256(token.encode("ascii")).hexdigest()


def record_audit(conn, created_at: float, actor_id: str | None, action: str,
                 target_id: str | None, *, project_id: str | None = None,
                 target_version: int | None = None) -> None:
    conn.execute(
        "INSERT INTO collaboration_audit(event_id,project_id,actor_id,action,target_id,target_version,created_at) "
        "VALUES(?,?,?,?,?,?,?)",
        (uuid4().hex, project_id, actor_id, action, target_id, target_version, created_at),
    )


@dataclass(frozen=True)
class SessionResult:
    user: User
    is_admin: bool
    session_token: str
    csrf_token: str
    expires_at: float


@dataclass(frozen=True)
class InvitationResult:
    invitation_id: str
    token: str
    expires_at: float
    version: int


@dataclass(frozen=True)
class Access:
    user: User
    is_admin: bool
    csrf_token: str
    expires_at: float
    store: CollaborationStore
    conn: sqlite3.Connection


class AuthService:
    def __init__(self, database: Database, *, session_ttl: int = 28800,
                 invitation_ttl: int = 86400, clock=time.time):
        if not 60 <= session_ttl <= 86400 or not 60 <= invitation_ttl <= 604800:
            raise ValidationError("Invalid authentication lifetime")
        self.database = database
        self.session_ttl = session_ttl
        self.invitation_ttl = invitation_ttl
        self.clock = clock

    def audit(self, conn, actor_id: str | None, action: str, target_id: str | None, *,
              project_id: str | None = None, target_version: int | None = None):
        record_audit(conn, self.clock(), actor_id, action, target_id,
                     project_id=project_id, target_version=target_version)

    def _authenticate(self, conn, token) -> Access:
        row = conn.execute(
            "SELECT s.*,a.is_admin FROM auth_sessions s JOIN auth_accounts a ON a.user_id=s.user_id "
            "WHERE s.token_hash=?", (token_digest(token),)).fetchone()
        if row is None or row["revoked"] or row["expires_at"] <= self.clock():
            raise Unauthorized("Authentication required")
        store = CollaborationStore(self.database, connection=conn)
        user = store.get_user(row["user_id"])
        if user.status != "active" or user.version != row["user_version"]:
            raise Unauthorized("Authentication required")
        return Access(user, bool(row["is_admin"]), row["csrf_token"], row["expires_at"], store, conn)

    @contextmanager
    def access(self, token: str | None, csrf: str | None = None, *, project_id: str | None = None,
               roles: tuple[str, ...] | None = None, admin: bool = False, write: bool = False):
        principal = None
        context = self.database.transaction() if write else self.database.read()
        try:
            with context as conn:
                principal = self._authenticate(conn, token)
                if write and (not isinstance(csrf, str) or not re.fullmatch(r"[A-Za-z0-9_-]{43}", csrf)
                              or not hmac.compare_digest(csrf, principal.csrf_token)):
                    raise Forbidden("CSRF check failed")
                if admin and not principal.is_admin:
                    raise Forbidden("Operation not permitted")
                if project_id is not None:
                    member = principal.store.get_member(project_id, principal.user.user_id)
                    if member.status != "active":
                        raise NotFound("Resource not found")
                    if roles is not None and member.access_role not in roles:
                        raise Forbidden("Operation not permitted")
                yield principal
        except sqlite3.IntegrityError as exc:
            raise Conflict("Operation conflicts with existing data") from exc
        except (Forbidden, NotFound):
            if principal is not None:
                with self.database.transaction() as conn:
                    self.audit(conn, principal.user.user_id, "authorization.denied", None)
            raise

    def bootstrap(self, username: str, display_name: str, password: str) -> User:
        username = normalize_username(username)
        _text(display_name, "display_name")
        password_hash = hash_password(password)
        with self.database.transaction() as conn:
            if conn.execute("SELECT 1 FROM auth_accounts LIMIT 1").fetchone():
                raise Conflict("Administrator has already been initialized")
            store = CollaborationStore(self.database, connection=conn)
            user = store.create_user(display_name)
            conn.execute("INSERT INTO auth_accounts(user_id,username,password_hash,is_admin) VALUES(?,?,?,1)",
                         (user.user_id, username, password_hash))
            self.audit(conn, user.user_id, "account.bootstrapped", user.user_id, target_version=user.version)
            return user

    def _login_slot(self, peer: str):
        key = hashlib.sha256(peer.encode("utf-8")).hexdigest()
        now = self.clock()
        with self.database.transaction() as conn:
            row = conn.execute("SELECT * FROM auth_login_limits WHERE peer_hash=?", (key,)).fetchone()
            blocked = row is not None and now < row["window_start"] + LOGIN_WINDOW and row["attempts"] >= LOGIN_LIMIT
            if not blocked:
                if row is None or now >= row["window_start"] + LOGIN_WINDOW:
                    conn.execute(
                        "INSERT INTO auth_login_limits(peer_hash,window_start,attempts) VALUES(?,?,1) "
                        "ON CONFLICT(peer_hash) DO UPDATE SET window_start=excluded.window_start,attempts=1", (key, now))
                else:
                    conn.execute("UPDATE auth_login_limits SET attempts=attempts+1 WHERE peer_hash=?", (key,))
        if blocked:
            raise RateLimited("Too many login attempts; try again later")

    def login(self, username: str, password: str, peer: str, previous_token: str | None = None) -> SessionResult:
        self._login_slot(peer)
        try:
            username = normalize_username(username)
        except ValidationError:
            raise Unauthorized("Invalid credentials") from None
        with self.database.read() as conn:
            row = conn.execute(
                "SELECT a.*,u.version,u.status FROM auth_accounts a JOIN users u ON u.user_id=a.user_id "
                "WHERE a.username=?", (username,)).fetchone()
        password_hash = row["password_hash"] if row is not None else _DUMMY_HASH
        verified = verify_password(password, password_hash)
        if not verified or row is None or row["status"] != "active":
            with self.database.transaction() as conn:
                self.audit(conn, None, "login.failed", None)
            raise Unauthorized("Invalid credentials")
        with self.database.transaction() as conn:
            current = conn.execute(
                "SELECT a.*,u.version,u.status FROM auth_accounts a JOIN users u ON u.user_id=a.user_id "
                "WHERE a.username=?", (username,)).fetchone()
            if (current is None or current["status"] != "active" or current["version"] != row["version"]
                    or current["password_hash"] != password_hash):
                raise Unauthorized("Invalid credentials")
            if previous_token is not None:
                try:
                    previous_hash = token_digest(previous_token)
                except Unauthorized:
                    previous_hash = None
                if previous_hash is not None:
                    conn.execute("UPDATE auth_sessions SET revoked=1,version=version+1 WHERE token_hash=? AND revoked=0",
                                 (previous_hash,))
            token = secrets.token_urlsafe(32)
            csrf = secrets.token_urlsafe(32)
            now = self.clock()
            expires = now + self.session_ttl
            conn.execute(
                "INSERT INTO auth_sessions(token_hash,user_id,user_version,csrf_token,created_at,expires_at) "
                "VALUES(?,?,?,?,?,?)", (token_digest(token), current["user_id"], current["version"], csrf, now, expires))
            user = CollaborationStore(self.database, connection=conn).get_user(current["user_id"])
            self.audit(conn, user.user_id, "login.succeeded", user.user_id)
            return SessionResult(user, bool(current["is_admin"]), token, csrf, expires)

    def logout(self, token: str, csrf: str) -> None:
        with self.access(token, csrf, write=True) as access:
            access.conn.execute("UPDATE auth_sessions SET revoked=1,version=version+1 WHERE token_hash=?",
                                (token_digest(token),))
            self.audit(access.conn, access.user.user_id, "session.revoked", access.user.user_id)

    def create_invitation(self, token: str, csrf: str, username: str, display_name: str) -> InvitationResult:
        username = normalize_username(username)
        _text(display_name, "display_name")
        with self.access(token, csrf, admin=True, write=True) as access:
            conn = access.conn
            now = self.clock()
            if conn.execute("SELECT 1 FROM auth_accounts WHERE username=?", (username,)).fetchone():
                raise Conflict("Username is not available")
            if conn.execute("SELECT 1 FROM auth_invitations WHERE username=? AND status='pending' AND expires_at>?",
                            (username, now)).fetchone():
                raise Conflict("An invitation is already pending")
            invitation_id = uuid4().hex
            invitation_token = secrets.token_urlsafe(32)
            expires = now + self.invitation_ttl
            conn.execute(
                "INSERT INTO auth_invitations(invitation_id,token_hash,username,display_name,created_by,created_by_version,created_at,expires_at) "
                "VALUES(?,?,?,?,?,?,?,?)",
                (invitation_id, token_digest(invitation_token), username, display_name, access.user.user_id,
                 access.user.version, now, expires))
            self.audit(conn, access.user.user_id, "invitation.created", invitation_id, target_version=1)
            return InvitationResult(invitation_id, invitation_token, expires, 1)

    def revoke_invitation(self, token: str, csrf: str, invitation_id: str, expected_version: int) -> dict:
        with self.access(token, csrf, admin=True, write=True) as access:
            row = access.conn.execute("SELECT * FROM auth_invitations WHERE invitation_id=?", (invitation_id,)).fetchone()
            if row is None:
                raise NotFound("Resource not found")
            _version(row["version"], expected_version)
            if row["status"] != "pending":
                raise Conflict("Invitation is no longer pending")
            access.conn.execute("UPDATE auth_invitations SET status='revoked',version=version+1 WHERE invitation_id=?",
                                (invitation_id,))
            self.audit(access.conn, access.user.user_id, "invitation.revoked", invitation_id,
                       target_version=row["version"] + 1)
            return {"invitation_id": invitation_id, "status": "revoked", "version": row["version"] + 1}

    def register(self, invitation_token: str, password: str) -> User:
        token_hash = token_digest(invitation_token)
        with self.database.read() as conn:
            row = conn.execute("SELECT * FROM auth_invitations WHERE token_hash=?", (token_hash,)).fetchone()
            if row is None or row["status"] != "pending" or row["expires_at"] <= self.clock():
                raise Conflict("Invitation is not available")
        password_hash = hash_password(password)
        with self.database.transaction() as conn:
            row = conn.execute("SELECT * FROM auth_invitations WHERE token_hash=?", (token_hash,)).fetchone()
            if row is None or row["status"] != "pending" or row["expires_at"] <= self.clock():
                raise Conflict("Invitation is not available")
            inviter = conn.execute("SELECT u.status,u.version,a.is_admin FROM users u JOIN auth_accounts a ON a.user_id=u.user_id "
                                   "WHERE u.user_id=?", (row["created_by"],)).fetchone()
            if (inviter is None or inviter["status"] != "active" or not inviter["is_admin"]
                    or inviter["version"] != row["created_by_version"]):
                raise Conflict("Invitation is not available")
            if conn.execute("SELECT 1 FROM auth_accounts WHERE username=?", (row["username"],)).fetchone():
                raise Conflict("Username is not available")
            store = CollaborationStore(self.database, connection=conn)
            user = store.create_user(row["display_name"])
            conn.execute("INSERT INTO auth_accounts(user_id,username,password_hash) VALUES(?,?,?)",
                         (user.user_id, row["username"], password_hash))
            conn.execute("UPDATE auth_invitations SET status='accepted',accepted_user_id=?,version=version+1 "
                         "WHERE invitation_id=?", (user.user_id, row["invitation_id"]))
            self.audit(conn, user.user_id, "account.registered", user.user_id, target_version=user.version)
            self.audit(conn, user.user_id, "invitation.accepted", row["invitation_id"], target_version=row["version"] + 1)
            return user
