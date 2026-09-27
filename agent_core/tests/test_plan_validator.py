"""测试 agent_core.plan_validator：DAG 环/可达性/依赖存在性/重复 id 检测。

Slice 3 新增「重复步骤 id」检测——重规划合并（done_steps + 新步骤）可能产生
重复 id，使依赖引用歧义、拓扑检测失真，必须判为结构性无效。
"""
from agent_core.plan_validator import try_fix_plan, validate_plan
from agent_core.planning import Plan, PlanStep


def _plan(steps):
    """steps: (id, depends_on) 序列 → Plan（action 用占位）。"""
    return Plan(goal="g", steps=[
        PlanStep(id=i, action=f"步骤{i}", depends_on=list(d)) for i, d in steps])


def test_valid_linear_plan():
    r = validate_plan(_plan([(1, []), (2, [1]), (3, [1, 2])]))
    assert r.is_valid is True
    assert r.issues == []


def test_empty_plan_invalid():
    r = validate_plan(Plan(goal="g"))
    assert r.is_valid is False
    assert any("无步骤" in i for i in r.issues)


def test_duplicate_step_ids_invalid():
    """重复 id（合并/重规划常见）→ 结构性无效，并给出接续 id 的建议。"""
    r = validate_plan(_plan([(1, []), (2, [1]), (1, []), (2, [1])]))
    assert r.is_valid is False
    assert any("重复的步骤 id" in i for i in r.issues)
    assert any("唯一 id" in s for s in r.suggestions)


def test_duplicate_ids_from_replan_merge_shape():
    """模拟重规划合并：已完成步骤 id 与新步骤 id 冲突。"""
    done = [PlanStep(id=1, action="已完成", status="done")]
    new = [PlanStep(id=1, action="新步骤A"), PlanStep(id=2, action="新步骤B",
                                                      depends_on=[1])]
    merged = Plan(goal="g", steps=done + new)
    r = validate_plan(merged)
    assert r.is_valid is False
    assert any("重复的步骤 id" in i for i in r.issues)


def test_cycle_invalid():
    r = validate_plan(_plan([(1, [2]), (2, [1])]))
    assert r.is_valid is False
    assert any("环" in i for i in r.issues)


def test_missing_dependency_flagged():
    r = validate_plan(_plan([(1, []), (2, [99])]))
    assert r.is_valid is False
    assert any("依赖不存在的步骤 99" in i for i in r.issues)


def test_granularity_warnings():
    single = validate_plan(_plan([(1, [])]))
    assert any("过少" in w for w in single.warnings)
    many = validate_plan(_plan([(i, []) for i in range(1, 12)]))
    assert any("过多" in w for w in many.warnings)


def test_try_fix_breaks_cycle():
    plan = _plan([(1, [2]), (2, [1])])
    assert validate_plan(plan).is_valid is False
    assert try_fix_plan(plan) is True
    assert validate_plan(plan).is_valid is True


def test_missing_dep_on_reachable_step_not_autofixable():
    """缺失依赖不会使步骤不可达（被跳过），故 try_fix 无从修复：
    返回 False 且计划仍无效——调用方据此拒绝采用（不静默执行断链图）。"""
    plan = _plan([(1, []), (2, [1, 99])])
    assert validate_plan(plan).is_valid is False
    assert try_fix_plan(plan) is False
    assert validate_plan(plan).is_valid is False
