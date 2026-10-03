"""真进程、真 HTTP 的协作闭环验收：服务与 Worker 各占一个进程和一个监听端口。

派发后由负责人会话经真实 socket 触发 `scheduler/tick`，outbox 投递与 wake 提示都出自
服务进程自己的主线代码；Worker 进程再经真实 HTTP 回拉任务包与完整原文并逐项回传。

这里不启动任何真实模型，也不声称存在操作系统级隔离：执行实例按事实声明
`isolation_mode=none`，而服务的派发路由要求 `requires_real_data=True` 且 `verify` 明确
拒绝无隔离的实例，所以本轮不伪造隔离声明，那条路由留给第 13 节 P6/T09。人侧的建项目、
审核需求、发布计划与撤回同样直接调用服务层：这些入口的 HTTP 契约已在
`runtime/tests/test_collaboration_app.py` 覆盖，本文件只验证跨进程事实。
"""
from __future__ import annotations

from contextlib import closing, contextmanager
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import threading
import time
from types import SimpleNamespace

import httpx
import pytest

pytest.importorskip("uvicorn")

WORKER_DIR = Path(__file__).resolve().parents[1]
RUNTIME_DIR = WORKER_DIR.parent / "workspace" / "agent" / "orchestrator" / "runtime"
sys.path.insert(0, str(WORKER_DIR))
sys.path.insert(0, str(RUNTIME_DIR))

from collaboration.app import COOKIE, PREFIX
from collaboration.auth import Access, AuthService
from collaboration.database import Database
from collaboration.dispatch import (DispatchService, TOPIC_ATTEMPT_ASSIGNED,
                                    TOPIC_ATTEMPT_REVOKED)
from collaboration.instances import InstanceService
from collaboration.requirements import RequirementService
from collaboration.reviews import ReviewService
from collaboration.store import CollaborationStore

from collaboration_client import digest_of
from collaboration_journal import PAUSED_GATE, REPORTED, ExecutionJournal

ITEM_KEYS = ("A", "B", "C", "D", "E", "F", "G")
PASSWORD = "loop-only-password-2026"
ORIGINAL = "\n".join((
    "# 需求原文",
    "",
    "第一条验收：执行者必须拿到完整原文，而不是摘要。",
    "第二条验收：任何截断都要能被摘要查出来。",
    "第三条验收：报告要能回到提出需求的人。",
    "",
    "正文最后一行。",
)) + "\n"


class Process:
    """A child process we own: its output is drained so a full pipe can never deadlock it."""

    def __init__(self, argv, *, cwd, env=None):
        merged = dict(os.environ, PYTHONIOENCODING="utf-8")
        merged.pop("COLLAB_INSTANCE_TOKEN", None)
        merged.update(env or {})
        self.log: list[str] = []
        self.popen = subprocess.Popen([sys.executable, *argv], cwd=str(cwd), env=merged,
                                      stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                      text=True, encoding="utf-8", errors="replace")
        self._reader = threading.Thread(target=self._drain, daemon=True)
        self._reader.start()

    def _drain(self) -> None:
        for line in self.popen.stdout or ():
            self.log.append(line.rstrip())

    @property
    def output(self) -> str:
        return "\n".join(self.log)

    @property
    def alive(self) -> bool:
        return self.popen.poll() is None

    def stopped(self, timeout: float = 20.0) -> bool:
        if not self.alive:
            return True
        self.popen.terminate()
        try:
            self.popen.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            self.popen.kill()
            self.popen.wait(timeout=timeout)
        return self.popen.poll() is not None

    def wait_http(self, url: str, *, timeout=45.0, note: str) -> None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if not self.alive:
                pytest.fail(f"{note} exited with code {self.popen.poll()}:\n{self.output}")
            try:
                if local_get(url, timeout=0.5).status_code == 200:
                    return
            except httpx.HTTPError:
                time.sleep(0.1)
        pytest.fail(f"{note} did not answer on {url} within {timeout}s:\n{self.output}")


def local_get(url: str, **kwargs):
    """One request to a process this test started itself: direct, never via an ambient proxy.

    httpx reads the machine's proxy settings by default, so a developer with a local proxy tool
    running would have every loopback call here sent to that proxy instead, and the service would
    answer 404 to a percent-encoded copy of our own URL. The production worker client already
    refuses the environment for exactly this reason.
    """
    kwargs.setdefault("trust_env", False)
    return httpx.get(url, **kwargs)


def local_client(**kwargs) -> httpx.Client:
    kwargs.setdefault("trust_env", False)
    return httpx.Client(**kwargs)


def _free_port() -> int:
    with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


@contextmanager
def human(rig, user):
    with rig.database.transaction() as conn:
        store = CollaborationStore(rig.database, connection=conn)
        yield Access(store.get_user(user.user_id), False, "", 0.0, store, conn)


def approved_requirement(rig) -> str:
    with human(rig, rig.requester) as access:
        requirement = rig.requirements.create_requirement(
            access, project_id=rig.project.project_id, title="协作闭环需求", content=ORIGINAL,
            acceptance_items=["第一条验收", "第二条验收", "第三条验收"])
        requirement, _ = rig.requirements.request_review(
            access, project_id=rig.project.project_id,
            requirement_id=requirement.requirement_id,
            revision_id=requirement.current_revision_id, reviewer_user_id=rig.reviewer.user_id,
            expected_version=requirement.version)
    with human(rig, rig.reviewer) as access:
        requirement, _ = rig.requirements.decide_review(
            access, project_id=rig.project.project_id, requirement_id=requirement.requirement_id,
            revision_id=requirement.review_revision_id, decision="approved",
            expected_version=requirement.version)
    return requirement


def published_plan(rig, requirement_id: str, revision_id: str, revision_hash: str) -> dict:
    fields = [{"key": key, "title": f"{key} 任务", "objective": f"完成 {key} 的目标",
               "scope": "范围内", "non_goals": "不做的事", "duty": "实现",
               "assignee_user_id": rig.executor.user_id, "binding_id": rig.binding.binding_id,
               "acceptance_ids": [f"AC-{index:03d}" for index in range(1, 4)],
               "estimated_units": 2, "depends_on": []}
              for index, key in enumerate(ITEM_KEYS, start=1)]
    plan = rig.store.create_plan(rig.project.project_id, requirement_id,
                                 revision_id=revision_id, revision_hash=revision_hash,
                                 status="published",
                                 report=json.dumps({"problems": [], "item_count": len(fields)}),
                                 total_units=2 * len(fields), created_by=rig.reviewer.user_id,
                                 created_at=time.time(), items=fields)
    return {entry.key: entry for entry in rig.store.list_work_items(plan.project_id, plan.plan_id)}


@pytest.fixture(scope="module")
def rig(tmp_path_factory):
    """One real collaboration service process, plus every human decision the loop needs."""
    root = tmp_path_factory.mktemp("collab-loop")
    service_dir = root / "service"
    service_dir.mkdir()
    database = Database(service_dir / "metadata.sqlite3")
    database.initialize()
    store = CollaborationStore(database)
    # 负责人要有真实凭据：他的会话经真实 socket 登录，服务进程才会自己跑调度节拍。
    owner = AuthService(database).bootstrap("owner", "负责人", PASSWORD)
    requester, reviewer, executor = (store.create_user(name) for name in
                                     ("需求方", "审核人", "执行者"))
    project = store.create_project("闭环项目", owner.user_id)
    for member, role in ((requester, "member"), (reviewer, "manager"), (executor, "member")):
        store.add_member(project.project_id, member.user_id, access_role=role)
    agent = store.create_agent(executor.user_id, "执行者的 Agent")
    binding = store.create_binding(project.project_id, agent.agent_id, executor.user_id,
                                   duty="实现", persona_version="persona-v1",
                                   allowed_tools=("Read", "Edit"), workspace_ref="workspace-a",
                                   provider_ref="provider-a")
    context = SimpleNamespace(root=root, service_dir=service_dir, database=database, store=store,
                              owner=owner, requester=requester, reviewer=reviewer,
                              executor=executor, agent=agent, binding=binding, project=project,
                              requirements=RequirementService(), reviews=ReviewService(),
                              instances=InstanceService(database), processes=[])
    approved = approved_requirement(context)
    context.requirement_id = approved.requirement_id
    context.requirement_revision_id = approved.approved_revision_id
    context.requirement_revision_hash = approved.approved_revision_hash
    context.items = published_plan(context, context.requirement_id,
                                   context.requirement_revision_id,
                                   context.requirement_revision_hash)

    port = _free_port()
    origin = f"http://127.0.0.1:{port}"
    service = Process(["-m", "collaboration", "serve", "--data-dir", str(service_dir),
                       "--host", "127.0.0.1", "--port", str(port), "--origin", origin],
                      cwd=RUNTIME_DIR)
    context.processes.append(service)
    try:
        _expect_answer(origin + PREFIX + "/projects", 401, service, "the collaboration service")
        context.port = port
        context.origin = origin
        context.base = origin + PREFIX
        context.http = local_client(base_url=origin + PREFIX, timeout=20.0,
                                    headers={"Origin": origin})
        # 负责人的会话经真实 socket 登录取得：调度节拍必须由服务进程自己执行，推送才来自主线代码。
        login = context.http.post("/auth/login", json={"username": "owner", "password": PASSWORD})
        assert login.status_code == 200, login.text
        context.manager = {"Cookie": f"{COOKIE}={login.cookies.get(COOKIE)}",
                           "X-CSRF-Token": login.json()["csrf_token"]}
        context.dispatch = DispatchService(database, instances=context.instances,
                                           requires_real_data=False)
        yield context
    finally:
        # 起进程之后的任何一步失败都要把已起的进程收掉，否则一次失败的夹具会留下孤儿。
        for process in context.processes:
            process.stopped()
        if getattr(context, "http", None) is not None:
            context.http.close()


def tick(rig) -> httpx.Response:
    """Ask the running service process to drain its own outbox; that is where the push lives."""
    return rig.http.post(f"/projects/{rig.project.project_id}/scheduler/tick",
                         headers=rig.manager)


def _expect_answer(url: str, status: int, process: Process, note: str) -> None:
    """Readiness for a route we may not be allowed to use: any real answer means it is alive."""
    deadline = time.time() + 45
    while time.time() < deadline:
        if not process.alive:
            pytest.fail(f"{note} exited with code {process.popen.poll()}:\n{process.output}")
        try:
            if local_get(url, timeout=0.5).status_code == status:
                return
        except httpx.HTTPError:
            time.sleep(0.1)
    pytest.fail(f"{note} never answered {status} on {url}:\n{process.output}")


class Bundle:
    """One claimed execution instance and the worker process serving its state directory."""

    def __init__(self, rig, *, name: str, state_dir: Path, instance, instance_token: str,
                 port: int):
        self.rig = rig
        self.name = name
        self.state_dir = state_dir
        self.instance = instance
        self.instance_token = instance_token
        self.port = port
        self.url = f"http://127.0.0.1:{port}/wake"
        self.client = local_client(timeout=20.0)
        # 浏览器之外的写请求也必须带 Origin，这正是生产客户端每次都带的那一个。
        self.raw = local_client(base_url=rig.base, timeout=20.0, headers={"Origin": rig.origin})
        self.process = Process(["collaboration_app.py", "serve", "--base-url", rig.base,
                                "--state-dir", str(state_dir), "--host", "127.0.0.1",
                                "--port", str(port), "--runner", "fake", "--capacity", "1",
                                "--lease-interval", "600", "--poll-interval", "600"],
                               cwd=WORKER_DIR)
        rig.processes.append(self.process)
        self.process.wait_http(f"http://127.0.0.1:{port}/health", note="the worker")

    def _event(self, attempt, topic: str = TOPIC_ATTEMPT_ASSIGNED):
        return [entry for entry in self.rig.store.list_events(self.rig.project.project_id)
                if entry.topic == topic
                and json.loads(entry.payload)["attempt_id"] == attempt.attempt_id][-1]

    def hint(self, attempt, topic: str = TOPIC_ATTEMPT_ASSIGNED) -> dict:
        """The four fields the service actually posts; the queue row carries more than that."""
        payload = json.loads(self._event(attempt, topic).payload)
        return {key: payload[key] for key in ("attempt_id", "work_item_id", "input_hash",
                                              "control_epoch")}

    def notify(self, attempt) -> None:
        """The queued hint leaves from the service process, not from this test process."""
        response = tick(self.rig)
        assert response.status_code == 200, response.text
        assert response.json()["delivered"] >= 1, response.text
        assert self._event(attempt).delivered_at is not None

    def push(self, payload: dict) -> httpx.Response:
        return self.client.post(self.url, json=payload, headers={"x-collab-wake": "1"})

    def health(self) -> dict:
        return self.client.get(f"http://127.0.0.1:{self.port}/health").json()

    def local(self, attempt_id: str) -> httpx.Response:
        return self.client.get(f"http://127.0.0.1:{self.port}/attempts/{attempt_id}")

    def stop(self) -> bool:
        return self.process.stopped()


@pytest.fixture
def machine(rig):
    """Nodes, slots and worker processes started through their own command lines."""
    built: list[Bundle] = []

    def build(*, name: str) -> Bundle:
        port = _free_port()
        node, node_token = rig.instances.enroll_node(f"node-{name}")
        with human(rig, rig.owner) as access:
            instance, claim_token = rig.instances.register_slot(
                access, project_id=rig.project.project_id, binding_id=rig.binding.binding_id,
                node_id=node.node_id, worker_url=f"http://127.0.0.1:{port}/wake")
        state_dir = rig.root / f"state-{name}"
        claimed = subprocess.run(
            [sys.executable, "collaboration_app.py", "claim", "--base-url", rig.base,
             "--state-dir", str(state_dir), "--workspace-ref", rig.binding.workspace_ref,
             "--provider-ref", rig.binding.provider_ref, "--allowed-tools", "Read,Edit",
             "--isolation-mode", "none", "--capacity", "1"],
            cwd=str(WORKER_DIR), capture_output=True, text=True, encoding="utf-8", timeout=180,
            env=dict(os.environ, PYTHONIOENCODING="utf-8", COLLAB_NODE_TOKEN=node_token,
                     COLLAB_CLAIM_TOKEN=claim_token))
        assert claimed.returncode == 0, claimed.stdout + claimed.stderr
        token = (state_dir / "instance-token").read_text(encoding="utf-8").strip()
        bundle = Bundle(rig, name=name, state_dir=state_dir, instance=instance,
                        instance_token=token, port=port)
        built.append(bundle)
        return bundle

    yield build
    for bundle in built:
        bundle.stop()
        bundle.client.close()
        bundle.raw.close()


def stored(rig, attempt_id: str):
    return rig.store.get_attempt(rig.project.project_id, attempt_id)


def accepted_stages(rig, attempt_id: str) -> list[str]:
    return [report.stage for report in
            rig.store.list_attempt_reports(rig.project.project_id, attempt_id) if report.accepted]


def report_rows(rig, attempt_id: str, stage: str):
    return [report for report in rig.store.list_attempt_reports(rig.project.project_id, attempt_id)
            if report.stage == stage]


def wait_status(rig, attempt_id: str, wanted: str, *, timeout=60.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if stored(rig, attempt_id).status == wanted:
            return stored(rig, attempt_id)
        time.sleep(0.2)
    pytest.fail(f"attempt {attempt_id} is still {stored(rig, attempt_id).status}, "
                f"never {wanted}")


def wait_local(bundle: Bundle, attempt_id: str, wanted: str, *, timeout=45.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        response = bundle.local(attempt_id)
        if response.status_code == 200 and response.json().get("state") == wanted:
            return response.json()
        time.sleep(0.2)
    latest = bundle.local(attempt_id)
    pytest.fail(f"the worker never reported {wanted}: {latest.status_code} {latest.text} | "
                f"{bundle.process.output[-2000:]}")


# --- 闭环 -------------------------------------------------------------------------------

def test_the_service_and_the_worker_complete_one_attempt_over_real_http(rig, machine):
    bundle = machine(name="loop")
    attempt = rig.dispatch.dispatch(rig.project.project_id, rig.items["A"].work_item_id)
    bundle.notify(attempt)

    delivered = wait_status(rig, attempt.attempt_id, "submitted")
    assert (delivered.usage_units, delivered.trace_id) == (1, f"collab:{attempt.attempt_id}")
    assert accepted_stages(rig, attempt.attempt_id) == ["ack", "start", "artifact", "result"]
    assert bundle.health()["simulated"] is True

    # 完整原文越过了进程边界：产物同时带回摘要与字符数，二者都对得上被批准的版本。
    artifacts = rig.store.list_artifacts(rig.project.project_id, attempt.attempt_id)
    assert [entry.name for entry in artifacts] == ["simulated-output.md"]
    body = rig.store.get_artifact_content(rig.project.project_id,
                                         artifacts[0].artifact_id).decode("utf-8")
    assert f"requirement_sha256: {digest_of(ORIGINAL.encode('utf-8'))}" in body
    assert f"requirement_characters: {len(ORIGINAL)}" in body
    assert "simulated: true" in body

    # 产物归服务所有：Worker 进程关闭后仍能经真实 socket 下载。
    assert bundle.stop() is True
    reader = local_client(base_url=rig.base, timeout=20.0,
                          headers={"X-Instance-Token": bundle.instance_token})
    listed = reader.get(f"/machine/attempts/{attempt.attempt_id}/artifacts")
    assert listed.status_code == 200
    assert [entry["artifact_id"] for entry in listed.json()] == [artifacts[0].artifact_id]
    fetched = reader.get(f"/machine/attempts/{attempt.attempt_id}"
                         f"/artifacts/{artifacts[0].artifact_id}")
    assert fetched.status_code == 200
    assert fetched.headers["x-content-hash"] == artifacts[0].sha256
    assert fetched.content.decode("utf-8") == body
    reader.close()

    on_disk = bundle.state_dir / "work" / attempt.attempt_id
    assert (on_disk / f"package-{attempt.input_hash[:16]}.json").read_bytes() == \
        attempt.package_json.encode("utf-8")
    entry = ExecutionJournal(bundle.state_dir / "journal.sqlite3").get(attempt.attempt_id)
    assert entry.state == REPORTED
    assert (on_disk / f"requirement-{entry.requirement_hash[:16]}.md") \
        .read_text(encoding="utf-8") == ORIGINAL


def test_a_restarted_worker_reconciles_from_its_own_journal(rig, machine):
    bundle = machine(name="restart")
    attempt = rig.dispatch.dispatch(rig.project.project_id, rig.items["B"].work_item_id)
    bundle.notify(attempt)
    wait_status(rig, attempt.attempt_id, "submitted")
    before = accepted_stages(rig, attempt.attempt_id)
    assert bundle.stop() is True

    replacement = Bundle(rig, name="restart", state_dir=bundle.state_dir,
                         instance=bundle.instance, instance_token=bundle.instance_token,
                         port=bundle.port)
    try:
        response = replacement.push(replacement.hint(attempt))
        assert response.status_code == 200
        assert response.json() == {"outcome": "finished"}
        assert replacement.local(attempt.attempt_id).json()["state"] == REPORTED
    finally:
        replacement.client.close()
        replacement.stop()
    assert accepted_stages(rig, attempt.attempt_id) == before
    assert stored(rig, attempt.attempt_id).usage_units == 1


def test_repeated_real_wakes_advance_the_attempt_only_once(rig, machine):
    bundle = machine(name="repeat")
    attempt = rig.dispatch.dispatch(rig.project.project_id, rig.items["C"].work_item_id)
    bundle.notify(attempt)
    payload = bundle.hint(attempt)
    sequential = [bundle.push(payload) for _ in range(3)]
    answers: list[str] = []

    def push() -> None:
        answers.append(bundle.push(payload).json()["outcome"])

    threads = [threading.Thread(target=push) for _ in range(3)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    wait_status(rig, attempt.attempt_id, "submitted")

    assert all(response.status_code == 200 for response in sequential)
    assert len(answers) == 3
    assert [row.accepted for row in report_rows(rig, attempt.attempt_id, "result")].count(True) == 1
    assert len(rig.store.list_artifacts(rig.project.project_id, attempt.attempt_id)) == 1
    assert stored(rig, attempt.attempt_id).usage_units == 1


# --- 授权与隔离（跨进程仍然成立） --------------------------------------------------------

def test_the_worker_pauses_on_a_pending_rule_and_delivers_after_approval(rig, machine):
    bundle = machine(name="gate")
    attempt = rig.dispatch.dispatch(rig.project.project_id, rig.items["D"].work_item_id)
    hint = bundle.hint(attempt)
    with human(rig, rig.reviewer) as access:
        review = rig.reviews.record_rule(access, project_id=rig.project.project_id,
                                         attempt_id=attempt.attempt_id,
                                         rule_key="dangerous_command")
    bundle.push(hint)
    view = wait_local(bundle, attempt.attempt_id, PAUSED_GATE)
    assert stored(rig, attempt.attempt_id).status == "assigned"
    assert accepted_stages(rig, attempt.attempt_id) == []
    assert ORIGINAL not in json.dumps(view, ensure_ascii=False)

    with human(rig, rig.owner) as access:
        rig.reviews.decide_security(access, project_id=rig.project.project_id,
                                    security_review_id=review.security_review_id,
                                    decision="approved", reason="本次允许该危险命令",
                                    expected_version=review.version)
    bundle.push(hint)
    wait_status(rig, attempt.attempt_id, "submitted")
    assert accepted_stages(rig, attempt.attempt_id) == ["ack", "start", "artifact", "result"]
    assert bundle.instance_token not in bundle.local(attempt.attempt_id).text
    assert ORIGINAL not in bundle.local(attempt.attempt_id).text


def test_a_revoked_attempt_rejects_a_late_result_over_the_socket(rig, machine):
    bundle = machine(name="revoked")
    item = rig.items["E"]
    attempt = rig.dispatch.dispatch(rig.project.project_id, item.work_item_id)
    # 派发已把工作项版本推进了一格，撤回必须按最新的那一格提交。
    current_item = rig.store.get_work_item(rig.project.project_id, item.work_item_id)
    with human(rig, rig.owner) as access:
        rig.reviews.cancel(access, project_id=rig.project.project_id,
                           work_item_id=current_item.work_item_id,
                           expected_version=current_item.version, reason="先停止这一项")
    current = stored(rig, attempt.attempt_id)
    assert current.status == "reconciliation_required"

    late = bundle.raw.post(
        f"/machine/attempts/{attempt.attempt_id}/result",
        json={"input_hash": current.input_hash, "control_epoch": current.control_epoch,
              "lease_epoch": current.lease_epoch, "status": "submitted",
              "summary": "迟到的结果", "artifacts": [], "usage_units": 1},
        headers={"X-Instance-Token": bundle.instance_token})
    assert late.status_code == 409
    assert report_rows(rig, attempt.attempt_id, "result")
    assert all(not row.accepted for row in report_rows(rig, attempt.attempt_id, "result"))
    assert stored(rig, attempt.attempt_id).status == "reconciliation_required"

    # 被撤销后连唤醒也只能导向对账，不会让这一项重新变成“已交付”。
    bundle.push(bundle.hint(current, TOPIC_ATTEMPT_REVOKED))
    wait_local(bundle, attempt.attempt_id, "reconciling")
    assert stored(rig, attempt.attempt_id).status == "reconciliation_required"


def test_a_returned_attempt_is_requeued_and_the_next_attempt_is_told_what_missed(rig, machine):
    bundle = machine(name="rework")
    item = rig.items["G"]
    first = rig.dispatch.dispatch(rig.project.project_id, item.work_item_id)
    bundle.notify(first)
    delivered = wait_status(rig, first.attempt_id, "submitted")

    # 负责人退回并点名验收项，负责人之外的 manager 才有权重新打开执行权。
    with human(rig, rig.executor) as access:
        rig.reviews.confirm(access, project_id=rig.project.project_id,
                            attempt_id=first.attempt_id, decision="changes_requested",
                            reason="第二条验收未满足", expected_version=delivered.version)
    with human(rig, rig.owner) as access:
        rig.reviews.rework(access, project_id=rig.project.project_id,
                           work_item_id=item.work_item_id,
                           expected_version=rig.store.get_work_item(
                               rig.project.project_id, item.work_item_id).version)

    second = rig.dispatch.dispatch(rig.project.project_id, item.work_item_id)
    assert (second.number, second.attempt_id != first.attempt_id) == (2, True)
    bundle.notify(second)
    wait_status(rig, second.attempt_id, "submitted")

    body = rig.store.get_artifact_content(
        rig.project.project_id,
        rig.store.list_artifacts(rig.project.project_id, second.attempt_id)[0].artifact_id
    ).decode("utf-8")
    assert "rework_notes: 1" in body and "第二条验收未满足" in body
    assert second.input_hash != first.input_hash
    frozen = (bundle.state_dir / "work" / second.attempt_id
              / f"package-{second.input_hash[:16]}.json").read_text(encoding="utf-8")
    assert "第二条验收未满足" in frozen and second.package_json == frozen

    # 上一轮的结果仍是独立证据，不被新一轮覆盖。
    kept = stored(rig, first.attempt_id)
    assert (kept.status, kept.result_hash, kept.usage_units) == (
        "changes_requested", delivered.result_hash, 1)


def test_another_projects_instance_cannot_reach_these_inputs(rig, machine):
    bundle = machine(name="own-project")
    attempt = rig.dispatch.dispatch(rig.project.project_id, rig.items["F"].work_item_id)
    other = rig.store.create_project("另一个项目", rig.owner.user_id)
    rig.store.add_member(other.project_id, rig.executor.user_id)
    binding = rig.store.create_binding(other.project_id, rig.agent.agent_id, rig.executor.user_id,
                                       duty="实现", persona_version="persona-v1",
                                       allowed_tools=("Read",), workspace_ref="workspace-b",
                                       provider_ref="provider-b")
    node, node_token = rig.instances.enroll_node("node-foreign")
    with human(rig, rig.owner) as access:
        _slot, claim_token = rig.instances.register_slot(
            access, project_id=other.project_id, binding_id=binding.binding_id,
            node_id=node.node_id, worker_url="http://127.0.0.1:8400/wake")
    claimed = bundle.raw.post("/machine/instances/claim",
                              json={"claim_token": claim_token, "protocol_version": "1",
                                    "capacity": 1, "isolation_mode": "none",
                                    "isolation_attestation": "",
                                    "policy": {"workspace_ref": "workspace-b",
                                               "provider_ref": "provider-b",
                                               "allowed_tools": ["Read"], "env_allowlist": []}},
                              headers={"X-Node-Token": node_token})
    assert claimed.status_code == 201, claimed.text
    foreign = local_client(base_url=rig.base, timeout=20.0,
                           headers={"X-Instance-Token": claimed.json()["instance_token"]})
    for path in (f"/machine/attempts/{attempt.attempt_id}/package",
                 f"/machine/attempts/{attempt.attempt_id}/requirement",
                 f"/machine/attempts/{attempt.attempt_id}/artifacts",
                 f"/machine/attempts/{attempt.attempt_id}/security-reviews"):
        assert foreign.get(path).status_code == 404, path
    assert foreign.get("/machine/instances/me/attempts").json() == []

    anonymous = local_get(rig.base + f"/machine/attempts/{attempt.attempt_id}/package")
    assert anonymous.status_code == 401
    damaged = local_get(rig.base + f"/machine/attempts/{attempt.attempt_id}/package",
                        headers={"X-Instance-Token": bundle.instance_token[:-1]})
    assert damaged.status_code == 401
    foreign.close()
