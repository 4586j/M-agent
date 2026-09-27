"""Owned execution host：进程所有权、有界准入与 Windows 安全的进程树清理。

可靠性方案 Slice 3「bounded execution」的执行侧原语：
- 取消本地执行时，先发协作式中断（terminate/SIGTERM），有界等待收尾；仍不退再
  升级终止「且仅终止自己拥有的」进程树（Windows: taskkill /T /F；POSIX: 进程组
  SIGKILL）。仅取消一个 Python 线程并不能停掉它派生的 SDK 子进程——必须按进程树清理。
- 进程所有权 = 清理能力，不等于安全隔离（隔离见 isolated executor）。
- 有界准入构建在 RunStore.try_claim 之上：默认单活动执行，饱和显式拒绝。
- 启动对账只重整「状态」（RunStore.reconcile_interrupted），绝不按陈旧 pid 杀进程
  （重启后 pid 可能已被系统复用，误杀无辜）——崩溃遗留的副作用由 effects 账本单独挂起。

本模块是「附加式」执行原语：可被本地派发器在 rollout 开关下采用，将内核执行放入
owned 子进程；默认不改变既有进程内执行路径。
"""
from __future__ import annotations

import logging
import os
import signal
import subprocess
import time
from dataclasses import dataclass
from typing import Callable, Sequence

log = logging.getLogger(__name__)

IS_WINDOWS = (os.name == "nt")


def spawn_owned(argv: Sequence[str], **kwargs) -> subprocess.Popen:
    """派生一个「可整树清理」的 owned 子进程。

    关键：让子进程成为新进程组/新会话的首进程，这样 POSIX 下可用 killpg 一次性
    连同其派生的孙进程一起终止；Windows 下用 CREATE_NEW_PROCESS_GROUP 便于 taskkill /T。
    调用方仍持有 Popen 句柄（= 所有权凭证），只清理自己派生的树。
    """
    kwargs = dict(kwargs)
    kwargs.setdefault("stdin", subprocess.DEVNULL)
    if IS_WINDOWS:
        flags = kwargs.get("creationflags", 0)
        kwargs["creationflags"] = flags | subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kwargs.setdefault("start_new_session", True)  # setsid → 独立 pgid
    return subprocess.Popen(list(argv), **kwargs)


def pid_alive(pid: int | None) -> bool:
    """进程是否仍存活（跨平台）。pid 为空/非法 → False。用于清理断言与对账。"""
    if not pid or pid <= 0:
        return False
    if IS_WINDOWS:
        # tasklist 过滤该 pid；输出含 "PID" 表头但无匹配行时判定为已退出
        try:
            out = subprocess.run(
                ["tasklist", "/FI", f"PID eq {int(pid)}", "/NH"],
                capture_output=True, text=True, timeout=10, check=False)
        except (OSError, subprocess.SubprocessError):
            return False
        return str(int(pid)) in (out.stdout or "")
    try:
        os.kill(int(pid), 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # 存在但无权限：仍算存活
    except OSError:
        return False
    return True


def _kill_tree_windows(pid: int) -> None:
    """Windows：taskkill /T /F /PID——/T 连同子进程树，/F 强制。"""
    try:
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(int(pid))],
                       capture_output=True, check=False, timeout=15)
    except (OSError, subprocess.SubprocessError) as e:  # noqa: BLE001
        log.warning("taskkill 失败 pid=%s: %s", pid, e)


def _kill_tree_posix(proc: subprocess.Popen) -> None:
    """POSIX：优先按进程组 SIGKILL（需 spawn_owned 建立独立会话），回落到单进程 kill。"""
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        return
    except (ProcessLookupError, PermissionError, OSError, AttributeError):
        pass
    try:
        proc.kill()
    except OSError:
        pass


def terminate_process_tree(proc: subprocess.Popen | None, *,
                           grace_s: float = 2.0,
                           interrupt_first: bool = True) -> bool:
    """优雅中断 → 有界等待 → 升级终止整个（且仅自己拥有的）进程树。

    返回进程最终是否已退出。proc 为 None 或已退出时直接 True。
    interrupt_first=True 先 terminate()（给 SDK 子进程收尾机会），grace_s 内不退
    再整树强杀；False 直接整树强杀（硬取消）。
    """
    if proc is None:
        return True
    if proc.poll() is not None:
        return True
    if interrupt_first:
        try:
            proc.terminate()
        except OSError:
            pass
        try:
            proc.wait(timeout=max(0.0, grace_s))
            return True
        except subprocess.TimeoutExpired:
            pass
    # 升级：整树强杀（Windows taskkill /T；POSIX killpg）
    if IS_WINDOWS:
        _kill_tree_windows(proc.pid)
    else:
        _kill_tree_posix(proc)
    try:
        proc.wait(timeout=max(0.5, grace_s))
    except subprocess.TimeoutExpired:
        pass
    return proc.poll() is not None


@dataclass
class OwnedRun:
    """一次 owned 执行的句柄：尝试 id + 子进程 + 取消标志。"""
    attempt_id: str
    task_id: str
    proc: subprocess.Popen | None = None
    cancelled: bool = False


class ExecutionHost:
    """有界准入 + owned 子进程生命周期管理，构建在 RunStore 之上。

    典型用法（rollout 开关下）：
        host = ExecutionHost(store)
        claim = host.admit(task_id, workspace_key=...)
        if not claim.admitted: ...  # 饱和/串行化冲突，显式拒绝
        proc = spawn_owned(argv)
        run = host.attach(claim.attempt_id, task_id, proc)
        ...  # 等待/取消
        host.cancel(run)              # 升级终止整树
        host.release(run, "done")     # 落终态、清 host_pid
    """

    def __init__(self, run_store, *, grace_s: float = 2.0,
                 now_fn: Callable[[], float] | None = None):
        self.store = run_store
        self.grace_s = float(grace_s)
        self._now_fn = now_fn or time.time

    # ---- 准入 ----

    def admit(self, task_id: str, *, workspace_key: str | None = None,
              session_key: str | None = None, worker: str = "local"):
        """有界准入：委派 RunStore.try_claim（饱和/串行化冲突显式拒绝）。"""
        return self.store.try_claim(task_id, workspace_key=workspace_key,
                                    session_key=session_key, worker=worker)

    def active_count(self) -> int:
        return self.store.count_active()

    # ---- owned 进程 ----

    def attach(self, attempt_id: str, task_id: str,
               proc: subprocess.Popen) -> OwnedRun:
        """登记 owned 子进程 pid（供取消/对账定位；只清理登记过的、自己拥有的树）。"""
        self.store.set_attempt_host(attempt_id, proc.pid)
        return OwnedRun(attempt_id=attempt_id, task_id=task_id, proc=proc)

    def cancel(self, run: OwnedRun, *, hard: bool = False) -> bool:
        """取消一次 owned 执行：升级终止其进程树并清 host_pid。返回是否已退出。"""
        run.cancelled = True
        ok = terminate_process_tree(run.proc, grace_s=self.grace_s,
                                    interrupt_first=not hard)
        try:
            self.store.set_attempt_host(run.attempt_id, None)
        except Exception:  # noqa: BLE001 - 清理登记失败不阻断取消
            pass
        log.info("owned run cancelled attempt=%s task=%s exited=%s",
                 run.attempt_id, run.task_id, ok)
        return ok

    def release(self, run: OwnedRun, status: str, result=None) -> bool:
        """把尝试落终态（done/failed/cancelled），并清 host_pid（RunStore 内已清）。"""
        return self.store.finish_attempt(run.attempt_id, status, result=result)

    # ---- 启动对账 ----

    def reconcile(self) -> list[str]:
        """启动对账：把上个进程遗留的在飞尝试标记为 interrupted（仅重整状态）。

        绝不按陈旧 pid 杀进程——重启后 pid 可能被系统复用，误杀无辜；崩溃遗留的
        外部副作用由 RunStore 的 effects 账本挂起（unknown），交人工/对账裁决。
        """
        return self.store.reconcile_interrupted(now=self._now_fn())
