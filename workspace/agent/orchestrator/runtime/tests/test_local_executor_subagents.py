"""测试 LocalExecutorDispatcher 的每次请求子代理派发覆盖（enable_subagents）。

覆盖：
- enable_subagents=False 且全局默认 True -> 构造一次性覆盖内核（subagents_enabled=False），
  并在该覆盖内核上执行 run_oneshot（不复用共享内核）；
- enable_subagents=True 且全局默认 True -> 与全局一致，不构造新内核，复用共享内核；
- enable_subagents=None -> 跟随全局，复用共享内核；
- enable_subagents=True 且全局默认 False -> 双向覆盖：构造 subagents_enabled=True 的内核。

全程 monkeypatch，不触网、不依赖真实 SDK。覆盖内核经 kernel._build_options 的单一闸
（config.subagents_enabled）决定是否把 persona.sub_agents 映射为 options.agents。
"""
import sys
from pathlib import Path

_RUNTIME_DIR = Path(__file__).resolve().parents[1]
_PROJECT_ROOT = _RUNTIME_DIR.parents[3]
for _p in (str(_RUNTIME_DIR), str(_PROJECT_ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import local_executor_dispatcher as led  # noqa: E402
from agent_core.config import load_agent_config  # noqa: E402
from agent_core.kernel import KernelResult  # noqa: E402
from local_executor_dispatcher import LocalExecutorDispatcher  # noqa: E402
from task_store import TaskStore  # noqa: E402


def _local_cfg(tmp_path, **overrides):
    (tmp_path / "personas").mkdir(parents=True, exist_ok=True)
    base = dict(
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
    base.update(overrides)
    return load_agent_config(**base)


def _dispatcher(tmp_path, **cfg_overrides):
    store = TaskStore(str(tmp_path / "tasks"))
    disp = LocalExecutorDispatcher(task_store=store,
                                   config=_local_cfg(tmp_path, **cfg_overrides))
    return disp, store


def _install_probes(disp, monkeypatch):
    """探针：记录共享内核是否被用、覆盖内核以何配置构造并被用。"""
    seen = {"shared_used": False, "override_configs": [], "override_used": False}

    def shared_run_oneshot(prompt, persona, cwd, context_memory="",
                           cancel_event=None, resume=False, record_experience=True):
        seen["shared_used"] = True
        return KernelResult(output="ok", turns=1, is_error=False)

    disp._kernel.run_oneshot = shared_run_oneshot

    class _FakeKernel:
        def __init__(self, config):
            seen["override_configs"].append(config)
            self.config = config

        def run_oneshot(self, prompt, persona, cwd, context_memory="",
                        cancel_event=None, resume=False, record_experience=True):
            seen["override_used"] = True
            return KernelResult(output="ok", turns=1, is_error=False)

    monkeypatch.setattr(led, "AgentKernel", _FakeKernel)
    return seen


def test_disable_subagents_builds_override_kernel(tmp_path, monkeypatch):
    """全局默认 True，请求 enable_subagents=False -> 构造 subagents_enabled=False 的覆盖内核。"""
    disp, _ = _dispatcher(tmp_path, subagents_enabled=True)
    seen = _install_probes(disp, monkeypatch)

    res = disp.dispatch(role="executor", prompt="p", enable_subagents=False)
    assert res.status == "done"
    # 用了覆盖内核，未复用共享内核
    assert seen["override_used"] is True
    assert seen["shared_used"] is False
    assert len(seen["override_configs"]) == 1
    assert seen["override_configs"][0].subagents_enabled is False
    # 覆盖不改动共享配置（无跨任务串味）
    assert disp.config.subagents_enabled is True


def test_enable_subagents_matching_global_reuses_shared_kernel(tmp_path, monkeypatch):
    """全局默认 True，请求 enable_subagents=True -> 与全局一致，不构造新内核。"""
    disp, _ = _dispatcher(tmp_path, subagents_enabled=True)
    seen = _install_probes(disp, monkeypatch)

    res = disp.dispatch(role="executor", prompt="p", enable_subagents=True)
    assert res.status == "done"
    assert seen["override_configs"] == []   # 未构造覆盖内核
    assert seen["shared_used"] is True      # 复用共享内核
    assert seen["override_used"] is False


def test_none_follows_global_config(tmp_path, monkeypatch):
    """enable_subagents=None -> 跟随全局，复用共享内核。"""
    disp, _ = _dispatcher(tmp_path, subagents_enabled=True)
    seen = _install_probes(disp, monkeypatch)

    res = disp.dispatch(role="executor", prompt="p", enable_subagents=None)
    assert res.status == "done"
    assert seen["override_configs"] == []
    assert seen["shared_used"] is True


def test_enable_overrides_disabled_global(tmp_path, monkeypatch):
    """双向覆盖：全局默认 False，请求 enable_subagents=True -> 构造 subagents_enabled=True 内核。"""
    disp, _ = _dispatcher(tmp_path, subagents_enabled=False)
    seen = _install_probes(disp, monkeypatch)

    res = disp.dispatch(role="executor", prompt="p", enable_subagents=True)
    assert res.status == "done"
    assert seen["override_used"] is True
    assert len(seen["override_configs"]) == 1
    assert seen["override_configs"][0].subagents_enabled is True
    assert disp.config.subagents_enabled is False
