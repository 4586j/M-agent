"""测试 agent_core.planning：Plan 序列化、进度持久化、计划解析与提示构造。"""
import json

from agent_core.planning import (Plan, PlanningManager, PlanStep,
                                 build_planning_prompt, build_reflection_prompt,
                                 parse_plan_from_output)


def _sample_plan() -> Plan:
    return Plan(
        goal="构建一个网页",
        steps=[
            PlanStep(id=1, action="设计结构", depends_on=[]),
            PlanStep(id=2, action="写样式", depends_on=[1]),
            PlanStep(id=3, action="联调", depends_on=[1, 2]),
        ],
        risks=["时间不足", "依赖缺失"],
        created_at="2026-01-01 00:00:00",
    )


# ---- Plan.to_dict / from_dict ----

def test_plan_to_dict_shape():
    d = _sample_plan().to_dict()
    assert d["goal"] == "构建一个网页"
    assert len(d["steps"]) == 3
    assert d["steps"][1]["depends_on"] == [1]
    assert d["risks"] == ["时间不足", "依赖缺失"]
    # 可 JSON 序列化
    json.dumps(d, ensure_ascii=False)


def test_plan_roundtrip_consistent():
    original = _sample_plan()
    restored = Plan.from_dict(original.to_dict())
    assert restored.to_dict() == original.to_dict()
    assert restored.goal == original.goal
    assert [s.id for s in restored.steps] == [1, 2, 3]
    assert restored.steps[2].depends_on == [1, 2]


def test_plan_from_dict_defensive():
    """脏数据：缺字段、非法 id、非 dict 步骤项都不应崩溃。"""
    d = {
        "goal": "g",
        "steps": [
            {"action": "无 id 步骤"},               # id 缺省 -> 自增
            "不是字典的项",                          # 非 dict -> 跳过
            {"id": "bad", "action": "id 非法"},     # id 非法 -> 回退自增
            {"id": 5, "action": "正常", "depends_on": ["x", 1]},  # 脏依赖被过滤
        ],
        "risks": ["有效", 123, {"非": "字符串"}],    # 只保留 str/int/float
    }
    plan = Plan.from_dict(d)
    assert plan.goal == "g"
    assert len(plan.steps) == 3                     # 非 dict 项被跳过
    assert all(isinstance(s.id, int) for s in plan.steps)
    assert plan.steps[2].depends_on == [1]          # "x" 被过滤
    assert 123 in plan.risks or "123" in plan.risks
    assert all(isinstance(r, str) for r in plan.risks)


def test_plan_from_dict_empty():
    plan = Plan.from_dict({})
    assert plan.goal == ""
    assert plan.steps == []
    assert plan.risks == []


# ---- next_pending_step ----

def test_next_pending_step_returns_first_ready():
    plan = _sample_plan()
    # 初始只有步骤 1（无依赖）可执行
    nxt = plan.next_pending_step()
    assert nxt is not None
    assert nxt.id == 1


def test_next_pending_step_respects_dependencies():
    plan = _sample_plan()
    plan.steps[0].status = "done"      # 步骤1完成
    nxt = plan.next_pending_step()
    assert nxt.id == 2                 # 步骤2依赖1，现可执行
    # 步骤3依赖1和2，2尚未完成，不应被选
    assert nxt.id != 3


def test_next_pending_step_skips_blocked():
    plan = _sample_plan()
    # 步骤1未完成，步骤2/3被阻塞；将步骤1设为 in_progress（非 done）
    plan.steps[0].status = "in_progress"
    nxt = plan.next_pending_step()
    # 步骤1非 pending，步骤2/3依赖未满足 -> 无可执行步骤
    assert nxt is None


def test_next_pending_step_none_when_all_done():
    plan = _sample_plan()
    for s in plan.steps:
        s.status = "done"
    assert plan.next_pending_step() is None


# ---- is_complete ----

def test_is_complete_all_done():
    plan = _sample_plan()
    for s in plan.steps:
        s.status = "done"
    assert plan.is_complete() is True


def test_is_complete_with_failed():
    plan = _sample_plan()
    plan.steps[0].status = "done"
    plan.steps[1].status = "failed"
    plan.steps[2].status = "done"
    assert plan.is_complete() is True  # failed 也算收尾


def test_is_complete_false_when_pending():
    plan = _sample_plan()
    plan.steps[0].status = "done"
    assert plan.is_complete() is False


def test_is_complete_false_when_in_progress():
    plan = _sample_plan()
    for s in plan.steps:
        s.status = "in_progress"
    assert plan.is_complete() is False


def test_is_complete_empty_steps():
    assert Plan(goal="g").is_complete() is False


# ---- PlanningManager 持久化 ----

def test_planning_manager_creates_dir(tmp_path):
    progress = tmp_path / "nested" / "progress"
    PlanningManager(progress)
    assert progress.is_dir()


def test_planning_manager_save_and_load(tmp_path):
    mgr = PlanningManager(tmp_path / "progress")
    plan = _sample_plan()
    path = mgr.save_plan("sess-1", plan)
    assert path.is_file()
    loaded = mgr.load_plan("sess-1")
    assert loaded is not None
    assert loaded.to_dict() == plan.to_dict()


def test_planning_manager_save_sets_created_at(tmp_path):
    mgr = PlanningManager(tmp_path / "progress")
    plan = Plan(goal="g", steps=[PlanStep(id=1, action="a")])
    assert plan.created_at == ""
    mgr.save_plan("sess", plan)
    assert plan.created_at != ""       # save 时自动填充
    loaded = mgr.load_plan("sess")
    assert loaded.created_at == plan.created_at


def test_planning_manager_load_missing_returns_none(tmp_path):
    mgr = PlanningManager(tmp_path / "progress")
    assert mgr.load_plan("no-such-session") is None


def test_planning_manager_load_corrupt_returns_none(tmp_path):
    mgr = PlanningManager(tmp_path / "progress")
    mgr.save_plan("sess", _sample_plan())
    # 手动写坏进度文件
    bad = tmp_path / "progress" / "sess.json"
    bad.write_text("{ 不是合法 JSON", encoding="utf-8")
    assert mgr.load_plan("sess") is None


def test_planning_manager_session_id_sanitized(tmp_path):
    """session_id 含路径危险字符时被清洗，不越界。"""
    mgr = PlanningManager(tmp_path / "progress")
    mgr.save_plan("../../evil", _sample_plan())
    # 清洗后文件仍落在 progress 目录内
    files = list((tmp_path / "progress").glob("*.json"))
    assert len(files) == 1
    assert files[0].parent == tmp_path / "progress"


def test_planning_manager_update_step(tmp_path):
    mgr = PlanningManager(tmp_path / "progress")
    mgr.save_plan("sess", _sample_plan())
    mgr.update_step("sess", 1, "done", output="步骤1产物")
    loaded = mgr.load_plan("sess")
    step1 = next(s for s in loaded.steps if s.id == 1)
    assert step1.status == "done"
    assert step1.output == "步骤1产物"
    # 其它步骤不受影响
    step2 = next(s for s in loaded.steps if s.id == 2)
    assert step2.status == "pending"


def test_planning_manager_update_step_missing_session_noop(tmp_path):
    mgr = PlanningManager(tmp_path / "progress")
    # 不存在的 session：update 应静默忽略，不报错
    mgr.update_step("ghost", 1, "done")


def test_planning_manager_update_step_missing_step_noop(tmp_path):
    mgr = PlanningManager(tmp_path / "progress")
    mgr.save_plan("sess", _sample_plan())
    mgr.update_step("sess", 999, "done")   # 不存在的步骤 id
    loaded = mgr.load_plan("sess")
    assert all(s.status == "pending" for s in loaded.steps)


def test_planning_manager_progress_summary(tmp_path):
    mgr = PlanningManager(tmp_path / "progress")
    mgr.save_plan("sess", _sample_plan())
    mgr.update_step("sess", 1, "done")
    summary = mgr.get_progress_summary("sess")
    assert "构建一个网页" in summary
    assert "1/3 完成" in summary


def test_planning_manager_progress_summary_no_plan(tmp_path):
    mgr = PlanningManager(tmp_path / "progress")
    assert mgr.get_progress_summary("ghost") == "（无进行中的计划）"


# ---- parse_plan_from_output ----

def test_parse_plan_pure_json():
    raw = json.dumps({"goal": "g", "steps": [{"id": 1, "action": "a",
                                              "depends_on": []}],
                      "risks": ["r"]}, ensure_ascii=False)
    plan = parse_plan_from_output(raw)
    assert plan is not None
    assert plan.goal == "g"
    assert len(plan.steps) == 1


def test_parse_plan_fenced_json():
    inner = json.dumps({"goal": "围栏目标",
                        "steps": [{"id": 1, "action": "a", "depends_on": []}]},
                       ensure_ascii=False)
    raw = f"```json\n{inner}\n```"
    plan = parse_plan_from_output(raw)
    assert plan is not None
    assert plan.goal == "围栏目标"


def test_parse_plan_fenced_no_language():
    inner = json.dumps({"goal": "g", "steps": [{"id": 1, "action": "a"}]},
                       ensure_ascii=False)
    raw = f"```\n{inner}\n```"
    plan = parse_plan_from_output(raw)
    assert plan is not None
    assert plan.goal == "g"


def test_parse_plan_mixed_text():
    inner = json.dumps({"goal": "混杂目标",
                        "steps": [{"id": 1, "action": "第一步", "depends_on": []},
                                  {"id": 2, "action": "第二步", "depends_on": [1]}]},
                       ensure_ascii=False)
    raw = f"好的，这是我的计划：\n{inner}\n以上就是完整计划，请审阅。"
    plan = parse_plan_from_output(raw)
    assert plan is not None
    assert plan.goal == "混杂目标"
    assert len(plan.steps) == 2
    assert plan.steps[1].depends_on == [1]


def test_parse_plan_no_json_returns_none():
    assert parse_plan_from_output("这里完全没有任何结构化内容。") is None


def test_parse_plan_empty_returns_none():
    assert parse_plan_from_output("") is None
    assert parse_plan_from_output("   \n  ") is None


def test_parse_plan_json_without_steps_or_goal_returns_none():
    """有 JSON 但既无 steps 也无 goal：视为非计划，返回 None。"""
    assert parse_plan_from_output('{"foo": "bar"}') is None


def test_parse_plan_goal_only():
    plan = parse_plan_from_output('{"goal": "只有目标"}')
    assert plan is not None
    assert plan.goal == "只有目标"


# ---- build_planning_prompt / build_reflection_prompt ----

def test_build_planning_prompt_includes_original():
    original = "帮我做一个数据看板"
    prompt = build_planning_prompt(original)
    assert original in prompt
    # 规划指令前缀也被注入
    assert "结构化执行计划" in prompt or "JSON" in prompt


def test_build_reflection_prompt_includes_output():
    task_output = "任务完成，产出了 index.html"
    prompt = build_reflection_prompt(task_output)
    assert task_output in prompt
    assert "评估" in prompt  # 反思指令
