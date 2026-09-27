"""AI 推荐/解释顾问：在「独立上下文」里给出是否派发子代理、任务拆解与结果解释。

设计红线（与 task-565 计划一致，不可协商）：顾问绝不能污染任务上下文——
- 独立会话：run_oneshot 默认 resume=False，全新 SDK 会话，绝不复用任务会话；
- 独立 cwd：每次调用在 <advisor_work_dir>/adv-<uuid8> 下运行，绝不复用任务 cwd；
- 不读经验：context_memory="" 且从不 enrich_experience；
- 不写经验：run_oneshot(record_experience=False)；
- 不回写记忆：本模块从不 write_result_note / 不写 memory_store；
- 只读输入：顾问只拿到任务文本（explain 模式另附已完成产出），拿不到任务累积的记忆/经验。

顾问是一次性、无副作用的旁路调用：产出结构化 JSON 建议，交由上层（CLI/brain/web）
决定展示或（在 auto_apply 开启时）经既有 /dispatch 管线派发——派发仍过安全关卡。
"""
from __future__ import annotations

import dataclasses
import json
import re
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from agent_core.config import AgentConfig
from agent_core.kernel import AgentKernel
from agent_core.persona import Persona, load_persona_by_name

# 顾问模式：pre_flight=派发前建议 | plan=任务拆解 | explain=结果解释 | all=并集
ADVISOR_MODES = ("pre_flight", "plan", "explain", "all")

# 兜底人设（找不到 advisor.md 时）：只读工具集，硬约束不落地副作用。
_FALLBACK_TOOLS = ["Read", "Grep", "Glob"]


@dataclass
class AdvisorResult:
    """顾问结构化建议。字段全部防御式填充，解析失败时优雅降级（不抛异常）。"""

    should_dispatch: bool = False
    roles: list[str] = field(default_factory=list)
    plan: list[dict] = field(default_factory=list)
    explanation: str = ""
    risks: list[str] = field(default_factory=list)
    raw: str = ""
    mode: str = "all"

    def to_dict(self) -> dict:
        """序列化为可 JSON 化 dict（供 brain_api 响应与前端渲染）。"""
        return {
            "should_dispatch": self.should_dispatch,
            "roles": list(self.roles),
            "plan": list(self.plan),
            "explanation": self.explanation,
            "risks": list(self.risks),
            "mode": self.mode,
        }


def _extract_json(text: str) -> dict | None:
    """从模型输出里尽力抽取第一个 JSON 对象。

    依次尝试：去 ```json 围栏后整体解析 -> 首个 '{' 到末个 '}' 的子串解析。
    全失败返回 None（由调用方降级）。只接受 dict。
    """
    if not text:
        return None
    cleaned = re.sub(r"```(?:json)?", "", text).replace("```", "").strip()
    for candidate in (cleaned, text.strip()):
        try:
            obj = json.loads(candidate)
            if isinstance(obj, dict):
                return obj
        except (ValueError, TypeError):
            pass
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start != -1 and end > start:
        try:
            obj = json.loads(cleaned[start:end + 1])
            if isinstance(obj, dict):
                return obj
        except (ValueError, TypeError):
            pass
    return None


class AdvisorService:
    """独立上下文的推荐/解释顾问。

    构造参数：
    - config: AgentConfig（顾问据此派生隔离配置与独立 kernel）。
    - persona_dir: 可选，人设目录（缺省用 config.persona_dir），用于加载 advisor.md。
    - kernel_factory: 可选，Callable[[AgentConfig], kernel]，默认 AgentKernel；
      测试可注入假 kernel 以断言隔离参数（record_experience/context_memory/cwd）。
    """

    def __init__(self, config: AgentConfig, persona_dir: str | None = None,
                 kernel_factory: Callable[[AgentConfig], Any] | None = None):
        self.config = config
        self.persona_dir = persona_dir or getattr(config, "persona_dir", "") or ""
        self._kernel_factory = kernel_factory or AgentKernel
        self._persona = self._resolve_persona()

    # ---- 人设与隔离工作目录 ----

    def _resolve_persona(self) -> Persona:
        """加载 advisor 人设；缺定义文件时给只读兜底人设。"""
        try:
            return load_persona_by_name("advisor", self.persona_dir)
        except (FileNotFoundError, OSError):
            return Persona(
                name="advisor",
                description="只读顾问：推荐是否派发子代理、拆解任务、解释结果",
                tools=list(_FALLBACK_TOOLS),
                system_prompt=(
                    "你是只读的编排顾问。你只分析任务、给出建议与解释，"
                    "绝不修改文件、绝不执行副作用操作。严格按要求的 JSON 结构输出。"
                ),
            )

    def _advisor_base(self) -> Path:
        """顾问隔离工作根目录：显式 advisor_work_dir 优先，否则派生 <work_dir>/.advisor_work。"""
        explicit = getattr(self.config, "advisor_work_dir", "") or ""
        if explicit:
            return Path(explicit)
        work = (getattr(self.config, "work_dir", "") or ""
                or ((getattr(self.config, "project_root", "") or ".") + "/.agent_work"))
        return Path(work) / ".advisor_work"

    def _isolated_config(self, use_plan_engine: bool) -> AgentConfig:
        """派生一次性隔离配置：不复用任务配置对象，避免任何跨任务串味。

        - planning_enabled：仅 plan/all 且开启 PlanEngine 时为 True（复用既有拆解引擎）；
        - auto_record_experience=False：兜底再关一次经验沉淀（与 record_experience 双保险）；
        - result_translate_enabled=False：顾问产出是严格 JSON，绝不能被自动翻译破坏；
        - model：advisor_model 优先，否则跟随 config.model。
        config 非 dataclass（测试假对象）时原样返回，不做 replace。
        """
        if not dataclasses.is_dataclass(self.config):
            return self.config
        advisor_model = getattr(self.config, "advisor_model", "") or ""
        return dataclasses.replace(
            self.config,
            planning_enabled=bool(use_plan_engine),
            auto_record_experience=False,
            result_translate_enabled=False,
            model=(advisor_model or self.config.model),
        )

    # ---- 提示构造 ----

    def _build_prompt(self, task: str, mode: str,
                      finished_output: str | None) -> str:
        """按模式拼装顾问指令。要求模型「只输出一个严格 JSON 对象」。"""
        asks: list[str] = []
        if mode in ("pre_flight", "all"):
            asks.append(
                "1) 判断该任务是否值得派发子代理（should_dispatch, 布尔），"
                "并给出建议的角色列表（roles，如 [\"executor\"] 或 [\"executor\",\"security-guard\"]），"
                "以及判断理由。")
        if mode in ("plan", "all"):
            asks.append(
                "2) 把任务拆解为有序步骤 plan，每步形如 "
                "{\"id\":int,\"action\":str,\"depends_on\":[int]}。")
        if mode in ("explain", "all"):
            asks.append(
                "3) 用通俗语言解释该任务的目标/关键难点（explanation），"
                "并列出潜在风险 risks（字符串数组）。")
        if mode == "explain" and finished_output:
            asks.append(
                "3) 针对下面给出的『已完成产出』做解释与总结（explanation），"
                "并指出其中的风险或不足（risks）。")

        schema = (
            '{"should_dispatch": <bool>, "roles": [<str>, ...], '
            '"plan": [{"id": <int>, "action": "<str>", "depends_on": [<int>, ...]}], '
            '"explanation": "<str>", "risks": ["<str>", ...]}'
        )
        parts = [
            "你是只读编排顾问。请仅依据下面提供的信息进行分析，"
            "不要修改任何文件、不要执行任何副作用操作。",
            "",
            "[分析要求]",
            *asks,
            "",
            "[输出格式]",
            f"只输出一个严格合法的 JSON 对象，不要额外文字、不要 Markdown 围栏。结构：{schema}",
            "缺失的字段用其类型的空值（false / [] / \"\"）填充。",
            "",
            "[任务]",
            task or "",
        ]
        if mode in ("explain",) and finished_output:
            parts += ["", "[已完成产出]", finished_output[:8000]]
        return "\n".join(parts)

    # ---- 主入口 ----

    def recommend(self, task: str, *, mode: str = "all",
                  finished_output: str | None = None,
                  enable_plan_engine: bool | None = None) -> AdvisorResult:
        """在独立上下文里跑一次顾问，返回结构化建议（永不抛异常）。

        隔离保证见模块 docstring：独立会话 + 独立 cwd + 不读经验 + 不写经验 + 不回写记忆。
        """
        if mode not in ADVISOR_MODES:
            mode = "all"
        use_plan = (mode in ("plan", "all")) and bool(
            enable_plan_engine if enable_plan_engine is not None
            else getattr(self.config, "advisor_use_plan_engine", False))

        iso_cfg = self._isolated_config(use_plan)
        kernel = self._kernel_factory(iso_cfg)

        cwd = self._advisor_base() / f"adv-{uuid.uuid4().hex[:8]}"
        try:
            cwd.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass  # 目录创建失败不阻断：kernel 会自行处理不可写 cwd

        prompt = self._build_prompt(task, mode, finished_output)
        try:
            res = kernel.run_oneshot(
                prompt=prompt,
                persona=self._persona,
                cwd=str(cwd),
                context_memory="",          # 不注入任何上下文记忆（读关）
                record_experience=False,    # 不沉淀经验（写关）
            )
        except Exception as e:  # noqa: BLE001 - 顾问失败降级为「不建议派发」
            return AdvisorResult(should_dispatch=False, roles=[], plan=[],
                                 explanation=f"顾问执行失败：{e}", risks=[],
                                 raw="", mode=mode)

        raw = str(getattr(res, "output", "") or "")
        result = self._parse(raw, mode)
        # plan/all 模式：若内核 PlanEngine 在 meta 里产出了结构化计划，优先并入
        meta = getattr(res, "meta", None) or {}
        if mode in ("plan", "all") and not result.plan:
            meta_plan = meta.get("plan")
            if isinstance(meta_plan, dict) and isinstance(meta_plan.get("steps"), list):
                result.plan = [
                    {"id": s.get("id"), "action": s.get("action", ""),
                     "depends_on": s.get("depends_on", [])}
                    for s in meta_plan["steps"] if isinstance(s, dict)
                ]
        return result

    def _parse(self, raw: str, mode: str) -> AdvisorResult:
        """把模型输出解析为 AdvisorResult；解析失败优雅降级（保留 raw）。"""
        data = _extract_json(raw)
        if not isinstance(data, dict):
            return AdvisorResult(should_dispatch=False, roles=[], plan=[],
                                 explanation=raw.strip(), risks=[], raw=raw, mode=mode)

        roles_raw = data.get("roles")
        roles = [str(r) for r in roles_raw if str(r).strip()] \
            if isinstance(roles_raw, list) else []

        plan_raw = data.get("plan")
        plan: list[dict] = []
        if isinstance(plan_raw, list):
            for step in plan_raw:
                if isinstance(step, dict):
                    plan.append({
                        "id": step.get("id"),
                        "action": str(step.get("action", "") or ""),
                        "depends_on": step.get("depends_on", [])
                        if isinstance(step.get("depends_on"), list) else [],
                    })

        risks_raw = data.get("risks")
        risks = [str(x) for x in risks_raw if str(x).strip()] \
            if isinstance(risks_raw, list) else []

        return AdvisorResult(
            should_dispatch=bool(data.get("should_dispatch", False)),
            roles=roles,
            plan=plan,
            explanation=str(data.get("explanation", "") or ""),
            risks=risks,
            raw=raw,
            mode=mode,
        )
