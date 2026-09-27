"""测试 execution_host：owned 子进程派生、整树终止（含 Windows 后代清理）、
有界准入与启动对账。用真实子进程验证「取消能停掉派生的 SDK 子进程树」。
"""
import subprocess
import sys
import time

import pytest

from agent_core.execution_host import (ExecutionHost, spawn_owned,
                                       terminate_process_tree, pid_alive)
from agent_core.run_store import RunStore

# 子进程：派生一个睡 60s 的孙进程，把孙进程 pid 打印到 stdout（带换行，供 readline
# 立即返回），然后自己也睡。
_PARENT_SPAWNS_GRANDCHILD = (
    "import subprocess, sys, time\n"
    "g = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
    "sys.stdout.write(str(g.pid) + '\\n'); sys.stdout.flush()\n"
    "time.sleep(60)\n"
)
_SLEEP = "import time; time.sleep(60)"


def _wait_dead(pid, timeout=5.0):
    """轮询等待 pid 退出，返回是否在 timeout 内变dead。"""
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if not pid_alive(pid):
            return True
        time.sleep(0.1)
    return not pid_alive(pid)


@pytest.fixture
def store(tmp_path):
    s = RunStore(tmp_path / "runtime.sqlite3", max_active=1)
    yield s
    s.close()


# ---- 进程树终止 ----

def test_terminate_none_and_already_exited():
    assert terminate_process_tree(None) is True
    p = subprocess.Popen([sys.executable, "-c", "pass"])
    p.wait()
    assert terminate_process_tree(p) is True  # 已退出，直接 True


def test_spawn_owned_child_is_killable():
    proc = spawn_owned([sys.executable, "-c", _SLEEP])
    try:
        assert proc.poll() is None          # 在跑
        assert pid_alive(proc.pid) is True
    finally:
        assert terminate_process_tree(proc, grace_s=1.0) is True
    assert proc.poll() is not None          # 已终止
    assert _wait_dead(proc.pid)


def test_terminate_tree_includes_descendant():
    """核心门：终止 owned 进程树时，其派生的孙进程也一并被清理
    （Windows: taskkill /T；POSIX: 进程组 SIGKILL）。"""
    proc = spawn_owned([sys.executable, "-c", _PARENT_SPAWNS_GRANDCHILD],
                       stdout=subprocess.PIPE, text=True)
    grandchild_pid = None
    try:
        line = proc.stdout.readline().strip()
        assert line.isdigit(), f"未读到孙进程 pid: {line!r}"
        grandchild_pid = int(line)
        assert pid_alive(grandchild_pid) is True
        # 升级终止整棵树（硬取消，直接强杀）
        assert terminate_process_tree(proc, grace_s=1.0, interrupt_first=False) is True
        assert _wait_dead(proc.pid)
        # 孙进程（后代）也必须被清理——仅取消父进程线程/进程是不够的
        assert _wait_dead(grandchild_pid), "孙进程未被整树清理"
    finally:
        if proc.poll() is None:
            terminate_process_tree(proc, grace_s=0.5, interrupt_first=False)
        # 兜底：确保孙进程不残留（测试异常路径下也不泄漏 60s 睡眠进程）
        if grandchild_pid and pid_alive(grandchild_pid):
            try:
                subprocess.run(
                    (["taskkill", "/T", "/F", "/PID", str(grandchild_pid)]
                     if sys.platform == "win32" else
                     ["kill", "-9", str(grandchild_pid)]),
                    capture_output=True, check=False)
            except OSError:
                pass


def test_pid_alive_invalid():
    assert pid_alive(None) is False
    assert pid_alive(0) is False
    assert pid_alive(-1) is False


def test_graceful_interrupt_first():
    """interrupt_first=True 先 terminate()，子进程响应即在 grace 内退出。"""
    proc = spawn_owned([sys.executable, "-c", _SLEEP])
    try:
        assert terminate_process_tree(proc, grace_s=2.0, interrupt_first=True) is True
    finally:
        if proc.poll() is None:
            terminate_process_tree(proc, grace_s=0.5, interrupt_first=False)
    assert proc.poll() is not None


# ---- ExecutionHost：准入 / attach / cancel / release / reconcile ----

def test_host_admission_saturated(store):
    store.create_task("t1", "r", "p", workspace_key="w1")
    store.create_task("t2", "r", "p", workspace_key="w2")
    host = ExecutionHost(store)
    assert host.admit("t1").admitted is True
    assert host.active_count() == 1
    res = host.admit("t2")
    assert res.admitted is False and res.reason == "saturated"


def test_host_attach_cancel_clears_pid(store):
    store.create_task("t1", "r", "p", workspace_key="w1")
    host = ExecutionHost(store, grace_s=1.0)
    claim = host.admit("t1")
    proc = spawn_owned([sys.executable, "-c", _SLEEP])
    try:
        run = host.attach(claim.attempt_id, "t1", proc)
        assert store.get_attempt(claim.attempt_id)["host_pid"] == proc.pid
        assert host.cancel(run) is True
        assert proc.poll() is not None
        # 取消后 host_pid 清空（进程已收）
        assert store.get_attempt(claim.attempt_id)["host_pid"] is None
        assert run.cancelled is True
    finally:
        if proc.poll() is None:
            terminate_process_tree(proc, grace_s=0.5, interrupt_first=False)


def test_host_release_finishes_attempt(store):
    store.create_task("t1", "r", "p", workspace_key="w1")
    host = ExecutionHost(store)
    claim = host.admit("t1")
    p = subprocess.Popen([sys.executable, "-c", "pass"])
    p.wait()
    run = host.attach(claim.attempt_id, "t1", p)
    assert host.release(run, "done", result={"output": "ok"}) is True
    att = store.get_attempt(claim.attempt_id)
    assert att["status"] == "done" and att["result"] == {"output": "ok"}
    assert host.active_count() == 0


def test_host_reconcile_marks_interrupted(store):
    """启动对账：上个进程遗留的在飞尝试（含登记的 host_pid）→ interrupted，
    且不按陈旧 pid 杀进程（只重整状态）。"""
    store.create_task("t1", "r", "p", workspace_key="w1")
    host = ExecutionHost(store)
    claim = host.admit("t1")
    store.set_attempt_host(claim.attempt_id, 999999)  # 陈旧 pid（对账不得去杀它）
    # 模拟重启：新 host 实例对账
    host2 = ExecutionHost(store)
    affected = host2.reconcile()
    assert affected == ["t1"]
    assert store.get_attempt(claim.attempt_id)["status"] == "interrupted"
    assert store.get_attempt(claim.attempt_id)["host_pid"] is None
    assert store.get_task("t1")["status"] == "interrupted"
    assert pid_alive(999999) is False  # 未误判/未触碰该 pid
