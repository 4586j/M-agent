"""协作服务机器接口的 HTTP 客户端：只带本机实例令牌，只认服务端返回的字段。

本模块不理解业务，也不导入协作服务的任何内部模块：新表、新进程和新令牌与旧
brain/worker 完全分开，旧入口不需要依赖它，它也不复用旧 /task 的请求契约。
"""
from __future__ import annotations

from dataclasses import dataclass
from ipaddress import ip_address
import base64
import hashlib
import re
from typing import Any
from urllib.parse import urlsplit

import httpx

PATH_PREFIX = "/collab/v1"
PROTOCOL_VERSION = "1"
MAX_JSON_BYTES = 4 * 1024 * 1024
_ID_PATTERN = re.compile(r"^[0-9a-f]{32}$")
_HEADER_PATTERN = re.compile(r"^[0-9a-f]{64}$")


class WorkerError(RuntimeError):
    """One collaboration service outcome the worker has to react to."""

    status = 0

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class Unauthorized(WorkerError):
    status = 401


class Forbidden(WorkerError):
    status = 403


class Missing(WorkerError):
    status = 404


class Conflict(WorkerError):
    """The service no longer agrees with what this worker believed."""

    status = 409


class Rejected(WorkerError):
    status = 422


class Unavailable(WorkerError):
    status = 503


class TransportError(WorkerError):
    status = 0


_STATUS_ERRORS: dict[int, type[WorkerError]] = {
    401: Unauthorized, 403: Forbidden, 404: Missing, 409: Conflict, 422: Rejected,
    503: Unavailable,
}


def digest_of(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def encode_content(data: bytes) -> str:
    if not isinstance(data, bytes):
        raise Rejected("Artifact content must be bytes")
    return base64.b64encode(data).decode("ascii")


def validate_base_url(value: str) -> str:
    """Accept one bare origin plus the machine prefix; anything else is refused early.

    Credentials, queries and fragments would let a registration smuggle an extra
    target, and a non-loopback plain HTTP endpoint would send the instance token in clear.
    """
    if not isinstance(value, str) or not 8 <= len(value) <= 512 \
            or re.search(r"[\x00-\x20\x7f\s\\]", value):
        raise Rejected("Base URL is invalid")
    try:
        parsed = urlsplit(value)
        host, port = parsed.hostname, parsed.port
    except ValueError:
        raise Rejected("Base URL is invalid") from None
    if (parsed.scheme not in ("http", "https") or not host or parsed.username is not None
            or parsed.password is not None or parsed.query or parsed.fragment
            or port is not None and not 1 <= port <= 65535):
        raise Rejected("Base URL is invalid")
    path = parsed.path.rstrip("/")
    if path != PATH_PREFIX:
        raise Rejected(f"Base URL path must be {PATH_PREFIX}")
    if parsed.scheme == "http":
        try:
            loopback = ip_address(host).is_loopback
        except ValueError:
            loopback = host == "localhost"
        if not loopback:
            raise Rejected("Plain HTTP base URLs must point at loopback")
    return f"{parsed.scheme}://{parsed.netloc}{path}"


def validate_identifier(value: str, field_name: str) -> str:
    if not isinstance(value, str) or not _ID_PATTERN.fullmatch(value):
        raise Rejected(f"Invalid {field_name}")
    return value


def digest_header(response: httpx.Response, name: str) -> str:
    values = response.headers.get_list(name)
    if len(values) != 1 or not _HEADER_PATTERN.fullmatch(values[0] or ""):
        raise Conflict(f"Missing or invalid {name}")
    return values[0]


@dataclass(frozen=True)
class Fetched:
    """Raw bytes plus the digest the service claimed for them."""
    content: bytes
    digest: str


class CollaborationClient:
    """Thin synchronous client for the /machine endpoints of one collaboration service."""

    def __init__(self, base_url: str, *, instance_token: str | None = None,
                 transport: httpx.BaseTransport | None = None,
                 timeout: float = 20.0, max_bytes: int = MAX_JSON_BYTES):
        self.base_url = validate_base_url(base_url)
        origin = self.base_url[: -len(PATH_PREFIX)] if self.base_url.endswith(PATH_PREFIX) \
            else self.base_url
        self.origin = origin
        self.instance_token = instance_token
        self.max_bytes = max_bytes
        headers = {"Origin": origin, "Accept": "application/json, text/markdown, */*"}
        self._client = httpx.Client(base_url=self.base_url, headers=headers,
                                    timeout=httpx.Timeout(timeout, connect=min(timeout, 10.0)),
                                    transport=transport, follow_redirects=False,
                                    trust_env=False)

    def close(self) -> None:
        self._client.close()

    # --- plumbing --------------------------------------------------------------

    def _headers(self, extra: dict[str, str] | None = None, *,
                 token: str | None = None) -> dict[str, str]:
        actual = dict(extra or {})
        bearer = token if token is not None else self.instance_token
        if bearer:
            actual["X-Instance-Token"] = bearer
        return actual

    def _send(self, method: str, path: str, *, token: str | None = None,
              node_token: str | None = None, **kwargs) -> httpx.Response:
        headers = self._headers(kwargs.pop("headers", None), token=token)
        if node_token:
            headers["X-Node-Token"] = node_token
        try:
            response = self._client.request(method, path, headers=headers, **kwargs)
        except httpx.HTTPError as error:
            raise TransportError(type(error).__name__) from None
        error = _STATUS_ERRORS.get(response.status_code)
        if error is not None:
            raise error(f"{method} {path} -> {response.status_code}")
        if response.status_code >= 400:
            raise TransportError(f"{method} {path} -> {response.status_code}")
        if len(response.content) > self.max_bytes:
            raise Conflict("Response is larger than the configured limit")
        return response

    def json(self, method: str, path: str, **kwargs) -> Any:
        response = self._send(method, path, **kwargs)
        try:
            return response.json()
        except ValueError:
            raise Conflict("Response is not valid JSON") from None

    # --- instance lifecycle ------------------------------------------------------

    def claim(self, *, node_token: str, claim_token: str, capacity: int,
              isolation_mode: str, isolation_attestation: str, workspace_ref: str,
              provider_ref: str, allowed_tools: list[str],
              env_allowlist: list[str]) -> dict:
        body = {"claim_token": claim_token, "protocol_version": PROTOCOL_VERSION,
                "capacity": capacity, "isolation_mode": isolation_mode,
                "isolation_attestation": isolation_attestation,
                "policy": {"workspace_ref": workspace_ref, "provider_ref": provider_ref,
                           "allowed_tools": allowed_tools, "env_allowlist": env_allowlist}}
        result = self.json("POST", "/machine/instances/claim", node_token=node_token,
                           json=body, token="")
        if not isinstance(result, dict) or not isinstance(result.get("instance_token"), str):
            raise Conflict("Claim response is missing its instance token")
        return result

    def use_instance_token(self, instance_token: str) -> None:
        self.instance_token = instance_token

    def heartbeat(self) -> dict:
        return self.json("POST", "/machine/instances/heartbeat")

    def describe(self) -> dict:
        return self.json("GET", "/machine/instances/me")

    # --- pull paths --------------------------------------------------------------

    def held_attempts(self) -> list[dict]:
        result = self.json("GET", "/machine/instances/me/attempts")
        if not isinstance(result, list) or any(not isinstance(entry, dict) for entry in result):
            raise Conflict("Held attempts are malformed")
        return result

    def package(self, attempt_id: str) -> Fetched:
        attempt_id = validate_identifier(attempt_id, "attempt_id")
        response = self._send("GET", f"/machine/attempts/{attempt_id}/package")
        claimed = digest_header(response, "X-Input-Hash")
        payload = response.content
        if digest_of(payload) != claimed:
            raise Conflict("Task package digest does not match its own header")
        return Fetched(payload, claimed)

    def requirement(self, attempt_id: str) -> Fetched:
        attempt_id = validate_identifier(attempt_id, "attempt_id")
        response = self._send("GET", f"/machine/attempts/{attempt_id}/requirement")
        claimed = digest_header(response, "X-Content-Hash")
        revisions = response.headers.get_list("X-Revision-Id")
        if len(revisions) != 1 or not _ID_PATTERN.fullmatch(revisions[0] or ""):
            raise Conflict("Revision id is missing or invalid")
        if digest_of(response.content) != claimed:
            raise Conflict("Requirement digest does not match its own header")
        return Fetched(response.content, claimed)

    def security_reviews(self, attempt_id: str) -> list[dict]:
        attempt_id = validate_identifier(attempt_id, "attempt_id")
        result = self.json("GET", f"/machine/attempts/{attempt_id}/security-reviews")
        if not isinstance(result, list) or any(not isinstance(entry, dict) for entry in result):
            raise Conflict("Security reviews are malformed")
        return result

    def artifact(self, attempt_id: str, artifact_id: str) -> Fetched:
        attempt_id = validate_identifier(attempt_id, "attempt_id")
        artifact_id = validate_identifier(artifact_id, "artifact_id")
        response = self._send("GET", f"/machine/attempts/{attempt_id}/artifacts/{artifact_id}")
        claimed = digest_header(response, "X-Content-Hash")
        if digest_of(response.content) != claimed:
            raise Conflict("Artifact digest does not match its own header")
        return Fetched(response.content, claimed)

    # --- callbacks ---------------------------------------------------------------

    def _callback(self, stage: str, attempt_id: str, body: dict,
                  claims: dict) -> dict:
        attempt_id = validate_identifier(attempt_id, "attempt_id")
        result = self.json("POST", f"/machine/attempts/{attempt_id}/{stage}",
                           json={**body, **claims})
        if not isinstance(result, dict) or not isinstance(result.get("attempt"), dict):
            raise Conflict("Callback response is malformed")
        return result

    def ack(self, attempt_id: str, claims: dict) -> dict:
        return self._callback("ack", attempt_id, {}, claims)

    def start(self, attempt_id: str, claims: dict, *, trace_id: str | None = None) -> dict:
        return self._callback("start", attempt_id, {"trace_id": trace_id}, claims)

    def renew_lease(self, attempt_id: str, claims: dict) -> dict:
        return self._callback("lease", attempt_id, {}, claims)

    def artifact_upload(self, attempt_id: str, claims: dict, *, name: str, media_type: str,
                        content: bytes) -> dict:
        digest = digest_of(content)
        return self._callback("artifacts", attempt_id, {
            "name": name, "media_type": media_type, "sha256": digest,
            "content": encode_content(content)}, claims)

    def result(self, attempt_id: str, claims: dict, *, status: str, summary: str,
               artifacts: list[dict], usage_units: int,
               trace_id: str | None = None) -> dict:
        return self._callback("result", attempt_id, {
            "status": status, "summary": summary, "artifacts": artifacts,
            "usage_units": usage_units, "trace_id": trace_id}, claims)

    def cancel(self, attempt_id: str, claims: dict, *, reason: str = "") -> dict:
        return self._callback("cancel", attempt_id, {"reason": reason}, claims)
