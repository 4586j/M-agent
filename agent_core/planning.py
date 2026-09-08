"""规划增强：Plan-then-Execute 模式 + 进度持久化 + 自我反思。

借鉴 smolagents 的 PlanningStep 与 Anthropic 长任务「进度文件」模式：
- 先让 agent 产出结构化计划（JSON），再按步骤执行；
- 计划与步骤状态持久化到 progress_dir，跨轮次/重启可恢复；
- 任务收尾时用反思提示让 agent 自评产出质量。
"""
import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class PlanStep:
    """计划中的单个步骤。"""

    id: int
    action: str
    depends_on: list[int] = field(default_factory=list)
    status: str = "pending"  # pending | in_progress | done | failed
    output: str = ""


@dataclass
class Plan:
    """结构化执行计划。"""

    goal: str
    steps: list[PlanStep] = field(default_factory=list)
    risks: list[str] = field(default_factory=list)
    created_at: str = ""

    def to_dict(self) -> dict:
        """序列化为可 JSON 化的 dict。"""
        return {
            "goal": self.goal,
            "steps": [
                {
                    "id": s.id,
                    "action": s.action,
                    "depends_on": list(s.depends_on),
                    "status": s.status,
                    "output": s.output,
                }
                for s in self.steps
            ],
            "risks": list(self.risks),
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Plan":
        """从 dict 反序列化。对缺字段/脏数据做防御式解析。"""
        steps: list[PlanStep] = []
        for raw in d.get("steps") or []:
            if not isinstance(raw, dict):
                continue
            try:
                step_id = int(raw.get("id", len(steps) + 1))
            except (TypeError, ValueError):
                step_id = len(steps) + 1
            depends = raw.get("depends_on") or []
            depends_ids: list[int] = []
            for dep in depends if isinstance(depends, list) else []:
                try:
                    depends_ids.append(int(dep))
                except (TypeError, ValueError):
                    continue
            steps.append(PlanStep(
                id=step_id,
                action=str(raw.get("action", "")),
                depends_on=depends_ids,
                status=str(raw.get("status", "pending")),
                output=str(raw.get("output", "")),
            ))
        risks = [str(r) for r in (d.get("risks") or []) if isinstance(r, (str, int, float))]
        return cls(
            goal=str(d.get("goal", "")),
            steps=steps,
            risks=risks,
            created_at=str(d.get("created_at", "")),
        )

    def next_pending_step(self) -> PlanStep | None:
        """返回下一个可执行的 pending 步骤（其依赖均已 done），否则 None。"""
        done_ids = {s.id for s in self.steps if s.status == "done"}
        for s in self.steps:
            if s.status != "pending":
                continue
            if all(dep in done_ids for dep in s.depends_on):
                return s
        return None

    def is_complete(self) -> bool:
        """所有步骤都非 pending/in_progress 即视为完成（含 failed 收尾）。"""
        if not self.steps:
            return False
        return all(s.status in ("done", "failed") for s in self.steps)


PLANNING_PROMPT_PREFIX = """在开始执行之前，请先分析任务并产出一个结构化执行计划。
以 JSON 格式输出，结构如下：
{"goal": "任务总目标", "steps": [{"id": 1, "action": "具体步骤描述", "depends_on": []}], "risks": ["可能的风险"]}

规则：
- 步骤要具体可执行，每步有明确的完成标准
- depends_on 标注步骤间依赖（前置步骤 id 列表）
- risks 列出可能阻碍完成的风险
- 计划产出后，按步骤顺序逐一执行

计划：
"""


REFLECTION_PROMPT = """请评估上述任务产出的质量：
1. 是否完整完成了原始需求？
2. 有无明显错误或遗漏？
3. 代码/文档质量如何？
4. 列出改进建议（如有）

以 JSON 格式输出：{"complete": bool, "quality": "high|medium|low", "issues": [...], "suggestions": [...]}
"""


class PlanningManager:
    """管理规划生命周期：计划生成、进度追踪、持久化。

    进度文件默认落在 ``project_root / ".orch_progress"`` 下，每个 session 一个 JSON。
    """

    def __init__(self, progress_dir: str | Path):
        self.progress_dir = Path(progress_dir)
        self.progress_dir.mkdir(parents=True, exist_ok=True)

    def _path(self, session_id: str) -> Path:
        """session_id -> 进度文件路径。做最小路径安全清洗，避免越界。"""
        safe = "".join(c if c not in '\\/:*?"<>|' else "_" for c in str(session_id))
        return self.progress_dir / f"{safe or 'default'}.json"

    def save_plan(self, session_id: str, plan: Plan) -> Path:
        """持久化计划到进度文件，返回文件路径。"""
        if not plan.created_at:
            plan.created_at = time.strftime("%Y-%m-%d %H:%M:%S")
        path = self._path(session_id)
        path.write_text(
            json.dumps(plan.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return path

    def load_plan(self, session_id: str) -> Plan | None:
        """加载已持久化的计划；不存在或损坏返回 None。"""
        path = self._path(session_id)
        if not path.is_file():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return Plan.from_dict(data)
        except Exception:  # noqa: BLE001 - 损坏的进度文件不拖垮执行
            return None

    def update_step(self, session_id: str, step_id: int, status: str,
                    output: str = "") -> None:
        """更新指定步骤的状态/产出并回写进度文件。步骤不存在则忽略。"""
        plan = self.load_plan(session_id)
        if plan is None:
            return
        for s in plan.steps:
            if s.id == step_id:
                s.status = status
                if output:
                    s.output = output
                break
        self.save_plan(session_id, plan)

    def get_progress_summary(self, session_id: str) -> str:
        """生成人类可读的进度摘要；无计划时返回提示文本。"""
        plan = self.load_plan(session_id)
        if plan is None:
            return "（无进行中的计划）"
        done = sum(1 for s in plan.steps if s.status == "done")
        failed = sum(1 for s in plan.steps if s.status == "failed")
        total = len(plan.steps)
        lines = [
            f"目标：{plan.goal}",
            f"进度：{done}/{total} 完成" + (f"，{failed} 失败" if failed else ""),
        ]
        for s in plan.steps:
            mark = {"done": "✓", "in_progress": "▶", "failed": "✗"}.get(s.status, "·")
            dep = f"（依赖 {s.depends_on}）" if s.depends_on else ""
            lines.append(f"  {mark} [{s.id}] {s.action}{dep} — {s.status}")
        if plan.risks:
            lines.append("风险：" + "；".join(plan.risks))
        return "\n".join(lines)


def build_planning_prompt(original_prompt: str) -> str:
    """在原始 prompt 前注入规划指令，引导 agent 先产出结构化计划。"""
    return f"{PLANNING_PROMPT_PREFIX}\n{original_prompt}"


def parse_plan_from_output(output: str) -> Plan | None:
    """从 agent 输出中解析结构化计划 JSON。

    兼容三种形态：纯 JSON、```json 围栏代码块、混在自然语言中的 JSON 片段。
    解析失败或无 steps 字段返回 None。
    """
    if not output or not output.strip():
        return None
    import re

    text = output.strip()
    candidates: list[str] = []
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if fenced:
        candidates.append(fenced.group(1))
    candidates.append(text)
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        candidates.append(text[start:end + 1])

    for cand in candidates:
        try:
            data = json.loads(cand)
        except Exception:  # noqa: BLE001 - 逐个候选尝试
            continue
        if isinstance(data, dict) and ("steps" in data or "goal" in data):
            plan = Plan.from_dict(data)
            if plan.steps or plan.goal:
                return plan
    return None


def build_reflection_prompt(task_output: str) -> str:
    """构造反思提示，让 agent 评估自身产出质量。"""
    return f"{task_output}\n\n{REFLECTION_PROMPT}"
