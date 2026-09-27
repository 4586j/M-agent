"""测试 LocalExecutorDispatcher 的取消语义（P0）。

覆盖：
- cancel_event 被登记并传入内核，cancel(task_id) 能真正置位它（本地取消
  不再只是改状态、执行照常跑完）；
- 内核观察到取消 -> dispatch 如实归类 cancelled，绝不谎报 done；
- 收尾用 finalize_if：cancel 端点已置 cancelled（终态、不可变）时，派发线程
  迟到的收尾写入不得覆盖它。

全程 monkeypatch 内核 run_oneshot，不触网、不依赖真实 SDK。
"""
import sys
import threading
import time
from pathlib import Path

_RUNTIME_DIR = Path(__file__).resolve().parents[1]
_PROJECT_ROOT = _RUNTIME_DIR.parents[3]
for _p in (str(_RUNTIME_DIR), str(_PROJECT_ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from agent_core.config import load_agent_config  # noqa: E402
from agent_core.kernel import KernelResult  # noqa: E402
from local_executor_dispatcher import LocalExecutorDispatcher  # noqa: E402
from task_store import TaskStore  # noqa: E402


def _local_cfg(tmp_path):
    """隔离配置：所有目录指向 tmp_path，关闭经验自动沉淀，避免污染真实数据。"""
    (tmp_path / "personas").mkdir(parents=True, exist_ok=True)
    return load_agent_config(
        project_root=str(tmp_path),
        work_dir=str(tmp_path / "work"),
        persona_dir=str(tmp_path / "personas"),
        memory_backend="local",
        memory_core_dir=str(tmp_path / "mem" / "core"),
        memory_context_dir=str(tmp_path / "mem" / "context"),
        experience_store_dir=str(tmp_path / "exp"),
        mcp_config_path="",
        auto_record_experience=False,
    )


def _dispatcher(tmp_path):
    store = TaskStore(str(tmp_path / "tasks"))
    return LocalExecutorDispatcher(task_store=store, config=_local_cfg(tmp_path)), store


def test_cancel_signals_inflight_execution_and_reports_cancelled(tmp_path):
    """cancel(task_id) 置位在飞任务 cancel_event；内核据此取消 -> 归类 cancelled。"""
    disp, store = _dispatcher(tmp_path)
    started = threading.Event()
    seen = {}

    def fake_run_oneshot(prompt, persona, cwd, context_memory="",
                         cancel_event=None, resume=False, record_experience=True):
        seen["cancel_event"] = cancel_event
        started.set()
        # 协作式：轮询 cancel_event，直到被取消（或超时兜底 release）
        deadline = time.monotonic() + 3.0
        while not cancel_event.is_set() and time.monotonic() < deadline:
            time.sleep(0.01)
        return KernelResult(output="partial", turns=1,
                            cancelled=cancel_event.is_set())

    disp._kernel.run_oneshot = fake_run_oneshot

    box = {}

    def run():
        box["res"] = disp.dispatch(role="executor", prompt="p")

    t = threading.Thread(target=run)
    t.start()
    assert started.wait(2.0), "内核未在时限内启动"
    # cancel_event 已登记并传入内核
    assert seen["cancel_event"] is not None
    task_id = next(iter(disp._cancel_events))
    assert disp.cancel(task_id) is True
    t.join(4.0)
    assert not t.is_alive()

    res = box["res"]
    assert res.status == "cancelled"
    # store 终态为 cancelled，未被收尾覆盖为 done
    assert store.get(task_id).status == "cancelled"
    # 执行结束后事件已从注册表清理
    assert task_id not in disp._cancel_events


def test_cancel_returns_false_for_unknown_task(tmp_path):
    disp, _ = _dispatcher(tmp_path)
    assert disp.cancel("task-nonexistent") is False


def test_finalize_does_not_overwrite_cancel(tmp_path):
    """收尾竞态：cancel 端点已置 cancelled，内核未观察到仍返回成功——
    finalize_if 不得把 cancelled 覆盖为 done。"""
    disp, store = _dispatcher(tmp_path)

    def fake_run_oneshot(prompt, persona, cwd, context_memory="",
                         cancel_event=None, resume=False, record_experience=True):
        # 模拟执行期间外部已把状态置为 cancelled（终态）
        tid = next(iter(disp._cancel_events))
        store.update(tid, status="cancelled")
        return KernelResult(output="完整结果", turns=2, is_error=False)

    disp._kernel.run_oneshot = fake_run_oneshot
    res = disp.dispatch(role="executor", prompt="p")
    assert res.status == "cancelled"
    rec = store.get(res.task_id)
    assert rec.status == "cancelled"
    assert rec.output != "完整结果"  # 迟到写入未覆盖终态


def test_dispatch_done_writes_terminal(tmp_path):
    """无取消的正常路径：仍如实写 done 终态与产物/输出（回归保护）。"""
    disp, store = _dispatcher(tmp_path)

    def fake_run_oneshot(prompt, persona, cwd, context_memory="",
                         cancel_event=None, resume=False, record_experience=True):
        Path(cwd).mkdir(parents=True, exist_ok=True)
        (Path(cwd) / "out.txt").write_text("产物", encoding="utf-8")
        return KernelResult(output="完成", turns=1, is_error=False,
                            artifacts=["out.txt"])

    disp._kernel.run_oneshot = fake_run_oneshot
    res = disp.dispatch(role="executor", prompt="p")
    assert res.status == "done"
    rec = store.get(res.task_id)
    assert rec.status == "done" and rec.output == "完成"
    assert "out.txt" in rec.artifacts
