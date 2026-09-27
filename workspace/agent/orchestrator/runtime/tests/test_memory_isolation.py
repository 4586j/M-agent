"""Slice 4 记忆隔离门：工作区/会话不泄漏记忆（no cross-run leakage）。

方案门（Slice 4）：「Workspaces and sessions do not leak memory.」
评测门：「Keep fixtures and evaluation memories isolated from normal user work.」

本文件锁定四条隔离不变量，全部离线、不触网、不依赖真实 SDK：
1. 工作区隔离（core 记忆）：不同 store_dir 的 FileMemoryStore 互不可见；
2. 工作区隔离（经验库）：不同 store_dir 的 ExperienceStore 记录/检索互不串台；
3. 会话隔离（context 记忆）：同一 store 内按 task_id 分目录，跨任务读不到，
   且 delete_context 只清目标任务、不波及他会话；
4. 派发运行隔离：LocalExecutorDispatcher 为每个任务开独立 cwd，
   两次派发的产物互不落到对方工作目录（per-run workspace 不泄漏）。
"""
import sys
from pathlib import Path

_RUNTIME_DIR = Path(__file__).resolve().parents[1]
_PROJECT_ROOT = _RUNTIME_DIR.parents[3]
for _p in (str(_RUNTIME_DIR), str(_PROJECT_ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from agent_core.config import load_agent_config  # noqa: E402
from agent_core.kernel import KernelResult  # noqa: E402
from experience_store import ExperienceStore  # noqa: E402
from local_executor_dispatcher import LocalExecutorDispatcher  # noqa: E402
from memory_store import FileMemoryStore  # noqa: E402
from task_store import TaskStore  # noqa: E402


# ------------------------------------------------------- 1. 工作区隔离：core

def test_workspace_core_memory_isolated(tmp_path):
    """不同工作区（core_dir）的长期记忆互不可见。"""
    a = FileMemoryStore(core_dir=str(tmp_path / "wsA" / "core"),
                        context_dir=str(tmp_path / "wsA" / "ctx"))
    b = FileMemoryStore(core_dir=str(tmp_path / "wsB" / "core"),
                        context_dir=str(tmp_path / "wsB" / "ctx"))
    a.write_core("proj-pref", "工作区A的偏好")
    assert a.read_core("proj-pref") == "工作区A的偏好"
    # B 读不到 A 的 core 记忆（工作区隔离）
    assert b.read_core("proj-pref") is None
    assert b.list_core() == []


# --------------------------------------------------- 2. 工作区隔离：经验库

def test_workspace_experience_store_isolated(tmp_path):
    """不同经验库目录互不串台：A 沉淀的经验在 B 检索/列举不到。"""
    exp_a = ExperienceStore(str(tmp_path / "wsA" / "exp"))
    exp_b = ExperienceStore(str(tmp_path / "wsB" / "exp"))
    exp_a.record_from_task(task_id="tA", role="executor",
                           prompt="compute the total order amount",
                           output="TOTAL=42", success=True, created_at="1")
    assert len(exp_a.list_all()) == 1
    assert exp_a.search("compute the total order amount", role="executor")
    # 工作区 B 完全看不到 A 的经验
    assert exp_b.list_all() == []
    assert exp_b.search("compute the total order amount", role="executor") == []


# --------------------------------------------------- 3. 会话隔离：context

def test_session_context_isolated_and_scoped_delete(tmp_path):
    """同一 store 内按 task_id 分会话：跨会话读不到，删除只影响目标任务。"""
    ms = FileMemoryStore(core_dir=str(tmp_path / "core"),
                         context_dir=str(tmp_path / "ctx"))
    ms.write_context("task-A", "tried", "会话A的方案")
    ms.write_context("task-B", "tried", "会话B的方案")
    # 会话隔离：各自读到各自的，读不到对方的
    assert ms.read_context("task-A", "tried") == "会话A的方案"
    assert ms.read_context("task-B", "tried") == "会话B的方案"
    # 全新会话读同名 key 返回 None（无跨运行泄漏）
    assert ms.read_context("task-C", "tried") is None
    # 定向清理只清 task-A，不波及 task-B
    assert ms.delete_context("task-A") is True
    assert ms.read_context("task-A", "tried") is None
    assert ms.read_context("task-B", "tried") == "会话B的方案"


# --------------------------------------------------- 4. 派发运行隔离：cwd

def _local_cfg(tmp_path, ws):
    (tmp_path / ws / "personas").mkdir(parents=True, exist_ok=True)
    return load_agent_config(
        project_root=str(tmp_path / ws),
        work_dir=str(tmp_path / ws / "work"),
        persona_dir=str(tmp_path / ws / "personas"),
        memory_backend="local",
        memory_core_dir=str(tmp_path / ws / "mem" / "core"),
        memory_context_dir=str(tmp_path / ws / "mem" / "context"),
        experience_store_dir=str(tmp_path / ws / "exp"),
        mcp_config_path="",
        auto_record_experience=False,
    )


def test_dispatch_runs_get_isolated_workspaces(tmp_path):
    """两次派发各得独立 cwd；一个运行的产物不落到另一个的工作目录。"""
    cfg = _local_cfg(tmp_path, "ws")
    store = TaskStore(str(tmp_path / "ws" / "tasks"))
    disp = LocalExecutorDispatcher(task_store=store, config=cfg)
    seen_cwds = []

    def fake_run_oneshot(prompt, persona, cwd, context_memory="",
                         cancel_event=None, resume=False, record_experience=True):
        seen_cwds.append(cwd)
        Path(cwd).mkdir(parents=True, exist_ok=True)
        # 每个运行在自己的 cwd 落一个以 prompt 命名的产物
        (Path(cwd) / f"{prompt}.txt").write_text(prompt, encoding="utf-8")
        return KernelResult(output=prompt, turns=1, is_error=False,
                            artifacts=[f"{prompt}.txt"])

    disp._kernel.run_oneshot = fake_run_oneshot

    r1 = disp.dispatch(role="executor", prompt="run1")
    r2 = disp.dispatch(role="executor", prompt="run2")
    assert r1.status == "done" and r2.status == "done"

    # 运行隔离：两个 cwd 互不相同
    assert seen_cwds[0] != seen_cwds[1]
    cwd1, cwd2 = Path(seen_cwds[0]), Path(seen_cwds[1])
    # 各自只含自己的产物，绝不含对方的（无跨运行泄漏）
    assert (cwd1 / "run1.txt").is_file()
    assert not (cwd1 / "run2.txt").exists()
    assert (cwd2 / "run2.txt").is_file()
    assert not (cwd2 / "run1.txt").exists()
