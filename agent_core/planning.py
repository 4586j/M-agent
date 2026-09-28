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
    # 产生本计划的原始任务 prompt：断点续跑按 prompt 匹配未完成计划（旧进度
    # 文件缺该字段时 from_dict 兜底为空串，匹配自然失败，不影响旧数据）
    source_prompt: str = ""

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
            "source_prompt": self.source_prompt,
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
            source_prompt=str(d.get("source_prompt", "")),
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

    def next_pending_steps(self) -> list[PlanStep]:
        """[预留] 返回所有可并行执行的 pending 步骤（依赖均已 done）。

        用于 plan_parallel_steps=True 时批量取步骤；返回的列表按原始
        顺序排列（id 升序），调用方需自行检查步骤间是否有相互依赖
        （并行步骤之间不应有依赖关系）。当前 kernel 步骤循环恒单步执行，
        此接口仅测试引用——预留待并行步骤功能接入。
        """
        done_ids = {s.id for s in self.steps if s.status == "done"}
        return [
            s for s in self.steps
            if s.status == "pending"
            and all(dep in done_ids for dep in s.depends_on)
        ]

    def is_complete(self) -> bool:
        """[预留] 所有步骤都非 pending/in_progress 即视为完成（含 failed 收尾）。

        仅测试引用；kernel 用 all_done() 判定（要求全部 done 才成功），
        is_complete 兼容 failed 收尾语义，预留供外部判定使用。
        """
        if not self.steps:
            return False
        return all(s.status in ("done", "failed") for s in self.steps)

    def all_done(self) -> bool:
        """所有步骤均为 done（无 failed/pending/in_progress）才算真正完成。

        与 is_complete() 区分：is_complete 表示「执行循环已停」（步骤都落终态，
        含 failed 收尾）；all_done 表示「每个必需步骤都成功完成」。收尾判定任务
        成功与否必须用 all_done——绝不把含 failed 步骤的计划谎报为成功。
        """
        if not self.steps:
            return False
        return all(s.status == "done" for s in self.steps)

    def max_step_id(self) -> int:
        """当前步骤 id 最大值（无步骤返回 0）：重规划接续分配新 id 用。"""
        return max((s.id for s in self.steps), default=0)

    def done_summaries(self, max_chars: int = 200) -> list[str]:
        """已完成步骤的产出摘要列表（供后续步骤 prompt 注入上下文）。"""
        lines = []
        for s in self.steps:
            if s.status != "done":
                continue
            out = (s.output or "").strip()
            lines.append(f"步骤 {s.id}（{s.action}）：{out[:max_chars]}"
                         + ("…" if len(out) > max_chars else ""))
        return lines


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


# 计划 created_at 的时间格式（save_plan 用 time.strftime 写入的本地时间）
_PLAN_TIME_FORMAT = "%Y-%m-%d %H:%M:%S"

# find_resumable 缓存哨兵：区分「尚未扫描」与「扫描结果为无可用计划（None）」
_SCAN_NOT_CACHED = object()


def _plan_age_hours(created_at: str) -> float | None:
    """按 created_at 计算计划年龄（小时）；缺失或格式非法返回 None。

    返回 None 时调用方按「无法判定年龄」处理（断点续跑场景视同超龄，保守不续）。
    """
    if not created_at:
        return None
    try:
        ts = time.mktime(time.strptime(created_at, _PLAN_TIME_FORMAT))
    except (ValueError, OverflowError, OSError):
        return None
    return max(0.0, (time.time() - ts) / 3600.0)


class PlanningManager:
    """管理规划生命周期：计划生成、进度追踪、持久化。

    进度文件默认落在 ``project_root / ".orch_progress"`` 下，每个 session 一个 JSON。
    """

    def __init__(self, progress_dir: str | Path):
        self.progress_dir = Path(progress_dir)
        self.progress_dir.mkdir(parents=True, exist_ok=True)
        # find_resumable 扫描短路缓存：计划文件 stat 级无变化且入参一致时
        # 复用上次结果，避免每 run/每入口重复 glob + 全量 json 解析。
        self._scan_sig: int = -1
        self._scan_prompt: str = ""
        self._scan_max_age: float | None = None
        self._scan_result: Any = _SCAN_NOT_CACHED

    def _scan_signature(self) -> int:
        """进度目录 stat 级指纹：全部 .json 的 (mtime_ns, size) 累和。

        任何计划文件增删改都会改变指纹——比全量 json 解析便宜得多（只 stat）；
        目录不可达返回 0 兜底（触发重扫）。
        """
        total = 0
        try:
            for p in self.progress_dir.glob("*.json"):
                st = p.stat()
                total += st.st_mtime_ns + st.st_size
        except OSError:
            return 0
        return total

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

    def find_resumable(self, prompt: str, max_age_hours: float | None = None
                       ) -> tuple[str, Plan] | None:
        """断点续跑：按原始 prompt 匹配未完成计划，返回 (session_id, plan)。

        扫描进度目录下全部计划，条件：source_prompt 与 prompt 一致且存在
        pending/in_progress 步骤（未完成）。命中多个时取 created_at 最新的。

        max_age_hours（可选）：计划年龄上限（小时，按 created_at 计算）——
        超龄不续跑（返回 None，让调用方开新计划），避免定时任务/评测用同
        prompt 错配陈旧计划；created_at 缺失或无法解析时同样视同超龄。
        None 或 <=0 表示不限年龄（保持旧行为）。
        """
        if not prompt or not self.progress_dir.is_dir():
            return None
        sig = self._scan_signature()
        # stat 级无变化 + 入参一致 → 复用上次扫描结果；缓存 Plan 返回深拷贝，
        # 防调用方（断点续跑重置步骤）在返回对象上的修改污染缓存。
        if (sig == self._scan_sig and prompt == self._scan_prompt
                and max_age_hours == self._scan_max_age
                and self._scan_result is not _SCAN_NOT_CACHED):
            if self._scan_result is None:
                return None
            sid, plan = self._scan_result
            return sid, Plan.from_dict(plan.to_dict())
        self._scan_sig = sig
        self._scan_prompt = prompt
        self._scan_max_age = max_age_hours

        best: tuple[str, Plan] | None = None
        for path in self.progress_dir.glob("*.json"):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                plan = Plan.from_dict(data)
            except Exception:  # noqa: BLE001 - 损坏文件跳过
                continue
            if plan.source_prompt != prompt:
                continue
            if all(s.status in ("done", "failed") for s in plan.steps):
                continue
            if max_age_hours is not None and max_age_hours > 0:
                age = _plan_age_hours(plan.created_at)
                if age is None or age > max_age_hours:
                    continue
            sid = path.stem
            if best is None or plan.created_at > best[1].created_at:
                best = (sid, plan)
        self._scan_result = best
        return best

    def update_step(self, session_id: str, step_id: int, status: str,
                    output: str = "") -> None:
        """[预留] 更新指定步骤的状态/产出并回写进度文件。步骤不存在则忽略。

        仅测试引用；kernel 走 save_plan 全量落盘（含 _persist_plan 合并），
        单步更新接口预留供外部渐进式持久化使用。
        """
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


STEP_PROMPT_PREFIX = """当前任务采用 Plan-Execute 模式推进：整体计划已拆分为多个步骤，
本次只需要完成下面这一个步骤，不要越权执行其它步骤。
"""


def build_step_prompt(original_prompt: str, plan: Plan, step: PlanStep,
                      failure_hint: str = "", context_memory: str = "") -> str:
    """构造单步骤执行提示：任务原文 + 当前步骤 + 已完成步骤摘要 + 失败教训。

    - original_prompt：用户的原始任务 prompt（保持目标完整可见）；
    - context_memory：调用方传入的上下文记忆，与规划轮同源注入——保证首跑
      与断点续跑（跳过规划轮）的步骤轮输入一致；空串不注入，行为不变；
    - 已完成步骤摘要：来自 plan.done_summaries()（依赖产物提示）；
    - failure_hint：上一次该步骤失败的原因/输出（重试时注入，避免重蹈覆辙）。
    """
    lines = [STEP_PROMPT_PREFIX]
    if context_memory:
        lines.append(f"\n[上下文记忆]\n{context_memory}")
    lines.append(f"\n[任务]\n{original_prompt}")
    lines.append("")
    lines.append(f"[当前步骤 {step.id}]\n{step.action}")
    if step.depends_on:
        lines.append(f"（依赖步骤：{step.depends_on}，其产出见下方已完成步骤摘要）")
    summaries = plan.done_summaries()
    if summaries:
        lines.append("\n[已完成步骤摘要（供参考的依赖产物）]")
        lines.extend(f"- {s}" for s in summaries)
    if failure_hint:
        lines.append("\n[上次执行该步骤失败，教训如下，请调整方案避免同样的问题]")
        lines.append(failure_hint[:1000])
    return "\n".join(lines)


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


FAILURE_REFLECTION_PROMPT = """上述任务执行失败了。请作为事后复盘分析：
1. 失败的根本原因是什么？
2. 哪些做法或假设导致了失败？
3. 下次重试该任务时，给出具体的改进建议（避免重蹈覆辙）。

以简洁的要点输出，不超过 200 字。
"""


def build_failure_reflection_prompt(error_output: str) -> str:
    """构造失败反思提示：分析失败原因与改进建议（重试时作为教训注入）。"""
    return f"{error_output}\n\n{FAILURE_REFLECTION_PROMPT}"
