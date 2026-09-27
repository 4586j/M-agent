"""计划验证器：对 Plan-Execute 引擎产出的结构化计划做质量检查。

验证规则：
1. DAG 环检测（Kahn 拓扑排序）
2. 步骤可达性（从入度为 0 的步骤 BFS）
3. 依赖存在性（depends_on 引用的 step_id 必须存在）
4. 粒度启发（步骤数 > 10 或 < 2 发 warning）

验证结果用于规划轮解析出计划后、执行前的质量门禁。
"""
import logging
from collections import deque
from dataclasses import dataclass, field

log = logging.getLogger(__name__)


@dataclass
class PlanValidationResult:
    """计划验证结果。"""

    is_valid: bool = True
    issues: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    # 修复建议（如有）：如 "移除步骤3对步骤5的依赖以解除环"
    suggestions: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "is_valid": self.is_valid,
            "issues": self.issues,
            "warnings": self.warnings,
            "suggestions": self.suggestions,
        }


def _detect_cycles(steps: list) -> list[list[int]]:
    """Kahn 算法检测 DAG 中的环。返回环涉及的 step_id 列表（可能多个环）。

    steps: PlanStep 列表，每个有 .id 和 .depends_on 属性。
    """
    # 构建邻接表和入度
    step_ids = {s.id for s in steps}
    in_degree: dict[int, int] = {s.id: 0 for s in steps}
    adj: dict[int, list[int]] = {s.id: [] for s in steps}

    for s in steps:
        for dep in s.depends_on:
            if dep not in step_ids:
                continue  # 依赖不存在，由另一个规则检查
            adj[dep].append(s.id)
            in_degree[s.id] += 1

    # BFS 拓扑排序
    queue = deque(sid for sid, deg in in_degree.items() if deg == 0)
    visited = 0
    while queue:
        node = queue.popleft()
        visited += 1
        for neighbor in adj[node]:
            in_degree[neighbor] -= 1
            if in_degree[neighbor] == 0:
                queue.append(neighbor)

    # 未访问的节点在环中
    if visited < len(step_ids):
        cycle_ids = [sid for sid, deg in in_degree.items() if deg > 0]
        return [cycle_ids]
    return []


def _check_reachability(steps: list) -> list[int]:
    """从入度为 0 的步骤出发 BFS，返回不可达的 step_id 列表。"""
    step_ids = {s.id for s in steps}
    adj: dict[int, list[int]] = {s.id: [] for s in steps}
    in_degree: dict[int, int] = {s.id: 0 for s in steps}

    for s in steps:
        for dep in s.depends_on:
            if dep not in step_ids:
                continue
            adj[dep].append(s.id)
            in_degree[s.id] += 1

    # 从所有入度为 0 的步骤出发
    visited: set[int] = set()
    queue = deque(sid for sid, deg in in_degree.items() if deg == 0)
    while queue:
        node = queue.popleft()
        if node in visited:
            continue
        visited.add(node)
        for neighbor in adj[node]:
            if neighbor not in visited:
                queue.append(neighbor)

    return [sid for sid in step_ids if sid not in visited]


def validate_plan(plan) -> PlanValidationResult:
    """验证一个 Plan 对象的质量。

    plan: agent_core.planning.Plan 实例（需有 .steps 属性，
    每个 step 有 .id 和 .depends_on 属性）。
    """
    result = PlanValidationResult()
    steps = plan.steps
    if not steps:
        result.is_valid = False
        result.issues.append("计划无步骤")
        return result

    step_ids = {s.id for s in steps}

    # 0. 重复步骤 id：合并/重规划（done_steps + 新步骤）可能产生重复 id，
    #    使依赖引用歧义、拓扑检测失真——视为结构性无效，绝不静默执行。
    seen: set = set()
    dup_ids: list = []
    for s in steps:
        if s.id in seen and s.id not in dup_ids:
            dup_ids.append(s.id)
        seen.add(s.id)
    if dup_ids:
        result.is_valid = False
        result.issues.append(f"存在重复的步骤 id：{dup_ids}")
        result.suggestions.append(
            "重规划合并时为新步骤分配唯一 id（接续已完成步骤的最大 id）")

    # 1. 依赖存在性
    for s in steps:
        for dep in s.depends_on:
            if dep not in step_ids:
                result.issues.append(
                    f"步骤 {s.id} 依赖不存在的步骤 {dep}"
                )
                result.suggestions.append(
                    f"移除步骤 {s.id} 对步骤 {dep} 的依赖，或添加步骤 {dep}"
                )

    # 2. DAG 环检测
    cycles = _detect_cycles(steps)
    if cycles:
        result.is_valid = False
        for cycle_ids in cycles:
            result.issues.append(
                f"依赖关系存在环，涉及步骤：{cycle_ids}"
            )
            result.suggestions.append(
                f"检查步骤 {cycle_ids} 的依赖关系，移除环形依赖"
            )

    # 3. 步骤可达性
    unreachable = _check_reachability(steps)
    if unreachable:
        result.issues.append(
            f"步骤 {unreachable} 不可达（依赖链断裂）"
        )
        result.suggestions.append(
            f"检查步骤 {unreachable} 的依赖是否正确连接"
        )

    # 4. 粒度启发
    n = len(steps)
    if n > 10:
        result.warnings.append(
            f"步骤数过多（{n} 步），建议合并相似步骤"
        )
    elif n < 2:
        result.warnings.append(
            f"步骤数过少（{n} 步），可能不需要 Plan-Execute 模式"
        )

    # 有 issue 则标记无效
    if result.issues:
        result.is_valid = False

    return result


def try_fix_plan(plan) -> bool:
    """尝试自动修复计划中的简单问题（移除环形依赖）。

    返回 True 表示做了修复（plan 已就地修改），False 表示无法修复。
    修复策略：
    - 环检测 → 移除环中最后一条依赖边
    - 不可达 → 移除不可达步骤的不可满足依赖
    """
    fixed = False
    step_ids = {s.id for s in plan.steps}

    # 尝试修复环
    cycles = _detect_cycles(plan.steps)
    if cycles:
        for cycle_ids in cycles:
            # 找到环中的步骤，移除其最后一个在环内的依赖
            cycle_set = set(cycle_ids)
            for s in plan.steps:
                if s.id in cycle_set and s.depends_on:
                    # 移除最后一个在环内的依赖
                    in_cycle_deps = [d for d in s.depends_on if d in cycle_set]
                    if in_cycle_deps:
                        dep_to_remove = in_cycle_deps[-1]
                        s.depends_on.remove(dep_to_remove)
                        log.info("修复环形依赖：移除步骤 %d 对步骤 %d 的依赖",
                                 s.id, dep_to_remove)
                        fixed = True
                        break  # 每次只修一条边，重新检测

    # 尝试修复不可达
    unreachable = _check_reachability(plan.steps)
    if unreachable:
        for s in plan.steps:
            if s.id in unreachable and s.depends_on:
                # 移除指向不存在步骤的依赖
                bad_deps = [d for d in s.depends_on if d not in step_ids]
                for d in bad_deps:
                    s.depends_on.remove(d)
                    log.info("修复不可达依赖：移除步骤 %d 对不存在步骤 %d 的依赖",
                             s.id, d)
                    fixed = True

    return fixed
