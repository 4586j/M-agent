"""独立协作执行实例的契约测试：真实协作服务与真实路由，不占端口、不调模型、不改旧入口。

分三层验证：
1. 本机持久执行日志（重复投递只启动一个执行者，重启后先对账）；
2. 输入校验与安全 gate 的拒绝路径（任务包或原文对不上时不执行、不回传）；
3. 走真实 HTTP 路由的闭环（wake 提示→重新拉取→校验→ack/start/租约/产物/结果）。
"""
from __future__ import annotations

from contextlib import contextmanager
from types import SimpleNamespace
import asyncio
import dataclasses
import json
from pathlib import Path
import secrets
import subprocess
import sys
import threading
import time

import httpx
import pytest
from fastapi.testclient import TestClient

WORKER_DIR = Path(__file__).resolve().parents[1]
RUNTIME_DIR = WORKER_DIR.parent / "workspace" / "agent" / "orchestrator" / "runtime"
sys.path.insert(0, str(WORKER_DIR))
sys.path.insert(0, str(RUNTIME_DIR))

from collaboration.app import AppConfig, create_app
from collaboration.auth import Access, token_digest
from collaboration.database import Database
from collaboration.dispatch import DispatchService, TOPIC_ATTEMPT_ASSIGNED, TOPIC_ATTEMPT_REVOKED
from collaboration.requirements import RequirementService
from collaboration.reviews import ReviewService
from collaboration.store import CollaborationStore

from collaboration_app import (ACCEPTED, CLOSED, FakeRunner, FINISHED, MAX_ARTIFACT_BYTES,
                               PACKAGE_SCHEMA_VERSION, RUNNING, STALE, UNKNOWN,
                               CollaborationWorker, Produced, RunOutcome, WorkerSettings,
                               create_worker_app, read_instance_token, write_instance_token,
                               _Claims)
from collaboration_client import (CollaborationClient, Conflict, Fetched, Missing, Rejected,
                                 Unauthorized, digest_of, validate_base_url)
from collaboration_journal import (DISCOVERED, PAUSED_GATE, RECONCILING, REFUSED, REPORTED,
                                   STARTED, VERIFIED, ExecutionJournal, JournalError)

ORIGIN = "http://127.0.0.1:7400"
BASE_URL = ORIGIN + "/collab/v1"
ATTEMPT = "a" * 32
WORK_ITEM = "b" * 32
REVISION = "c" * 32
REQUIREMENT = "d" * 32
ORIGINAL = "# 需求原文\n\n第二条验收\n"
ORIGINAL_BYTES = ORIGINAL.encode("utf-8")


class SyncAsgi(httpx.BaseTransport):
    """让同步客户端直接驱动真实 ASGI 路由：不开端口，也不绕过任何中间件。"""

    def __init__(self, app):
        self._transport = httpx.ASGITransport(app=app)

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        return asyncio.run(self._call(request))

    async def _call(self, request):
        response = await self._transport.handle_async_request(request)
        await response.aread()
        result = httpx.Response(response.status_code, headers=list(response.headers.raw),
                                content=response.content)
        await response.aclose()
        return result


# --- 本机日志的独立用例 ---------------------------------------------------------------

def seeded(tmp_path, *, input_hash: str = "e" * 64) -> ExecutionJournal:
    ledger = ExecutionJournal(tmp_path / "journal.sqlite3")
    ledger.discover(attempt_id=ATTEMPT, work_item_id=WORK_ITEM, input_hash=input_hash,
                    control_epoch=2)
    return ledger


def verified(tmp_path, *, input_hash: str = "e" * 64) -> ExecutionJournal:
    ledger = seeded(tmp_path, input_hash=input_hash)
    ledger.record_inputs(ATTEMPT, input_hash=input_hash, requirement_hash="f" * 64,
                         package_path=f"{ATTEMPT}/package.json",
                         requirement_path=f"{ATTEMPT}/requirement.md",
                         control_epoch=2, lease_epoch=3)
    return ledger


def test_journal_allows_only_one_executor_to_start(tmp_path):
    ledger = verified(tmp_path)
    assert ledger.get(ATTEMPT).state == VERIFIED
    assert ledger.claim_execution(ATTEMPT, executor="holder-a") is True
    assert ledger.claim_execution(ATTEMPT, executor="holder-b") is False
    assert ledger.release_execution(ATTEMPT, executor="holder-b", state=RECONCILING) is False
    entry = ledger.get(ATTEMPT)
    assert (entry.state, entry.executor) == (STARTED, "holder-a")
    assert ledger.release_execution(ATTEMPT, executor="holder-a", state=REPORTED,
                                   reason="结果已回传") is True
    assert ledger.get(ATTEMPT).executor is None
    ledger.close()


def test_journal_requires_verified_inputs_before_execution(tmp_path):
    ledger = seeded(tmp_path)
    assert ledger.claim_execution(ATTEMPT, executor="holder-a") is False
    assert ledger.get(ATTEMPT).state == DISCOVERED
    with pytest.raises(JournalError):
        ledger.note(ATTEMPT, STARTED, reason="只有占位写入可以标记执行中")
    ledger.close()


def test_journal_answers_a_repeated_wake_without_running_again(tmp_path):
    ledger = verified(tmp_path)
    ledger.claim_execution(ATTEMPT, executor="holder-a")
    ledger.release_execution(ATTEMPT, executor="holder-a", state=REPORTED, reason="已回传")
    outcome, entry = ledger.discover(attempt_id=ATTEMPT, work_item_id=WORK_ITEM,
                                     input_hash="e" * 64, control_epoch=2)
    assert (outcome, entry.state, entry.settled) == ("known", REPORTED, True)
    with pytest.raises(JournalError):
        ledger.note(ATTEMPT, RECONCILING, reason="终局答案不能改写")
    ledger.close()


def test_journal_sends_a_changed_digest_to_reconciliation(tmp_path):
    ledger = seeded(tmp_path)
    outcome, entry = ledger.discover(attempt_id=ATTEMPT, work_item_id=WORK_ITEM,
                                     input_hash="1" * 64, control_epoch=2)
    assert (outcome, entry.state) == ("conflicting", RECONCILING)
    ledger.record_inputs(ATTEMPT, input_hash="1" * 64, requirement_hash="f" * 64,
                         package_path=f"{ATTEMPT}/package.json",
                         requirement_path=f"{ATTEMPT}/requirement.md",
                         control_epoch=2, lease_epoch=1)
    ledger.claim_execution(ATTEMPT, executor="holder-a")
    ledger.release_execution(ATTEMPT, executor="holder-a", state=REPORTED)
    # 已结束的尝试不会被伪造的 wake 复活。
    outcome, entry = ledger.discover(attempt_id=ATTEMPT, work_item_id=WORK_ITEM,
                                     input_hash="2" * 64, control_epoch=2)
    assert (outcome, entry.state) == ("known", REPORTED)
    ledger.close()


def test_restart_turns_an_in_flight_attempt_into_reconciliation(tmp_path):
    ledger = verified(tmp_path)
    ledger.claim_execution(ATTEMPT, executor="holder-a")
    assert ledger.recover() == [ATTEMPT]
    entry = ledger.get(ATTEMPT)
    assert (entry.state, entry.executor) == (RECONCILING, None)
    assert "restarted" in ledger.events(ATTEMPT)[0]["reason"]
    ledger.close()
    reopened = ExecutionJournal(tmp_path / "journal.sqlite3")
    assert reopened.recover() == []
    assert reopened.get(ATTEMPT).state == RECONCILING
    reopened.close()


@pytest.mark.parametrize("changes", [
    {"attempt_id": "not-hex"},
    {"work_item_id": "also-not-hex"},
    {"input_hash": "short"},
    {"control_epoch": True},
    {"control_epoch": "1"},
])
def test_journal_refers_to_values_it_cannot_trust(tmp_path, changes):
    values = {"attempt_id": ATTEMPT, "work_item_id": WORK_ITEM, "input_hash": "e" * 64,
              "control_epoch": 1}
    values.update(changes)
    ledger = ExecutionJournal(tmp_path / "journal.sqlite3")
    with pytest.raises(JournalError):
        ledger.discover(**values)
    assert ledger.entries() == []
    ledger.close()


def test_journal_keeps_only_relative_local_paths(tmp_path):
    ledger = seeded(tmp_path)
    for bad in ("/etc/passwd", "../escape/package.json", ""):
        with pytest.raises(JournalError):
            ledger.record_inputs(ATTEMPT, input_hash="e" * 64, requirement_hash="f" * 64,
                                 package_path=bad, requirement_path=f"{ATTEMPT}/r.md",
                                 control_epoch=2, lease_epoch=1)
    assert ledger.get(ATTEMPT).state == DISCOVERED
    ledger.close()


# --- 客户端与本机配置的护栏 -------------------------------------------------------------

def test_the_client_refuses_a_target_that_would_move_the_token():
    for bad in ("http://example.com/collab/v1", "https://example.com/wrong/prefix",
                "https://example.com/collab/v1?next=other",
                "https://user:pass@example.com/collab/v1", "not a url"):
        with pytest.raises(Rejected):
            validate_base_url(bad)
    assert validate_base_url("https://collab.example.net/collab/v1") == \
        "https://collab.example.net/collab/v1"
    assert validate_base_url(BASE_URL) == BASE_URL


def test_settings_refuse_a_remote_listen_host_without_opt_in(tmp_path):
    with pytest.raises(Rejected):
        WorkerSettings(base_url=BASE_URL, state_dir=tmp_path, listen_host="0.0.0.0")
    WorkerSettings(base_url=BASE_URL, state_dir=tmp_path, listen_host="0.0.0.0",
                   allow_remote=True)
    with pytest.raises(Rejected):
        WorkerSettings(base_url=BASE_URL, state_dir=tmp_path, wake_path="/health")
    with pytest.raises(Rejected):
        WorkerSettings(base_url=BASE_URL, state_dir=tmp_path, runner="real")
    assert WorkerSettings(base_url=BASE_URL, state_dir=tmp_path).journal_path == \
        tmp_path / "journal.sqlite3"


def test_instance_token_prefers_the_environment(tmp_path, monkeypatch):
    settings = WorkerSettings(base_url=BASE_URL, state_dir=tmp_path)
    write_instance_token(settings, "d" * 32)
    monkeypatch.delenv("COLLAB_INSTANCE_TOKEN", raising=False)
    assert read_instance_token(settings) == "d" * 32
    monkeypatch.setenv("COLLAB_INSTANCE_TOKEN", "e" * 32)
    assert read_instance_token(settings) == "e" * 32
    with pytest.raises(Rejected):
        write_instance_token(settings, "short")


def test_claims_only_move_forward_when_the_service_agrees():
    claims = _Claims("e" * 64, 2, 3)
    claims.advance({"attempt": {"input_hash": "e" * 64, "control_epoch": 2, "lease_epoch": 4}})
    assert (claims.control_epoch, claims.lease_epoch) == (2, 4)
    with pytest.raises(Conflict):
        claims.advance({"attempt": {"input_hash": "f" * 64, "control_epoch": 2, "lease_epoch": 5}})
    with pytest.raises(Conflict):
        claims.advance({"attempt": {"input_hash": "e" * 64, "control_epoch": 2}})
    assert claims.snapshot()["lease_epoch"] == 4


# --- 校验与安全 gate（用桩服务精确构造坏输入） ------------------------------------------

class StubClient:
    """只回答机器接口中被测分支所需的字段，并记录收到的每一次回调。"""

    def __init__(self, *, held: dict, package: bytes, requirement: bytes, reviews=()):
        self.held = held
        self.package_bytes = package
        self.requirement_bytes = requirement
        self.reviews = list(reviews)
        self.calls: list[str] = []
        self.artifacts: list[tuple[str, bytes]] = []
        self.reported: dict | None = None
        self.instance_token = "stub-instance-token"

    def held_attempts(self):
        return [dict(self.held)]

    def package(self, attempt_id):
        return Fetched(self.package_bytes, digest_of(self.package_bytes))

    def requirement(self, attempt_id):
        return Fetched(self.requirement_bytes, digest_of(self.requirement_bytes))

    def security_reviews(self, attempt_id):
        return [dict(entry) for entry in self.reviews]

    def heartbeat(self):
        return {"instance_id": "instance"}

    def close(self):
        return None

    def _call(self, stage, **changes):
        self.calls.append(stage)
        attempt = dict(self.held)
        attempt.update(changes)
        return {"outcome": "accepted", "attempt": attempt}

    def ack(self, attempt_id, claims):
        return self._call("ack", status="acked")

    def start(self, attempt_id, claims, *, trace_id=None):
        return self._call("start", status="running", trace_id=trace_id)

    def renew_lease(self, attempt_id, claims):
        return self._call("lease", lease_epoch=self.held["lease_epoch"] + 1)

    def artifact_upload(self, attempt_id, claims, *, name, media_type, content):
        self.artifacts.append((name, content))
        return self._call("artifact")

    def result(self, attempt_id, claims, *, status, summary, artifacts, usage_units,
               trace_id=None):
        self.reported = {"status": status, "summary": summary, "artifacts": artifacts,
                         "usage_units": usage_units}
        return self._call("result", status=status, usage_units=usage_units)

    def cancel(self, attempt_id, claims, *, reason=""):
        return self._call("cancel", status="cancelled")


def stub_held(input_hash: str, **changes) -> dict:
    view = {"attempt_id": ATTEMPT, "work_item_id": WORK_ITEM, "plan_id": "1" * 32,
            "requirement_id": REQUIREMENT, "revision_id": REVISION, "number": 1,
            "schema_version": PACKAGE_SCHEMA_VERSION, "input_hash": input_hash,
            "status": "assigned", "lease_epoch": 3, "control_epoch": 2,
            "lease_expires_at": time.time() + 120, "usage_units": 0, "result_hash": None,
            "trace_id": None, "version": 1, "updated_at": time.time()}
    view.update(changes)
    return view


def stub_package(held: dict, *, original: bytes = ORIGINAL_BYTES, document: dict = None,
                 **changes) -> bytes:
    entry = {"kind": "requirement_text", "revision_id": held["revision_id"],
             "content_hash": digest_of(original), "size_bytes": len(original),
             "media_type": "text/markdown;charset=utf-8", "href": "requirement",
             "complete": True}
    entry.update(document or {})
    payload = {"schema_version": PACKAGE_SCHEMA_VERSION, "work_item_id": held["work_item_id"],
               "requirement_id": REQUIREMENT, "revision_id": held["revision_id"],
               "documents": [entry], "subtask": {"key": "A", "objective": "完成目标"},
               "policy": {"binding_id": "2" * 32, "binding_version": 1,
                          "allowed_tools": ["Read"], "budget_units": 4}}
    payload.update(changes)
    return json.dumps(payload, ensure_ascii=False).encode("utf-8")


def stub_worker(tmp_path, *, held: dict, package: bytes, original: bytes = ORIGINAL_BYTES,
                reviews=(), runner=Ellipsis):
    """A worker over a stubbed service view: the refusal paths must never reach the callbacks."""
    client = StubClient(held=held, package=package, requirement=original, reviews=reviews)
    settings = WorkerSettings(base_url=BASE_URL, state_dir=tmp_path)
    worker = CollaborationWorker(client, ExecutionJournal(settings.journal_path),
                                 settings=settings,
                                 runner=FakeRunner() if runner is Ellipsis else runner,
                                 executor="stub-holder")
    return worker


def drive(worker, held: dict) -> str:
    worker.journal.discover(attempt_id=held["attempt_id"], work_item_id=held["work_item_id"],
                            input_hash=held["input_hash"], control_epoch=held["control_epoch"])
    return worker.run(held["attempt_id"])


def test_a_package_that_disagrees_with_the_service_is_refused(tmp_path):
    readable = stub_package(stub_held(b""))
    unreadable = b"not json at all"
    cases = [
        (stub_held("f" * 64), readable,
         "the task package does not match the digest the service holds"),
        (stub_held(digest_of(unreadable)), unreadable, "the task package is not readable JSON"),
        (None, stub_package(stub_held(b""), schema_version="other-v1"),
         "the task package is not the schema this worker speaks"),
        (None, stub_package(stub_held(b""), work_item_id="9" * 32),
         "the task package describes a different work item"),
    ]
    for index, (held, package, reason) in enumerate(cases):
        directory = tmp_path / f"package-{index}"
        held = held or stub_held(digest_of(package))
        worker = stub_worker(directory, held=held, package=package)
        assert drive(worker, held) == CLOSED, index
        entry = worker.journal.get(ATTEMPT)
        assert (entry.state, entry.reason) == (REFUSED, reason), index
        assert worker.client.calls == []
        worker.journal.close()


def test_only_the_complete_approved_original_passes_verification(tmp_path):
    held = stub_held("e" * 64)
    utf8_bad = b"\xff\xfe not utf8"
    cases = [
        ({"document": {"complete": False}}, ORIGINAL_BYTES,
         "the requirement original is not the complete approved one"),
        ({"document": {"kind": "summary"}}, ORIGINAL_BYTES,
         "the requirement original is not the complete approved one"),
        ({"document": {"href": "other"}}, ORIGINAL_BYTES,
         "the requirement original is not the complete approved one"),
        ({"document": {"revision_id": "9" * 32}}, ORIGINAL_BYTES,
         "the requirement original is not the complete approved one"),
        ({"document": {"content_hash": "1" * 64}}, ORIGINAL_BYTES,
         "the requirement original does not match the task package"),
        ({"document": {"content_hash": None}}, ORIGINAL_BYTES,
         "the requirement original has no usable digest"),
        ({"document": {"content_hash": "not-a-digest"}}, ORIGINAL_BYTES,
         "the requirement original has no usable digest"),
        ({"document": {"size_bytes": 3}}, ORIGINAL_BYTES,
         "the requirement original has an unexpected size"),
        ({"documents": []}, ORIGINAL_BYTES, "the task package is missing the requirement original"),
        ({}, utf8_bad, "the requirement original is not UTF-8"),
    ]
    for index, (changes, original, reason) in enumerate(cases):
        directory = tmp_path / f"original-{index}"
        package = stub_package(held, original=original, **changes)
        worker = stub_worker(directory, held=stub_held(digest_of(package)), package=package,
                             original=original)
        assert drive(worker, worker.client.held) == CLOSED, changes
        entry = worker.journal.get(ATTEMPT)
        assert (entry.state, entry.reason) == (REFUSED, reason), changes
        assert worker.client.calls == []
        worker.journal.close()


def test_the_security_gate_blocks_before_any_callback(tmp_path):
    def review(**changes):
        entry = {"rule_key": "dangerous_command", "decision": "pending", "input_hash": None,
                 "revision_id": REVISION, "binding_version": 1}
        entry.update(changes)
        return entry

    package = stub_package(stub_held(b""))
    held = stub_held(digest_of(package))
    digest = digest_of(package)
    cases = [
        (review(input_hash=digest), PAUSED_GATE),
        (review(input_hash=digest, decision="rejected"), REFUSED),
        (review(input_hash=digest, decision="approved", revision_id="9" * 32), PAUSED_GATE),
        (review(input_hash=digest, decision="approved", binding_version=2), PAUSED_GATE),
        (review(input_hash="1" * 64, decision="approved"), PAUSED_GATE),
    ]
    for index, (entry, state) in enumerate(cases):
        directory = tmp_path / f"gate-{index}"
        worker = stub_worker(directory, held=held, package=package, reviews=[entry])
        assert drive(worker, held) == CLOSED, entry
        journaled = worker.journal.get(ATTEMPT)
        assert journaled.state == state, entry
        assert worker.client.calls == [] and journaled.executor is None, entry
        worker.journal.close()


def test_an_approved_gate_record_lets_the_attempt_run_and_deliver_the_original(tmp_path):
    package = stub_package(stub_held(b""))
    held = stub_held(digest_of(package))
    entry = {"rule_key": "dangerous_command", "decision": "approved",
             "input_hash": digest_of(package), "revision_id": REVISION, "binding_version": 1}
    worker = stub_worker(tmp_path, held=held, package=package, reviews=[entry])
    assert drive(worker, held) == FINISHED
    assert worker.client.calls == ["ack", "start", "artifact", "result"]
    name, content = worker.client.artifacts[0]
    assert name == "simulated-output.md"
    assert f"requirement_characters: {len(ORIGINAL)}".encode("utf-8") in content
    assert b"rework_notes: 0" in content
    assert worker.journal.get(ATTEMPT).state == REPORTED
    worker.journal.close()


def test_the_rework_notes_frozen_into_the_package_reach_the_runner(tmp_path):
    package = stub_package(stub_held(b""), rework=[
        {"attempt_id": REQUIREMENT, "reason": "第二条验收未满足", "reviewed_by": "u-1",
         "reviewed_at": 1.0},
        {"attempt_id": WORK_ITEM, "reason": "缺少回归证据", "reviewed_by": "u-2",
         "reviewed_at": 2.0}])
    held = stub_held(digest_of(package))
    worker = stub_worker(tmp_path, held=held, package=package)
    assert drive(worker, held) == FINISHED
    content = worker.client.artifacts[0][1]
    assert b"rework_notes: 2" in content
    assert "第二条验收未满足".encode("utf-8") in content
    assert "缺少回归证据".encode("utf-8") in content
    worker.journal.close()


def test_a_worker_without_a_runner_pauses_instead_of_running(tmp_path):
    package = stub_package(stub_held(b""))
    held = stub_held(digest_of(package))
    worker = stub_worker(tmp_path, held=held, package=package, runner=None)
    assert drive(worker, held) == CLOSED
    assert worker.journal.get(ATTEMPT).state == PAUSED_GATE
    assert worker.client.calls == []
    assert worker.health()["status"] == "idle"
    worker.journal.close()


def test_a_broken_runner_reports_failure_instead_of_staying_silent(tmp_path):
    def broken(request):
        raise RuntimeError("模型不可用")

    package = stub_package(stub_held(b""))
    held = stub_held(digest_of(package))
    worker = stub_worker(tmp_path, held=held, package=package, runner=broken)
    assert drive(worker, held) == FINISHED
    assert worker.client.calls == ["ack", "start", "result"]
    assert worker.client.reported["status"] == "failed"
    assert worker.journal.get(ATTEMPT).state == REPORTED
    worker.journal.close()


def test_only_statuses_the_protocol_allows_are_reported(tmp_path):
    package = stub_package(stub_held(b""))
    held = stub_held(digest_of(package))
    worker = stub_worker(tmp_path, held=held, package=package,
                         runner=lambda request: RunOutcome(status="accepted", summary="越界状态"))
    assert drive(worker, held) == FINISHED
    assert worker.client.reported["status"] == "failed"
    worker.journal.close()


def test_an_oversized_artifact_is_not_uploaded(tmp_path):
    def too_big(request):
        return RunOutcome(status="submitted", summary="产物过大",
                          produced=(Produced(name="big.md", media_type="text/markdown",
                                             content=b"x" * (MAX_ARTIFACT_BYTES + 1)),))

    package = stub_package(stub_held(b""))
    held = stub_held(digest_of(package))
    worker = stub_worker(tmp_path, held=held, package=package, runner=too_big)
    assert drive(worker, held) == FINISHED
    assert worker.client.artifacts == []
    assert worker.client.reported["status"] == "failed"
    worker.journal.close()


# --- 真实协作服务的闭环 -------------------------------------------------------------------

@contextmanager
def human(env, user):
    with env.database.transaction() as conn:
        store = CollaborationStore(env.database, connection=conn)
        yield Access(store.get_user(user.user_id), False, "", 0.0, store, conn)


def approved_requirement(env):
    with human(env, env.requester) as access:
        requirement = env.requirements.create_requirement(
            access, project_id=env.project.project_id, title="协作需求", content=ORIGINAL,
            acceptance_items=["第一条验收", "第二条验收"])
    with human(env, env.requester) as access:
        requirement, _ = env.requirements.request_review(
            access, project_id=env.project.project_id,
            requirement_id=requirement.requirement_id,
            revision_id=requirement.current_revision_id, reviewer_user_id=env.reviewer.user_id,
            expected_version=requirement.version)
    with human(env, env.reviewer) as access:
        requirement, _ = env.requirements.decide_review(
            access, project_id=env.project.project_id, requirement_id=requirement.requirement_id,
            revision_id=requirement.review_revision_id, decision="approved",
            expected_version=requirement.version)
    return requirement


def ready_work_item(env, requirement, *, estimate=4):
    item = {"key": "A", "title": "A 任务", "objective": "完成目标", "scope": "范围内",
            "non_goals": "不做的事", "duty": "实现", "assignee_user_id": env.worker.user_id,
            "binding_id": env.binding.binding_id, "acceptance_ids": ["AC-001", "AC-002"],
            "estimated_units": estimate, "depends_on": []}
    plan = env.store.create_plan(env.project.project_id, requirement.requirement_id,
                                 revision_id=requirement.approved_revision_id,
                                 revision_hash=requirement.approved_revision_hash,
                                 status="published",
                                 report=json.dumps({"problems": [], "item_count": 1}),
                                 total_units=estimate, created_by=env.reviewer.user_id,
                                 created_at=time.time(), items=[item])
    return env.store.list_work_items(plan.project_id, plan.plan_id)[0]


def register_instance(env, *, project_id, binding, node_name):
    """Register one slot for real, then claim it through the machine endpoint."""
    node_token = secrets.token_urlsafe(32)
    node = env.store.create_node(node_name, token_digest(node_token))
    with human(env, env.owner) as access:
        instance, claim_token = env.app.state.instances.register_slot(
            access, project_id=project_id, binding_id=binding.binding_id, node_id=node.node_id,
            worker_url="http://127.0.0.1:8400/wake")
    client = CollaborationClient(BASE_URL, transport=SyncAsgi(env.app))
    claimed = client.claim(node_token=node_token, claim_token=claim_token, capacity=1,
                           isolation_mode="none", isolation_attestation="",
                           workspace_ref=binding.workspace_ref, provider_ref=binding.provider_ref,
                           allowed_tools=list(binding.allowed_tools), env_allowlist=[])
    client.use_instance_token(claimed["instance_token"])
    client.heartbeat()
    return instance, client, claimed["instance_token"]


@pytest.fixture
def env(tmp_path):
    directory = tmp_path / "service"
    directory.mkdir()
    app = create_app(AppConfig(directory, public_origin=ORIGIN))
    with TestClient(app, base_url=ORIGIN, client=("127.0.0.1", 42000)):
        database = Database(directory / "metadata.sqlite3")
        store = CollaborationStore(database)
        owner, requester, reviewer, worker = (store.create_user(name) for name in
                                              ("负责人", "需求方", "审核人", "执行者"))
        project = store.create_project("协作项目", owner.user_id)
        for member, role in ((requester, "member"), (reviewer, "manager"), (worker, "member")):
            store.add_member(project.project_id, member.user_id, access_role=role)
        agent = store.create_agent(worker.user_id, "执行者的 Agent")
        binding = store.create_binding(project.project_id, agent.agent_id, worker.user_id,
                                       duty="实现", persona_version="persona-v1",
                                       allowed_tools=("Read", "Edit"),
                                       workspace_ref="workspace-a", provider_ref="provider-a")
        pushes = []
        context = SimpleNamespace(
            app=app, database=database, store=store, owner=owner, requester=requester,
            reviewer=reviewer, worker=worker, agent=agent, binding=binding, project=project,
            pushes=pushes, requirements=RequirementService(), reviews=ReviewService(),
            dispatch=DispatchService(database, instances=app.state.instances,
                                     push=lambda url, payload: pushes.append((url, payload))))
        context.instance, context.client, context.instance_token = register_instance(
            context, project_id=project.project_id, binding=binding, node_name="node-a")
        settings = WorkerSettings(base_url=BASE_URL, state_dir=tmp_path / "runtime", runner="fake")
        context.settings = settings
        context.journal = ExecutionJournal(settings.journal_path)
        context.worker_instance = CollaborationWorker(
            CollaborationClient(BASE_URL, instance_token=context.instance_token,
                                transport=SyncAsgi(app)),
            context.journal, settings=settings, runner=FakeRunner(), executor="service-holder")
        context.requirement = approved_requirement(context)
        context.item = ready_work_item(context, context.requirement)
        try:
            yield context
        finally:
            context.worker_instance.close()
            context.client.close()


def wake_payload(env, item=None):
    """真实派发一次，然后把服务端实际推送的字节交给 Worker。"""
    attempt = env.dispatch.dispatch(env.project.project_id,
                                    (item or env.item).work_item_id, requires_real_data=False)
    event = [entry for entry in env.store.list_events(env.project.project_id)
             if entry.topic == TOPIC_ATTEMPT_ASSIGNED][-1]
    env.dispatch.wake(event)
    assert env.pushes[-1][0] == env.instance.worker_url
    return attempt, env.pushes[-1][1]


def accepted_stages(env, attempt_id):
    return [report.stage for report in
            env.store.list_attempt_reports(env.project.project_id, attempt_id) if report.accepted]


def result_reasons(env, attempt_id):
    return [report.reason for report in
            env.store.list_attempt_reports(env.project.project_id, attempt_id)
            if report.stage == "result"]


def stored(env, attempt_id):
    return env.store.get_attempt(env.project.project_id, attempt_id)


def record_gate(env, attempt_id, *, rule_key="dangerous_command"):
    with human(env, env.reviewer) as access:
        return env.reviews.record_rule(access, project_id=env.project.project_id,
                                       attempt_id=attempt_id, rule_key=rule_key)


def decide_gate(env, review, decision):
    with human(env, env.owner) as access:
        return env.reviews.decide_security(access, project_id=env.project.project_id,
                                           security_review_id=review.security_review_id,
                                           decision=decision, reason="人工复核该条规则",
                                           expected_version=review.version)


def test_a_real_wake_runs_the_attempt_once_and_delivers_the_full_original(env):
    attempt, payload = wake_payload(env)
    assert env.worker_instance.wake(payload) == ACCEPTED
    assert env.worker_instance.wait_idle(timeout=20) is True

    delivered = stored(env, attempt.attempt_id)
    assert (delivered.status, delivered.usage_units) == ("submitted", 1)
    assert delivered.trace_id == f"collab:{attempt.attempt_id}"
    assert accepted_stages(env, attempt.attempt_id) == ["ack", "start", "artifact", "result"]

    artifacts = env.store.list_artifacts(env.project.project_id, attempt.attempt_id)
    assert [entry.name for entry in artifacts] == ["simulated-output.md"]
    body = env.store.get_artifact_content(env.project.project_id,
                                         artifacts[0].artifact_id).decode("utf-8")
    assert "simulated: true" in body
    assert f"requirement_sha256: {digest_of(ORIGINAL_BYTES)}" in body
    assert f"requirement_characters: {len(ORIGINAL)}" in body

    entry = env.journal.get(attempt.attempt_id)
    assert (entry.state, entry.executor) == (REPORTED, None)
    assert entry.input_hash == attempt.input_hash
    frozen = env.settings.work_root / attempt.attempt_id / \
        f"package-{attempt.input_hash[:16]}.json"
    assert frozen.read_bytes() == attempt.package_json.encode("utf-8")
    original = env.settings.work_root / attempt.attempt_id / \
        f"requirement-{entry.requirement_hash[:16]}.md"
    assert original.read_text(encoding="utf-8") == ORIGINAL
    assert env.store.get_revision_content(env.project.project_id, attempt.requirement_id,
                                         attempt.revision_id) == ORIGINAL


def test_a_repeated_wake_does_not_advance_the_attempt_twice(env):
    attempt, payload = wake_payload(env)
    assert env.worker_instance.wake(payload) == ACCEPTED
    assert env.worker_instance.wait_idle(timeout=20) is True
    reports = accepted_stages(env, attempt.attempt_id)
    assert env.worker_instance.wake(payload) == FINISHED
    assert env.worker_instance.poll() == [FINISHED]
    assert accepted_stages(env, attempt.attempt_id) == reports
    assert stored(env, attempt.attempt_id).status == "submitted"


def test_concurrent_wakes_start_exactly_one_execution(env):
    counted, slow = [], FakeRunner(delay=0.3)

    def runner(request):
        counted.append(request.attempt_id)
        return slow(request)

    env.worker_instance.runner = runner
    attempt, payload = wake_payload(env)
    results = []
    threads = [threading.Thread(target=lambda: results.append(env.worker_instance.wake(payload)))
               for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=20)
    assert env.worker_instance.wait_idle(timeout=20) is True
    assert len(results) == 4 and set(results) <= {ACCEPTED, RUNNING}
    assert counted == [attempt.attempt_id]
    assert accepted_stages(env, attempt.attempt_id).count("result") == 1
    assert stored(env, attempt.attempt_id).status == "submitted"


def test_a_pending_security_review_pauses_and_an_approval_lets_it_run(env):
    attempt, payload = wake_payload(env)
    review = record_gate(env, attempt.attempt_id)
    assert env.worker_instance.wake(payload) == ACCEPTED
    assert env.worker_instance.wait_idle(timeout=20) is True
    assert env.journal.get(attempt.attempt_id).state == PAUSED_GATE
    assert stored(env, attempt.attempt_id).status == "assigned"
    assert accepted_stages(env, attempt.attempt_id) == []

    decide_gate(env, review, "approved")
    assert env.worker_instance.wake(payload) == ACCEPTED
    assert env.worker_instance.wait_idle(timeout=20) is True
    assert env.journal.get(attempt.attempt_id).state == REPORTED
    assert stored(env, attempt.attempt_id).status == "submitted"


def test_a_rejected_security_review_is_refused_without_reporting(env):
    attempt, payload = wake_payload(env)
    decide_gate(env, record_gate(env, attempt.attempt_id), "rejected")
    assert env.worker_instance.wake(payload) == ACCEPTED
    assert env.worker_instance.wait_idle(timeout=20) is True
    entry = env.journal.get(attempt.attempt_id)
    assert entry.state == REFUSED and "rejected" in entry.reason
    assert stored(env, attempt.attempt_id).status == "assigned"
    assert accepted_stages(env, attempt.attempt_id) == []
    assert env.worker_instance.wake(payload) == CLOSED


def test_a_human_withdrawal_is_recorded_as_an_unconfirmed_stop(env):
    attempt, _payload = wake_payload(env)
    item = env.store.get_work_item(env.project.project_id, env.item.work_item_id)
    with human(env, env.owner) as access:
        env.reviews.cancel(access, project_id=env.project.project_id,
                           work_item_id=item.work_item_id, expected_version=item.version,
                           reason="先停止这一项")
    event = [entry for entry in env.store.list_events(env.project.project_id)
             if entry.topic == TOPIC_ATTEMPT_REVOKED][-1]
    assert env.worker_instance.wake(json.loads(event.payload)) == ACCEPTED
    assert env.worker_instance.wait_idle(timeout=20) is True

    entry = env.journal.get(attempt.attempt_id)
    assert entry.state == RECONCILING
    assert "stop confirmation was not accepted" in entry.reason
    assert stored(env, attempt.attempt_id).status == "reconciliation_required"
    assert accepted_stages(env, attempt.attempt_id).count("result") == 0


def test_a_forged_wake_hint_changes_nothing(env):
    attempt, payload = wake_payload(env)
    assert env.worker_instance.wake(dict(payload, input_hash="1" * 64)) == STALE
    assert env.worker_instance.wake(dict(payload, control_epoch=payload["control_epoch"] + 1)) \
        == STALE
    assert env.worker_instance.wake(dict(payload, attempt_id="f" * 32)) == UNKNOWN
    assert env.journal.get(attempt.attempt_id) is None
    assert stored(env, attempt.attempt_id).status == "assigned"
    assert accepted_stages(env, attempt.attempt_id) == []


def test_a_service_serving_other_bytes_is_never_executed(env):
    attempt, payload = wake_payload(env)
    real = env.app.state.execution.package

    def serving_wrong_bytes(instance_token, attempt_id):
        content, row = real(instance_token, attempt_id)
        return b'{"schema_version": "' + PACKAGE_SCHEMA_VERSION.encode() + b'"}', row

    env.app.state.execution.package = serving_wrong_bytes
    try:
        assert env.worker_instance.wake(payload) == ACCEPTED
        assert env.worker_instance.wait_idle(timeout=20) is True
    finally:
        env.app.state.execution.package = real
    # 摘要头与内容自相矛盾：客户端拒绝，本机只做对账，不执行也不回传。
    entry = env.journal.get(attempt.attempt_id)
    assert entry.state == RECONCILING
    assert "package digest does not match" in entry.reason
    assert stored(env, attempt.attempt_id).status == "assigned"
    assert accepted_stages(env, attempt.attempt_id) == []


def test_an_original_that_is_not_the_frozen_one_is_refused(env):
    attempt, payload = wake_payload(env)
    real = env.app.state.execution.requirement_text
    different = ORIGINAL_BYTES + "\r\n# 追加的一行不在被批准的版本里".encode("utf-8")

    def serving_other_original(instance_token, attempt_id):
        content, row = real(instance_token, attempt_id)
        return different, dataclasses.replace(row, revision_hash=digest_of(different))

    env.app.state.execution.requirement_text = serving_other_original
    try:
        assert env.worker_instance.wake(payload) == ACCEPTED
        assert env.worker_instance.wait_idle(timeout=20) is True
    finally:
        env.app.state.execution.requirement_text = real
    entry = env.journal.get(attempt.attempt_id)
    assert entry.state == REFUSED
    assert entry.reason == "the requirement original does not match the task package"
    assert stored(env, attempt.attempt_id).status == "assigned"
    assert accepted_stages(env, attempt.attempt_id) == []


def test_another_projects_instance_cannot_read_this_attempts_inputs(env):
    attempt, _payload = wake_payload(env)
    other = env.store.create_project("另一个项目", env.owner.user_id)
    env.store.add_member(other.project_id, env.worker.user_id)
    binding = env.store.create_binding(other.project_id, env.agent.agent_id, env.worker.user_id,
                                       duty="实现", persona_version="persona-v1",
                                       allowed_tools=("Read",), workspace_ref="workspace-b",
                                       provider_ref="provider-b")
    _instance, client, token = register_instance(env, project_id=other.project_id,
                                                binding=binding, node_name="node-b")
    try:
        assert client.held_attempts() == []
        for name in ("package", "requirement", "security_reviews"):
            with pytest.raises(Missing):
                getattr(client, name)(attempt.attempt_id)
        with pytest.raises(Unauthorized):
            CollaborationClient(BASE_URL, instance_token=token + "x",
                                transport=SyncAsgi(env.app)).held_attempts()
    finally:
        client.close()


def test_the_worker_never_claims_a_result_the_service_did_not_accept(env):
    attempt, payload = wake_payload(env)

    def outage(*args, **kwargs):
        raise Conflict("simulated service outage")

    real = env.app.state.execution.report_result
    env.app.state.execution.report_result = outage
    try:
        assert env.worker_instance.wake(payload) == ACCEPTED
        assert env.worker_instance.wait_idle(timeout=20) is True
    finally:
        env.app.state.execution.report_result = real
    entry = env.journal.get(attempt.attempt_id)
    assert entry.state == RECONCILING
    assert "stop confirmation" not in entry.reason
    assert stored(env, attempt.attempt_id).status == "running"

    # 服务恢复后按同一 attempt 重新上报，结果只被接受一次，用量也只结算一次。
    assert env.worker_instance.wake(payload) == ACCEPTED
    assert env.worker_instance.wait_idle(timeout=20) is True
    assert stored(env, attempt.attempt_id).status == "submitted"
    assert result_reasons(env, attempt.attempt_id).count(None) == 1
    assert accepted_stages(env, attempt.attempt_id).count("result") == 1
    assert stored(env, attempt.attempt_id).usage_units == 1


def test_a_committed_result_with_a_lost_response_is_only_counted_once(env):
    attempt, payload = wake_payload(env)
    real = env.app.state.execution.report_result

    def losing_the_response(*args, **kwargs):
        committed = real(*args, **kwargs)
        assert committed.outcome == "accepted"
        raise Conflict("simulated lost response")

    env.app.state.execution.report_result = losing_the_response
    try:
        assert env.worker_instance.wake(payload) == ACCEPTED
        assert env.worker_instance.wait_idle(timeout=20) is True
    finally:
        env.app.state.execution.report_result = real
    # 服务端已经收下结果，本机却因为超时对不上账：先按对账处理，不谎报已完成。
    assert stored(env, attempt.attempt_id).status == "submitted"
    assert env.journal.get(attempt.attempt_id).state == RECONCILING

    assert env.worker_instance.wake(payload) == ACCEPTED
    assert env.worker_instance.wait_idle(timeout=20) is True
    assert env.journal.get(attempt.attempt_id).state == REPORTED
    assert result_reasons(env, attempt.attempt_id).count(None) == 1
    assert stored(env, attempt.attempt_id).usage_units == 1


# --- 本机 HTTP 面 ---------------------------------------------------------------------------

def test_the_wake_endpoint_requires_the_single_signed_header(tmp_path):
    payload = stub_package(stub_held(b""))
    held = stub_held(digest_of(payload))
    worker = stub_worker(tmp_path, held=held, package=payload)
    body = {"attempt_id": ATTEMPT, "work_item_id": WORK_ITEM,
             "input_hash": digest_of(payload), "control_epoch": 2}
    with TestClient(create_worker_app(worker), base_url="http://127.0.0.1:8400") as client:
        for headers in ({}, {"x-collab-wake": "2"}, {"x-collab-wake": "1,1"},
                        [("x-collab-wake", "1"), ("x-collab-wake", "1")]):
            response = client.post("/wake", json=body, headers=headers)
            assert response.status_code == 403, headers
            assert response.headers["cache-control"] == "no-store"
        assert worker.client.calls == []
        assert client.request("GET", "/wake", json=body).status_code == 405
        response = client.post("/wake", json=body, headers={"x-collab-wake": "1"})
        assert response.status_code == 200
        assert response.json() == {"outcome": ACCEPTED}
        assert worker.wait_idle(timeout=10) is True
        worker.journal.close()


def test_the_wake_endpoint_rejects_malformed_or_oversized_bodies(tmp_path):
    payload = stub_package(stub_held(b""))
    held = stub_held(digest_of(payload))
    worker = stub_worker(tmp_path, held=held, package=payload)
    valid = {"attempt_id": ATTEMPT, "work_item_id": WORK_ITEM,
             "input_hash": digest_of(payload), "control_epoch": 2}
    headers = {"x-collab-wake": "1"}
    with TestClient(create_worker_app(worker), base_url="http://127.0.0.1:8400") as client:
        for bad in (dict(valid, attempt_id="zz"), dict(valid, control_epoch="2"),
                    dict(valid, extra=1), {key: value for key, value in valid.items()
                                          if key != "control_epoch"}):
            assert client.post("/wake", json=bad, headers=headers).status_code == 422, bad
        oversized = json.dumps(valid).encode("utf-8") + b" " * 9000
        assert client.post("/wake", content=oversized,
                           headers=dict(headers, **{"content-type": "application/json"})) \
            .status_code == 413
        assert client.get("/attempts/" + "f" * 32).status_code == 404
        assert client.get("/attempts/not-an-id").status_code == 404
        assert client.get("/nope").status_code == 404
        assert worker.client.calls == []
        worker.journal.close()


def test_the_local_views_never_expose_the_original_or_the_token(env):
    attempt, payload = wake_payload(env)
    assert env.worker_instance.wake(payload) == ACCEPTED
    assert env.worker_instance.wait_idle(timeout=20) is True
    with TestClient(create_worker_app(env.worker_instance),
                    base_url="http://127.0.0.1:8400") as client:
        health = client.get("/health")
        assert health.status_code == 200
        assert health.json()["simulated"] is True
        assert env.instance_token not in health.text
        view = client.get("/attempts/" + attempt.attempt_id)
        assert view.status_code == 200
        assert env.instance_token not in view.text
        assert ORIGINAL not in view.text
        assert view.json()["state"] == REPORTED
        assert view.json()["input_hash"] == attempt.input_hash


def test_a_second_worker_over_the_same_local_files_reconciles(env):
    attempt, payload = wake_payload(env)
    assert env.worker_instance.wake(payload) == ACCEPTED
    assert env.worker_instance.wait_idle(timeout=20) is True
    second = CollaborationWorker(CollaborationClient(BASE_URL, instance_token=env.instance_token,
                                                    transport=SyncAsgi(env.app)),
                                 ExecutionJournal(env.settings.journal_path),
                                 settings=env.settings, runner=FakeRunner(),
                                 executor="second-holder")
    try:
        assert second.poll() == [FINISHED]
        assert second.wake(payload) == FINISHED
        assert stored(env, attempt.attempt_id).status == "submitted"
    finally:
        second.client.close()
        second.journal.close()


# --- 与旧 worker 的隔离 ----------------------------------------------------------------------

def test_the_collaboration_entry_imports_no_legacy_worker_modules():
    probe = ("import sys; "
             f"sys.path.insert(0, {str(WORKER_DIR)!r}); "
             "import collaboration_app; "
             "print(sorted({name.split('.')[0] for name in sys.modules if name.split('.')[0] in "
             "{'worker_api', 'agent_runner', 'worker_config', 'agent_core', 'collaboration'}}))")
    result = subprocess.run([sys.executable, "-c", probe], cwd=str(WORKER_DIR),
                            capture_output=True, text=True, encoding="utf-8", timeout=180)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "[]"
