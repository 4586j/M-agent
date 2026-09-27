"""AdvisorService 单元测试：隔离契约、JSON 解析/降级、模式映射、PlanEngine 隔离。

核心红线：顾问运行于独立上下文，绝不污染任务上下文——
独立 cwd（adv-*）、context_memory=""、record_experience=False、不传 resume（全新会话）、
不回写记忆/经验。这些断言用假 kernel 捕获 run_oneshot 的入参来验证。
"""
from pathlib import Path
from types import SimpleNamespace

from agent_core.advisor import AdvisorResult, AdvisorService, _extract_json
from agent_core.config import load_agent_config

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_AGENTS_DIR = _PROJECT_ROOT / ".claude" / "agents"

_GOOD_JSON = ('{"should_dispatch": true, "roles": ["executor", "security-guard"], '
              '"plan": [{"id": 1, "action": "读取数据", "depends_on": []}, '
              '{"id": 2, "action": "汇总", "depends_on": [1]}], '
              '"explanation": "任务可拆两步", "risks": ["数据量大"]}')


class _FakeKernel:
    """假内核：捕获 run_oneshot 入参与所用配置，返回预设输出。"""

    def __init__(self, config, output=_GOOD_JSON, raises=None):
        self.config = config
        self._output = output
        self._raises = raises
        self.calls: list[dict] = []

    def run_oneshot(self, prompt, persona, cwd, context_memory="",
                    record_experience=True, **extra):
        self.calls.append({
            "prompt": prompt, "persona": persona, "cwd": cwd,
            "context_memory": context_memory, "record_experience": record_experience,
            "extra": extra,
        })
        if self._raises is not None:
            raise self._raises
        return SimpleNamespace(output=self._output, meta={}, is_error=False,
                               cancelled=False)


def _factory(kernels: list, output=_GOOD_JSON, raises=None):
    def f(cfg):
        k = _FakeKernel(cfg, output=output, raises=raises)
        kernels.append(k)
        return k
    return f


def _cfg(tmp_path, **overrides):
    base = dict(
        project_root=str(tmp_path),
        work_dir=str(tmp_path / "work"),
        persona_dir=str(tmp_path / "agents"),
        advisor_work_dir=str(tmp_path / "adv"),
    )
    base.update(overrides)
    return load_agent_config(**base)


# ---- 隔离契约（核心要求）----

def test_recommend_runs_in_isolated_context(tmp_path):
    cfg = _cfg(tmp_path)
    kernels: list = []
    svc = AdvisorService(cfg, kernel_factory=_factory(kernels))
    res = svc.recommend("分析 workspace/data.csv 并报告行数", mode="all")

    assert isinstance(res, AdvisorResult)
    k = kernels[0]
    call = k.calls[0]
    # 不读上下文记忆、不写经验
    assert call["context_memory"] == ""
    assert call["record_experience"] is False
    # 全新会话：从不传 resume / cancel_event
    assert "resume" not in call["extra"]
    # 独立 cwd：位于 advisor_work_dir 下、以 adv- 开头，绝不复用任务目录
    cwd = Path(call["cwd"])
    assert cwd.parent == Path(cfg.advisor_work_dir)
    assert cwd.name.startswith("adv-")
    # 隔离配置兜底再关一次经验沉淀
    assert k.config.auto_record_experience is False


def test_isolated_config_does_not_mutate_shared_config(tmp_path):
    cfg = _cfg(tmp_path, planning_enabled=False)
    kernels: list = []
    svc = AdvisorService(cfg, kernel_factory=_factory(kernels))
    svc.recommend("复杂任务", mode="plan", enable_plan_engine=True)
    # plan 模式 + 显式开启 → 隔离配置 planning_enabled=True
    assert kernels[0].config.planning_enabled is True
    # 共享配置对象未被改动（无跨任务串味）
    assert cfg.planning_enabled is False


def test_pre_flight_mode_does_not_enable_planning(tmp_path):
    cfg = _cfg(tmp_path)
    kernels: list = []
    svc = AdvisorService(cfg, kernel_factory=_factory(kernels))
    svc.recommend("t", mode="pre_flight", enable_plan_engine=True)
    assert kernels[0].config.planning_enabled is False


# ---- JSON 解析与降级 ----

def test_parses_structured_json(tmp_path):
    cfg = _cfg(tmp_path)
    svc = AdvisorService(cfg, kernel_factory=_factory([], output=_GOOD_JSON))
    res = svc.recommend("t", mode="all")
    assert res.should_dispatch is True
    assert res.roles == ["executor", "security-guard"]
    assert len(res.plan) == 2
    assert res.plan[1]["depends_on"] == [1]
    assert res.explanation == "任务可拆两步"
    assert res.risks == ["数据量大"]


def test_parses_json_inside_code_fence(tmp_path):
    fenced = "```json\n" + _GOOD_JSON + "\n```"
    cfg = _cfg(tmp_path)
    svc = AdvisorService(cfg, kernel_factory=_factory([], output=fenced))
    res = svc.recommend("t")
    assert res.should_dispatch is True
    assert res.roles == ["executor", "security-guard"]


def test_malformed_output_degrades_gracefully(tmp_path):
    cfg = _cfg(tmp_path)
    svc = AdvisorService(cfg, kernel_factory=_factory([], output="这不是 JSON"))
    res = svc.recommend("t")
    assert res.should_dispatch is False
    assert res.roles == [] and res.plan == [] and res.risks == []
    assert res.explanation == "这不是 JSON"  # 原文兜底进 explanation
    assert res.raw == "这不是 JSON"


def test_kernel_exception_degrades_gracefully(tmp_path):
    cfg = _cfg(tmp_path)
    svc = AdvisorService(cfg, kernel_factory=_factory([], raises=RuntimeError("boom")))
    res = svc.recommend("t")
    assert res.should_dispatch is False
    assert "顾问执行失败" in res.explanation


def test_extract_json_variants():
    assert _extract_json('{"a": 1}') == {"a": 1}
    assert _extract_json('前言 {"a": 1} 后语') == {"a": 1}
    assert _extract_json("no json here") is None
    assert _extract_json("") is None
    # 数组不是对象 → None（只接受 dict）
    assert _extract_json("[1, 2, 3]") is None


def test_invalid_mode_falls_back_to_all(tmp_path):
    cfg = _cfg(tmp_path)
    svc = AdvisorService(cfg, kernel_factory=_factory([], output=_GOOD_JSON))
    res = svc.recommend("t", mode="bogus")
    assert res.mode == "all"


# ---- 模式提示构造 ----

def test_explain_mode_includes_finished_output(tmp_path):
    cfg = _cfg(tmp_path)
    kernels: list = []
    svc = AdvisorService(cfg, kernel_factory=_factory(kernels))
    svc.recommend("t", mode="explain", finished_output="RESULT=42")
    prompt = kernels[0].calls[0]["prompt"]
    assert "已完成产出" in prompt
    assert "RESULT=42" in prompt


# ---- 真实 advisor 人设（只读、可加载）----

def test_real_advisor_persona_is_read_only():
    from agent_core.persona import load_persona_by_name
    p = load_persona_by_name("advisor", str(_AGENTS_DIR))
    assert p.name == "advisor"
    tools = set(p.tools or [])
    assert tools <= {"Read", "Glob", "Grep", "WebFetch"}
    # 物理隔离：无写入/执行/派发工具
    assert not (tools & {"Write", "Edit", "Bash", "NotebookEdit", "Agent"})
