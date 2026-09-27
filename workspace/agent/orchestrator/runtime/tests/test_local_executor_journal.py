"""测试 LocalExecutorDispatcher 的持久化日志接线（Slice 3 rollout flag）。

覆盖：
- 开关关闭（默认）：journal 为 None，派发行为与既有一致，不落任何 SQLite 文件；
- 开关开启（注入 RunStore）：有界准入——饱和时显式拒绝（status="rejected"），
  不创建 JSON 任务、不真正执行；
- 正常派发把终态回写日志（任务 done + 尝试 done）；
- 日志写入故障降级为放行（JSON 仍权威），绝不因持久化底座问题阻断执行。

全程 monkeypatch 内核 run_oneshot，不触网、不依赖真实 SDK。
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
from agent_core.run_store import RunStore  # noqa: E402
from local_executor_dispatcher import LocalExecutorDispatcher  # noqa: E402
from task_store import TaskStore  # noqa: E402


def _local_cfg(tmp_path, **kw):
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
        **kw,
    )


def _fake_done(prompt, persona, cwd, context_memory="", cancel_event=None,
               resume=False, record_experience=True):
    Path(cwd).mkdir(parents=True, exist_ok=True)
    return KernelResult(output="完成", turns=1, is_error=False)


# ---- 开关关闭：零行为变化 ----

def test_flag_off_journal_is_none_and_no_sqlite(tmp_path):
    store = TaskStore(str(tmp_path / "tasks"))
    disp = LocalExecutorDispatcher(task_store=store, config=_local_cfg(tmp_path))
    assert disp.journal is None
    disp._kernel.run_oneshot = _fake_done
    res = disp.dispatch(role="executor", prompt="p")
    assert res.status == "done"
    # 未在 work_dir 落任何 SQLite 日志文件
    assert not list((tmp_path / "work").glob("*.sqlite3"))


# ---- 开关开启：有界准入 + 尝试谱系 ----

def test_journal_admission_rejects_when_saturated(tmp_path):
    store = TaskStore(str(tmp_path / "tasks"))
    journal = RunStore(tmp_path / "work" / "runtime.sqlite3", max_active=1)
    try:
        cfg = _local_cfg(tmp_path)
        disp = LocalExecutorDispatcher(task_store=store, config=cfg, journal=journal)
        disp._kernel.run_oneshot = _fake_done
        # 占满唯一准入额度：先在日志里认领一个在飞尝试
        journal.create_task("blocker", "r", "p", workspace_key=cfg.work_dir)
        claim = journal.try_claim("blocker", workspace_key=cfg.work_dir,
                                  worker="other")
        assert claim.admitted is True
        # 新派发因饱和/串行化被显式拒绝，不真正执行、不落 JSON 任务
        res = disp.dispatch(role="executor", prompt="p")
        assert res.status == "rejected"
        assert res.error in ("saturated", "busy:workspace")
        assert store.get(res.task_id) is None
    finally:
        journal.close()


def test_journal_records_terminal_on_success(tmp_path):
    store = TaskStore(str(tmp_path / "tasks"))
    journal = RunStore(tmp_path / "work" / "runtime.sqlite3", max_active=2)
    try:
        cfg = _local_cfg(tmp_path)
        disp = LocalExecutorDispatcher(task_store=store, config=cfg, journal=journal)
        disp._kernel.run_oneshot = _fake_done
        res = disp.dispatch(role="executor", prompt="p")
        assert res.status == "done"
        # 日志回写：任务终态 done + 一条 done 尝试
        jtask = journal.get_task(res.task_id)
        assert jtask is not None and jtask["status"] == "done"
        attempts = journal.list_attempts(res.task_id)
        assert len(attempts) == 1 and attempts[0]["status"] == "done"
        # JSON 仍权威可读
        assert store.get(res.task_id).status == "done"
    finally:
        journal.close()


def test_journal_error_degrades_to_json_path(tmp_path, monkeypatch):
    """日志准入抛异常 → 降级放行（JSON 仍权威），派发不因底座故障失败。"""
    store = TaskStore(str(tmp_path / "tasks"))
    journal = RunStore(tmp_path / "work" / "runtime.sqlite3", max_active=2)
    try:
        cfg = _local_cfg(tmp_path)
        disp = LocalExecutorDispatcher(task_store=store, config=cfg, journal=journal)

        def boom(*a, **k):
            raise RuntimeError("journal down")

        monkeypatch.setattr(journal, "create_task", boom)
        disp._kernel.run_oneshot = _fake_done
        res = disp.dispatch(role="executor", prompt="p")
        # 未被拒绝，正常执行并写 JSON 终态
        assert res.status == "done"
        assert store.get(res.task_id).status == "done"
    finally:
        journal.close()
