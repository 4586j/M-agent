"""测试 LocalExecutorDispatcher 的经验消融硬关（Slice 4）。

核心门：use_experience=False 时「真正禁用经验读写」——
- 读关：不检索/不注入历史经验（context_memory 不含 [历史经验参考] 块）；
- 写关：内核 run_oneshot 收到 record_experience=False，且派发层不调用
  experience_store.record_from_task。
默认（True）时读写照常（回归保护）。全程 monkeypatch 内核，不触网、不依赖真实 SDK。
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
from local_executor_dispatcher import LocalExecutorDispatcher  # noqa: E402
from task_store import TaskStore  # noqa: E402


def _local_cfg(tmp_path):
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


class _FakeExp:
    def __init__(self):
        self.search_calls = 0
        self.recorded = []

    def search(self, prompt, role=None, successful_only=True, limit=3):
        self.search_calls += 1
        return [type("E", (), {"prompt": "历史任务",
                               "output_summary": "历史产出"})()]

    def record_from_task(self, **kw):
        self.recorded.append(kw)


def _dispatcher(tmp_path, exp):
    store = TaskStore(str(tmp_path / "tasks"))
    disp = LocalExecutorDispatcher(task_store=store, config=_local_cfg(tmp_path),
                                   experience_store=exp)
    captured = {}

    def fake_run_oneshot(prompt, persona, cwd, context_memory="",
                         cancel_event=None, resume=False, record_experience=True):
        captured["context_memory"] = context_memory
        captured["record_experience"] = record_experience
        Path(cwd).mkdir(parents=True, exist_ok=True)
        return KernelResult(output="完成", turns=1, is_error=False)

    disp._kernel.run_oneshot = fake_run_oneshot
    return disp, store, captured


def test_local_ablation_disables_read_and_write(tmp_path):
    exp = _FakeExp()
    disp, store, captured = _dispatcher(tmp_path, exp)
    res = disp.dispatch(role="executor", prompt="p", use_experience=False)
    assert res.status == "done"
    # 读关：未注入历史经验块，且未检索
    assert "历史经验参考" not in captured["context_memory"]
    assert exp.search_calls == 0
    # 写关（内核）：record_experience=False 透传
    assert captured["record_experience"] is False
    # 写关（派发层）：未沉淀
    assert exp.recorded == []


def test_local_with_experience_enriches_and_records(tmp_path):
    exp = _FakeExp()
    disp, store, captured = _dispatcher(tmp_path, exp)
    res = disp.dispatch(role="executor", prompt="p")   # 默认 use_experience=True
    assert res.status == "done"
    assert "历史经验参考" in captured["context_memory"]
    assert exp.search_calls == 1
    assert captured["record_experience"] is True
    assert len(exp.recorded) == 1
