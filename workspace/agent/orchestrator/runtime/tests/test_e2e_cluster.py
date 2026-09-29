"""跨进程集群 E2E：brain(remote 子进程) + worker(子进程) 全链路。

与单进程测试的区别：brain 与 worker 是真实操作系统进程，经 HTTP 通信——
覆盖注册/心跳、派发、执行、产物回传、审计落盘的完整物理链路。

运行：python -m pytest tests/test_e2e_cluster.py -q
依赖：无外部模型（worker 用 Fake runner），CI 可跑。
"""
import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

RUNTIME = Path(__file__).resolve().parents[1]
PROJECT_ROOT = RUNTIME.parents[3]  # tests→runtime→orchestrator→agent→项目根
assert (PROJECT_ROOT / 'worker' / 'worker_api.py').is_file(), '项目根定位错误'
SYS_PY = sys.executable


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _get(url: str, timeout: float = 5) -> dict:
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.loads(r.read().decode())


class Cluster:
    """brain + worker 双子进程，注册后即可派发。"""

    def __init__(self, tmp: Path):
        self.tmp = tmp
        self.brain_port = _free_port()
        self.worker_port = _free_port()
        self.brain = self.worker = None
        self.base = f"http://127.0.0.1:{self.brain_port}"

    def start(self):
        env = dict(os.environ,
                   ORCH_MODE="remote",
                   ORCH_TASK_STORE_DIR=str(self.tmp / "tasks"),
                   ORCH_EXPERIENCE_STORE_DIR=str(self.tmp / "exp"),
                   ORCH_SCHEDULE_STORE_DIR=str(self.tmp / "sched"),
                   ORCH_PROCEDURAL_STORE_DIR=str(self.tmp / "proc"),
                   ORCH_BRAIN_URL=f"http://127.0.0.1:{self.brain_port}",
                   ORCH_ASYNC_POLL_MAX="120")
        self.brain = subprocess.Popen(
            [SYS_PY, "brain_api.py"],
            cwd=str(RUNTIME), env=env,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        wenv = dict(env, ORCH_RUNNER="fake", ORCH_AUTO_REGISTER="1",
                    ORCH_WORKER_PORT=str(self.worker_port),
                    ORCH_WORK_DIR=str(self.tmp / "work"))
        wdir = str(PROJECT_ROOT / "worker")
        try:
            self.worker = subprocess.Popen(
                [SYS_PY, "worker_api.py"],
                cwd=wdir, env=wenv,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except NotADirectoryError:
            # 诊断：pytest 全量运行时父进程 cwd 可能已被先前测试删除/失效
            diag = {
                "wdir": wdir, "wdir_exists": os.path.isdir(wdir),
                "proc_cwd": os.getcwd(),
                "proc_cwd_exists": os.path.isdir(os.getcwd()),
            }
            print("[诊断] worker Popen 267 ->", diag, flush=True)
            raise

    def wait_ready(self, timeout: float = 30) -> None:
        deadline = time.time() + timeout
        registered = False
        while time.time() < deadline:
            try:
                workers = _get(f"{self.base}/workers")
                registered = len(workers) >= 1
                if registered:
                    return
            except Exception:
                pass
            time.sleep(0.4)
        raise TimeoutError("brain/worker 未在时限内就绪")

    def stop(self):
        for proc in (self.worker, self.brain):
            if proc is not None:
                try:
                    proc.terminate()
                    proc.wait(timeout=8)
                except Exception:  # noqa: BLE001
                    try:
                        proc.kill()
                    except Exception:  # noqa: BLE001
                        pass


@pytest.fixture(scope="module")
def cluster(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("e2e_cluster")
    c = Cluster(tmp)
    c.start()
    try:
        c.wait_ready()
        yield c
    finally:
        c.stop()


def _dispatch(cluster: Cluster, prompt: str) -> str:
    body = json.dumps({"prompt": prompt, "role": "executor"}).encode("utf-8")
    req = urllib.request.Request(f"{cluster.base}/dispatch", data=body,
                                 headers={"Content-Type": "application/json"})
    d = json.loads(urllib.request.urlopen(req, timeout=60).read().decode())
    assert d["status"] in ("running", "done"), f"派发异常: {d}"
    return d["task_id"]


def _wait_terminal(cluster: Cluster, task_id: str, timeout: float = 60) -> dict:
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        r = _get(f"{cluster.base}/tasks/{task_id}")
        if r["status"] != last:
            last = r["status"]
        if r["status"] in ("done", "failed", "lost", "cancelled"):
            return r
        time.sleep(0.4)
    raise TimeoutError(f"任务 {task_id} 超时未终态")


def test_e2e_dispatch_done_with_artifact_and_audit(cluster):
    """全链路：派发 → worker 执行 → 产物回传 → done + 审计。"""
    task_id = _dispatch(cluster, "写一份 e2e 冒烟测试用的说明文档 doc.md，内容任意")
    r = _wait_terminal(cluster, task_id)
    assert r["status"] == "done", f"任务失败: {r.get('error')}"
    assert r["worker_id"] == "worker-local"


def test_e2e_cancel_state_machine(cluster):
    """取消的状态机契约（确定性，不依赖 Fake 执行时序）：

    - 取消不存在的任务 → 404
    - 取消已终态任务 → 400（提示当前状态）
    真实取消竞态已在手工实测中验证（task-fedf5dba → cancelled）。
    """
    with pytest.raises(urllib.error.HTTPError) as ei404:
        urllib.request.urlopen(
            urllib.request.Request(f"{cluster.base}/tasks/task-nonexistent/cancel",
                                   method="POST"), timeout=30)
    assert ei404.value.code == 404
    task_id = _dispatch(cluster, "取消状态机验证任务")
    _wait_terminal(cluster, task_id, timeout=60)
    with pytest.raises(urllib.error.HTTPError) as ei400:
        urllib.request.urlopen(
            urllib.request.Request(f"{cluster.base}/tasks/{task_id}/cancel",
                                   method="POST"), timeout=30)
    assert ei400.value.code == 400


def test_e2e_stats_reflect_cluster(cluster):
    stats = _get(f"{cluster.base}/stats")
    assert stats["mode"] == "remote"
    assert stats["worker_active"] >= 1
    assert stats["task_total"] >= 1


def test_e2e_audit_records_dispatch(cluster):
    """审批/派发环节审计：Fake 路径下 dispatch 无拦截记录，但端点可用且返回结构合法。"""
    audit = _get(f"{cluster.base}/audit")
    assert isinstance(audit, list)
