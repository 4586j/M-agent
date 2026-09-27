"""测试 Plan-Execute 执行引擎（kernel._execute_plan_engine）与反思回流 meta。

阶段四：
- 规划轮 → next_pending_step 逐步骤驱动 → 重试/降级/断点续跑；
- 反思产物写入 KernelResult.meta["reflection"]（重试注入失败教训的数据源）。
全程假 SDK（按 query 调用序返回预设结果），不触网、不依赖真实 SDK 行为。
"""
import json
import sys
import types
from pathlib import Path

import pytest

from agent_core.kernel import AgentKernel
from agent_core.persona import Persona
from agent_core.planning import Plan, PlanStep, PlanningManager
from agent_core.tests.test_kernel import ResultMessage, _kernel_config

PERSONA = Persona(name="executor", system_prompt="执行人设")


# ---- 假 SDK：按调用序返回预设结果 ----

def _install_sequenced_sdk(monkeypatch, responses):
    """注入假 claude_agent_sdk，query() 按调用序从 responses 弹出结果。

    responses: list[str | tuple[str, bool]]——str 等价 (text, is_error=False)。
    捕获每次 prompt 供断言；超出预设数即 IndexError（调用次数精确可控）。
    """
    captured = {"prompts": []}
    items = [r if isinstance(r, tuple) else (r, False) for r in responses]

    class ClaudeAgentOptions:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    def query(prompt, options):
        captured["prompts"].append(prompt)
        text, is_error = items.pop(0)

        async def gen():
            yield ResultMessage(result=text, num_turns=1, is_error=is_error,
                                usage={"input_tokens": 10, "output_tokens": 5},
                                total_cost_usd=0.1, duration_ms=50,
                                session_id="plan-session-1")
        return gen()

    fake = types.ModuleType("claude_agent_sdk")
    fake.ClaudeAgentOptions = ClaudeAgentOptions
    fake.query = query
    monkeypatch.setitem(sys.modules, "claude_agent_sdk", fake)
    return captured


def _plan_json(steps):
    """构造规划轮输出的计划 JSON 文本。steps: (id, action, depends_on) 元组序列。"""
    return json.dumps({
        "goal": "测试目标",
        "steps": [{"id": i, "action": a, "depends_on": list(d)} for i, a, d in steps],
        "risks": [],
    }, ensure_ascii=False)


def _kernel(project_root, tmp_path, **overrides):
    """隔离配置的 kernel：记忆/经验全部指向 tmp，关掉经验沉淀。

    仅 planning_enabled 时把 project_root 指向 tmp（.orch_progress 进度目录隔离）——
    反思测试保持真实仓库根，以便 runtime memory_store 可导入并写入 tmp 记忆目录。
    """
    overrides.setdefault("auto_record_experience", False)
    cfg = _kernel_config(project_root, tmp_path, **overrides)
    if overrides.get("planning_enabled"):
        cfg.project_root = str(tmp_path / "projroot")
        Path(cfg.project_root).mkdir(parents=True, exist_ok=True)
    return AgentKernel(cfg), cfg


def _work_dir(tmp_path):
    d = tmp_path / "work"
    d.mkdir(parents=True, exist_ok=True)
    return str(d)


def _load_only_plan(cfg):
    """加载进度目录下唯一的计划文件（dict 形态）。"""
    files = list((Path(cfg.project_root) / ".orch_progress").glob("*.json"))
    assert len(files) == 1, files
    return json.loads(files[0].read_text(encoding="utf-8"))


# ---- Plan-Execute 引擎 ----

def test_plan_engine_completes_all_steps(monkeypatch, project_root, tmp_path):
    kernel, cfg = _kernel(project_root, tmp_path, planning_enabled=True)
    captured = _install_sequenced_sdk(monkeypatch, [
        _plan_json([(1, "步骤一", []), (2, "步骤二", [1])]),
        "步骤一产出", "步骤二产出",
    ])
    result = kernel.run_oneshot("做个任务", PERSONA, _work_dir(tmp_path))
    assert not result.is_error
    assert result.meta["plan_status"] == "complete"
    # 规划轮 + 两个步骤轮，prompt 依次注入规划指令 / 当前步骤 / 依赖产物
    p0, p1, p2 = captured["prompts"]
    assert "结构化执行计划" in p0 and "做个任务" in p0
    assert "[当前步骤 1]" in p1
    assert "[当前步骤 2]" in p2 and "步骤一产出" in p2  # done_summaries 注入
    # 规划轮与步骤轮的 turns/cost 全部累加
    assert result.turns == 3
    assert result.cost_usd == pytest.approx(0.3)
    # 计划实时持久化：全部步骤 done，产出回写
    plan = _load_only_plan(cfg)
    assert [s["status"] for s in plan["steps"]] == ["done", "done"]
    assert plan["steps"][1]["output"] == "步骤二产出"


def test_plan_engine_step_retry_then_success(monkeypatch, project_root, tmp_path):
    kernel, cfg = _kernel(project_root, tmp_path, planning_enabled=True)
    captured = _install_sequenced_sdk(monkeypatch, [
        _plan_json([(1, "步骤一", [])]),
        ("步骤一失败：文件被占用", True),
        "步骤一产出",
    ])
    result = kernel.run_oneshot("做个任务", PERSONA, _work_dir(tmp_path))
    assert not result.is_error
    assert result.meta["plan_status"] == "complete"
    assert len(captured["prompts"]) == 3
    retry_prompt = captured["prompts"][2]
    assert "上次执行该步骤失败" in retry_prompt        # 重试注入失败教训
    assert "文件被占用" in retry_prompt                # 教训含上次失败输出
    assert [s["status"] for s in _load_only_plan(cfg)["steps"]] == ["done"]


def test_plan_engine_retries_exhausted_terminates(monkeypatch, project_root, tmp_path):
    kernel, cfg = _kernel(project_root, tmp_path, planning_enabled=True,
                          plan_replan_enabled=False)
    captured = _install_sequenced_sdk(monkeypatch, [
        _plan_json([(1, "危险步骤", [])]),
        ("失败1", True), ("失败2", True), ("失败3", True),
    ])
    result = kernel.run_oneshot("做个任务", PERSONA, _work_dir(tmp_path))
    assert result.is_error
    assert result.meta["plan_status"] == "failed"
    assert result.meta["plan_failed_steps"] == [1]
    assert result.meta["plan"]["steps"][0]["status"] == "failed"
    assert "重试 2 次后仍失败" in result.output
    assert len(captured["prompts"]) == 4  # 规划 + 首次 + 2 次重试
    steps = _load_only_plan(cfg)["steps"]
    assert steps[0]["status"] == "failed" and steps[0]["output"]


def test_plan_engine_resume_skips_planning_round(monkeypatch, project_root, tmp_path):
    kernel, cfg = _kernel(project_root, tmp_path, planning_enabled=True)
    progress = Path(cfg.project_root) / ".orch_progress"
    plan = Plan(goal="测试目标", steps=[
        PlanStep(id=1, action="步骤一", status="done", output="步骤一产出"),
        PlanStep(id=2, action="步骤二", depends_on=[1]),
    ], source_prompt="任务R")
    PlanningManager(progress).save_plan("sess-resume", plan)

    captured = _install_sequenced_sdk(monkeypatch, ["步骤二续跑产出"])
    result = kernel.run_oneshot("任务R", PERSONA, _work_dir(tmp_path))
    assert not result.is_error
    assert result.meta["plan_status"] == "complete"
    assert len(captured["prompts"]) == 1              # 规划轮被跳过
    p = captured["prompts"][0]
    assert "[当前步骤 2]" in p and "步骤一产出" in p  # 直接从 pending 步骤续跑
    assert "结构化执行计划" not in p


def test_plan_engine_resume_resets_failed_steps_and_succeeds(monkeypatch, project_root, tmp_path):
    """续跑自愈：failed 步骤重置为 pending 重新执行，重试通过则整体完成。"""
    kernel, cfg = _kernel(project_root, tmp_path, planning_enabled=True)
    progress = Path(cfg.project_root) / ".orch_progress"
    plan = Plan(goal="测试目标", steps=[
        PlanStep(id=1, action="步骤一", status="failed", output="旧失败输出"),
        PlanStep(id=2, action="步骤二", depends_on=[1]),
    ], source_prompt="任务F")
    PlanningManager(progress).save_plan("sess-heal", plan)

    captured = _install_sequenced_sdk(monkeypatch, ["步骤一重试成功", "步骤二产出"])
    result = kernel.run_oneshot("任务F", PERSONA, _work_dir(tmp_path))
    assert not result.is_error
    assert result.meta["plan_status"] == "complete"
    # 跳过规划轮，从重置后的步骤 1 开始重新执行
    assert len(captured["prompts"]) == 2
    assert "[当前步骤 1]" in captured["prompts"][0]
    assert "[当前步骤 2]" in captured["prompts"][1]
    assert "步骤一重试成功" in captured["prompts"][1]
    # 落盘计划两步均为 done，旧失败输出被覆盖
    plan_after = _load_only_plan(cfg)
    assert [s["status"] for s in plan_after["steps"]] == ["done", "done"]
    assert plan_after["steps"][0]["output"] == "步骤一重试成功"


def test_plan_engine_resume_failed_retry_exhausted_reports_failed(monkeypatch, project_root, tmp_path):
    """失败计划续跑：重置后重试再耗尽则如实报 failed，绝不谎报 complete。"""
    kernel, cfg = _kernel(project_root, tmp_path, planning_enabled=True,
                          plan_replan_enabled=False)
    progress = Path(cfg.project_root) / ".orch_progress"
    plan = Plan(goal="测试目标", steps=[
        PlanStep(id=1, action="危险步骤", status="failed", output="旧失败输出"),
        PlanStep(id=2, action="步骤二", depends_on=[1]),
    ], source_prompt="任务F")
    PlanningManager(progress).save_plan("sess-broken", plan)

    captured = _install_sequenced_sdk(monkeypatch, [
        ("失败A", True), ("失败B", True), ("失败C", True),
    ])
    result = kernel.run_oneshot("任务F", PERSONA, _work_dir(tmp_path))
    assert result.is_error
    assert result.meta["plan_status"] == "failed"
    assert result.meta["plan_failed_steps"] == [1]
    assert "重试 2 次后仍失败" in result.output
    # 续跑无规划轮：3 个响应全部耗在步骤 1 的首次执行与两次重试上
    assert len(captured["prompts"]) == 3
    assert "[当前步骤 1]" in captured["prompts"][0]
    # 落盘计划保持 failed；依赖它的步骤 2 保持 pending，不产出假完成
    steps = _load_only_plan(cfg)["steps"]
    assert steps[0]["status"] == "failed"
    assert steps[1]["status"] == "pending"


def test_plan_engine_step_prompt_carries_context_memory(monkeypatch, project_root, tmp_path):
    """步骤轮 prompt 注入与规划轮相同的 context_memory，
    保证首跑与断点续跑的步骤输入一致。"""
    kernel, cfg = _kernel(project_root, tmp_path, planning_enabled=True)
    captured = _install_sequenced_sdk(monkeypatch, [
        _plan_json([(1, "步骤一", [])]),
        "步骤一产出",
    ])
    result = kernel.run_oneshot("做个任务", PERSONA, _work_dir(tmp_path),
                                context_memory="关键约定：产物一律 Markdown")
    assert not result.is_error
    planning_prompt, step_prompt = captured["prompts"]
    for p in (planning_prompt, step_prompt):
        assert "[上下文记忆]" in p
        assert "关键约定：产物一律 Markdown" in p
    assert "[当前步骤 1]" in step_prompt


def test_plan_engine_fallback_on_parse_failure(monkeypatch, project_root, tmp_path):
    kernel, cfg = _kernel(project_root, tmp_path, planning_enabled=True)
    captured = _install_sequenced_sdk(monkeypatch, [
        "我觉得不需要计划，直接开始干活。",
        "直接执行的产出",
    ])
    result = kernel.run_oneshot("做个任务", PERSONA, _work_dir(tmp_path))
    assert not result.is_error                        # 降级不报错
    assert result.output == "直接执行的产出"
    assert "plan_status" not in result.meta and "plan" not in result.meta
    assert len(captured["prompts"]) == 2
    assert "结构化执行计划" in captured["prompts"][0]      # 第一轮注入了规划指令
    assert "结构化执行计划" not in captured["prompts"][1]  # 降级为普通单轮
    assert not list((Path(cfg.project_root) / ".orch_progress").glob("*.json"))


# ---- 重规划 / 续跑重新校验（Slice 3 P1：依赖图 revalidation）----

def test_plan_engine_replan_valid_continues_and_completes(
        monkeypatch, project_root, tmp_path):
    """步骤耗尽 → 重规划产出有效新计划（无 id 冲突）→ 继续执行并完成。"""
    kernel, cfg = _kernel(project_root, tmp_path, planning_enabled=True)
    captured = _install_sequenced_sdk(monkeypatch, [
        _plan_json([(1, "危险步骤", [])]),
        ("失败1", True), ("失败2", True), ("失败3", True),
        _plan_json([(1, "改进步骤A", [])]),   # 重规划：done 为空，无 id 冲突
        "改进步骤A产出",
    ])
    result = kernel.run_oneshot("做个任务", PERSONA, _work_dir(tmp_path))
    assert not result.is_error
    assert result.meta["plan_status"] == "complete"
    assert len(captured["prompts"]) == 6
    # 重规划轮注入了 id 接续指引（降低与已完成步骤冲突的概率）
    assert "重新规划剩余步骤" in captured["prompts"][4]
    assert "新步骤 id 从" in captured["prompts"][4]


def test_plan_engine_replan_malformed_rejected_reports_failed(
        monkeypatch, project_root, tmp_path):
    """重规划合并后出现重复 id（与已完成步骤冲突）→ 拒绝采用、如实报 failed，
    绝不静默执行歧义图。"""
    kernel, cfg = _kernel(project_root, tmp_path, planning_enabled=True)
    captured = _install_sequenced_sdk(monkeypatch, [
        _plan_json([(1, "步骤一", []), (2, "步骤二", [1])]),
        "步骤一产出",                          # 步骤 1 done
        ("失败1", True), ("失败2", True), ("失败3", True),  # 步骤 2 耗尽
        _plan_json([(1, "冲突步骤", [])]),      # 新步骤 id=1 与 done 步骤 1 冲突
    ])
    result = kernel.run_oneshot("做个任务", PERSONA, _work_dir(tmp_path))
    assert result.is_error
    assert result.meta["plan_status"] == "failed"
    assert result.meta["plan_failed_steps"] == [2]
    assert len(captured["prompts"]) == 6
    # 落盘计划保持旧图：步骤 1 done、步骤 2 failed（未写入歧义合并图）
    steps = _load_only_plan(cfg)["steps"]
    assert steps[0]["status"] == "done"
    assert steps[1]["status"] == "failed"


def test_plan_engine_resume_invalid_plan_replans(monkeypatch, project_root, tmp_path):
    """持久化的续跑计划若依赖图非法（环）→ 放弃续跑、重新走规划轮。"""
    kernel, cfg = _kernel(project_root, tmp_path, planning_enabled=True)
    progress = Path(cfg.project_root) / ".orch_progress"
    cyclic = Plan(goal="坏计划", steps=[
        PlanStep(id=1, action="A", depends_on=[2]),
        PlanStep(id=2, action="B", depends_on=[1]),
    ], source_prompt="任务X")
    PlanningManager(progress).save_plan("sess-cycle", cyclic)

    captured = _install_sequenced_sdk(monkeypatch, [
        _plan_json([(1, "新步骤", [])]),   # 重新规划轮
        "新步骤产出",
    ])
    result = kernel.run_oneshot("任务X", PERSONA, _work_dir(tmp_path))
    assert not result.is_error
    assert result.meta["plan_status"] == "complete"
    # 非法续跑计划被拒 → 走了规划轮（与「有效计划跳过规划轮」相反）
    assert len(captured["prompts"]) == 2
    assert "结构化执行计划" in captured["prompts"][0]


# ---- 反思回流 meta ----

def test_reflection_written_to_meta_on_failure(monkeypatch, project_root, tmp_path):
    kernel, cfg = _kernel(project_root, tmp_path, reflection_enabled=True)
    captured = _install_sequenced_sdk(monkeypatch, [
        ("任务失败：目标机器磁盘已满", True),
        "根本原因：磁盘空间不足；改进建议：执行前先清理磁盘",
    ])
    result = kernel.run_oneshot("会失败的任务", PERSONA, _work_dir(tmp_path))
    assert result.is_error
    assert result.meta["reflection"] == \
        "根本原因：磁盘空间不足；改进建议：执行前先清理磁盘"
    # 失败场景走 build_failure_reflection_prompt（事后复盘）
    assert "事后复盘" in captured["prompts"][1]


def test_reflection_on_success_writes_memory(monkeypatch, project_root, tmp_path):
    kernel, cfg = _kernel(project_root, tmp_path, reflection_enabled=True)
    _install_sequenced_sdk(monkeypatch, [
        "任务产出：报告已生成",
        "质量评估：high；建议：补充数据来源",
    ])
    result = kernel.run_oneshot("写报告", PERSONA, _work_dir(tmp_path))
    assert not result.is_error
    assert result.meta["reflection"].startswith("质量评估")
    # 成功场景反思同时写入任务上下文记忆
    f = Path(cfg.memory_context_dir) / "plan-session-1" / "reflection.md"
    assert f.is_file()
    assert "质量评估" in f.read_text(encoding="utf-8")


def test_reflection_disabled_keeps_meta_clean(monkeypatch, project_root, tmp_path):
    kernel, cfg = _kernel(project_root, tmp_path)  # reflection_enabled 默认 False
    _install_sequenced_sdk(monkeypatch, ["普通产出"])
    result = kernel.run_oneshot("任务", PERSONA, _work_dir(tmp_path))
    assert "reflection" not in result.meta
