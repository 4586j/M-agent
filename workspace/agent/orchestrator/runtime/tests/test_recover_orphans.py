"""启动恢复（checkpoint 恢复基线）测试：本地执行孤儿任务标记 failed。

本地执行体与 brain 同进程——brain 重启即执行体消亡，遗留的
running/pending/dispatching 任务永远不会有结果。recover_orphans 在
brain 装配时（开始服务前）把它们标记 failed 供重派，不会误标本进程
新派发的任务。
"""
import sys
from pathlib import Path

_RUNTIME_DIR = Path(__file__).resolve().parents[1]
_PROJECT_ROOT = _RUNTIME_DIR.parents[3]
for _p in (str(_RUNTIME_DIR), str(_PROJECT_ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from task_store import TaskRecord, TaskStore  # noqa: E402
from local_executor_dispatcher import LocalExecutorDispatcher  # noqa: E402

from tests.test_local_executor_cancel import _local_cfg  # noqa: E402


def _dispatcher_with_tasks(tmp_path, statuses):
    """按给定状态序列播种任务（task-t0, task-t1, ...），返回 (disp, store)。"""
    store = TaskStore(str(tmp_path / "tasks"))
    for i, status in enumerate(statuses):
        ts = str(1000 + i)
        store.write(TaskRecord(
            task_id=f"task-{i}", role="executor", prompt=f"p{i}",
            worker_id="local", status=status, created_at=ts, updated_at=ts))
    return LocalExecutorDispatcher(task_store=store,
                                   config=_local_cfg(tmp_path)), store


def test_orphans_marked_failed(tmp_path):
    disp, store = _dispatcher_with_tasks(
        tmp_path, ["running", "pending", "dispatching", "done", "failed",
                   "cancelled", "awaiting_approval"])
    affected = disp.recover_orphans()
    assert affected == 3  # running/pending/dispatching 三种
    assert store.get("task-0").status == "failed"
    assert "重启" in store.get("task-0").error
    assert store.get("task-1").status == "failed"
    assert store.get("task-2").status == "failed"
    # 终态与待审批不受影响
    assert store.get("task-3").status == "done"
    assert store.get("task-4").status == "failed"  # 原本就是 failed
    assert "重启" not in (store.get("task-4").error or "")
    assert store.get("task-5").status == "cancelled"
    assert store.get("task-6").status == "awaiting_approval"


def test_recover_idempotent_on_empty(tmp_path):
    """无孤儿任务时返回 0（幂等，不报错）。"""
    disp, store = _dispatcher_with_tasks(tmp_path, ["done"])
    assert disp.recover_orphans() == 0
