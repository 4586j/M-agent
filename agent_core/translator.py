"""结果翻译服务：在「独立上下文」里把任务最终产出翻译成目标语言（默认中文）。

设计红线（与 advisor.py 一致，不可协商）：翻译绝不污染任务上下文——
- 独立会话：run_oneshot 默认 resume=False（全新 SDK 会话，绝不复用任务会话）；
- 独立 cwd：每次调用在 <translate_work_dir>/tr-<uuid8> 下运行，绝不复用任务 cwd；
- 不读经验：context_memory="" 且从不 enrich_experience；
- 不写经验：run_oneshot(record_experience=False)；
- 不回写记忆：本模块从不 write_result_note / 不写 memory_store；
- 隔离配置再关一次自动翻译（result_translate_enabled=False）+ 关规划/经验沉淀，
  从根本上杜绝「翻译触发翻译」的递归。

语义：思考过程（流式 token / 中间推理）保持原语言（通常英文），仅对最终结果文本
做一次旁路翻译。模型优先级：translate_model > advisor_model > config.model。
翻译是一次性、无副作用的旁路调用，任何失败都优雅降级为「返回原文」，绝不抛异常、
绝不影响主任务结果。
"""
from __future__ import annotations

import dataclasses
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from agent_core.config import AgentConfig
from agent_core.kernel import AgentKernel
from agent_core.persona import Persona, load_persona_by_name

# 兜底人设（找不到 translator.md 时）：只读工具集，硬约束不落地副作用。
_FALLBACK_TOOLS = ["Read"]

# 目标语言代码 -> 自然语言名（用于提示词）。未列出的代码原样传给模型。
_LANG_NAMES = {
    "zh": "简体中文", "zh-CN": "简体中文", "zh-Hans": "简体中文",
    "zh-TW": "繁體中文", "zh-Hant": "繁體中文",
    "en": "英文", "ja": "日文", "ko": "韩文",
}

# 已是目标语言（中文）时跳过翻译的 CJK 占比阈值：非空白字符中 CJK 达到此比例，
# 视为「结果本已是中文」，跳过再翻译（省成本、避免把中文越翻越乱）。
_CJK_SKIP_THRESHOLD = 0.5


@dataclass
class TranslationResult:
    """翻译结果。字段防御式填充，失败/跳过时优雅降级（不抛异常）。"""

    translated: str = ""        # 译文（跳过时=原文；失败时=""）
    target_lang: str = "zh"     # 目标语言
    changed: bool = False       # 是否发生了实际翻译（跳过/失败为 False）
    skipped: str = ""           # 跳过原因："empty" | "already_target_lang"
    error: str = ""             # 失败信息（优雅降级，不抛异常）
    raw: str = ""               # 模型原始输出（调试用）

    def to_dict(self) -> dict:
        """序列化为可 JSON 化 dict（供 brain_api 响应与前端渲染；不含 raw）。"""
        return {
            "translated": self.translated,
            "target_lang": self.target_lang,
            "changed": self.changed,
            "skipped": self.skipped,
            "error": self.error,
        }


def _cjk_ratio(text: str) -> float:
    """非空白字符中 CJK 统一表意文字的占比（0.0~1.0）。空文本返回 0.0。"""
    chars = [c for c in text if not c.isspace()]
    if not chars:
        return 0.0
    cjk = sum(1 for c in chars if "\u4e00" <= c <= "\u9fff")
    return cjk / len(chars)


def _is_target_lang(text: str, target_lang: str) -> bool:
    """粗略判断文本是否已是目标语言（仅对中文做 CJK 占比启发式判定）。"""
    if not (target_lang or "").lower().startswith("zh"):
        return False
    return _cjk_ratio(text) >= _CJK_SKIP_THRESHOLD


def build_translation_prompt(text: str, target_lang: str = "zh") -> str:
    """构造翻译指令：要求模型「只输出译文本身」，保留结构/代码/路径/专名不译。"""
    lang_name = _LANG_NAMES.get(target_lang, target_lang or "简体中文")
    return "\n".join([
        f"你是专业翻译。请把下面的『待翻译文本』完整、准确地翻译成{lang_name}。",
        "",
        "[硬性要求]",
        "- 只输出译文本身，不要任何解释、前言、结尾、标签或 Markdown 代码围栏；",
        "- 保留原文的段落结构与换行；",
        "- 代码块、命令行、文件路径、URL、变量名、API 名称、专有名词与数字保持原样不译"
        "（代码块内的注释可以翻译）；",
        "- 忠实原意，术语准确，语气自然，不要遗漏也不要臆造内容。",
        "",
        "[待翻译文本]",
        text or "",
    ])


class TranslatorService:
    """独立上下文的结果翻译服务。

    构造参数：
    - config: AgentConfig（据此派生隔离配置与独立 kernel）。
    - persona_dir: 可选，人设目录（缺省用 config.persona_dir），用于加载 translator.md；
      找不到时用只读兜底人设（不强制要求该文件存在）。
    - kernel_factory: 可选，Callable[[AgentConfig], kernel]，默认 AgentKernel；
      测试可注入假 kernel 以断言隔离参数（record_experience/context_memory/cwd）。
    """

    def __init__(self, config: AgentConfig, persona_dir: str | None = None,
                 kernel_factory: Callable[[AgentConfig], Any] | None = None):
        self.config = config
        self.persona_dir = persona_dir or getattr(config, "persona_dir", "") or ""
        self._kernel_factory = kernel_factory or AgentKernel
        self._persona = self._resolve_persona()

    # ---- 人设、模型与隔离工作目录 ----

    def _resolve_persona(self) -> Persona:
        """加载 translator 人设；缺定义文件时给只读兜底人设（翻译无需写/执行工具）。"""
        try:
            return load_persona_by_name("translator", self.persona_dir)
        except (FileNotFoundError, OSError):
            return Persona(
                name="translator",
                description="只读翻译：把任务结果译为目标语言，只输出译文",
                tools=list(_FALLBACK_TOOLS),
                system_prompt=(
                    "你是专业翻译。你只把给定文本翻译成目标语言并输出译文本身，"
                    "绝不修改文件、绝不执行任何副作用操作、绝不输出译文以外的内容。"
                ),
            )

    def _translate_model(self) -> str | None:
        """翻译模型优先级：translate_model > advisor_model > config.model。"""
        return (getattr(self.config, "translate_model", "") or ""
                or getattr(self.config, "advisor_model", "") or ""
                or getattr(self.config, "model", None))

    def _target_lang(self) -> str:
        return getattr(self.config, "translate_target_lang", "zh") or "zh"

    def _translate_base(self) -> Path:
        """翻译隔离工作根目录：显式 translate_work_dir 优先，否则派生 <work_dir>/.translate_work。"""
        explicit = getattr(self.config, "translate_work_dir", "") or ""
        if explicit:
            return Path(explicit)
        work = (getattr(self.config, "work_dir", "") or ""
                or ((getattr(self.config, "project_root", "") or ".") + "/.agent_work"))
        return Path(work) / ".translate_work"

    def _isolated_config(self) -> AgentConfig:
        """派生一次性隔离配置：不复用任务配置对象，杜绝任何跨任务串味与递归翻译。

        - result_translate_enabled=False：兜底再关一次自动翻译（防「翻译触发翻译」递归）；
        - planning_enabled=False：翻译不需要规划引擎；
        - auto_record_experience=False：兜底再关一次经验沉淀（与 record_experience 双保险）；
        - model：按 translate_model > advisor_model > model 优先级选定。
        config 非 dataclass（测试假对象）时原样返回，不做 replace。
        """
        if not dataclasses.is_dataclass(self.config):
            return self.config
        return dataclasses.replace(
            self.config,
            result_translate_enabled=False,
            planning_enabled=False,
            auto_record_experience=False,
            model=self._translate_model(),
        )

    # ---- 主入口 ----

    def translate(self, text: str, *, target_lang: str | None = None,
                  skip_if_target: bool = True) -> TranslationResult:
        """在独立上下文里把 text 翻译为目标语言，返回 TranslationResult（永不抛异常）。

        - text 为空 -> skipped="empty"（不发起模型调用）；
        - skip_if_target 且文本已是目标语言（中文启发式）-> skipped="already_target_lang"，
          translated=原文（省成本、避免把中文越翻越乱）；
        - 隔离保证见模块 docstring：独立会话 + 独立 cwd + 不读经验 + 不写经验 + 不回写记忆。
        """
        target = target_lang or self._target_lang()
        if not text or not text.strip():
            return TranslationResult(translated="", target_lang=target, skipped="empty")
        if skip_if_target and _is_target_lang(text, target):
            return TranslationResult(translated=text, target_lang=target,
                                     changed=False, skipped="already_target_lang")

        iso_cfg = self._isolated_config()
        kernel = self._kernel_factory(iso_cfg)

        cwd = self._translate_base() / f"tr-{uuid.uuid4().hex[:8]}"
        try:
            cwd.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass  # 目录创建失败不阻断：kernel 会自行处理不可写 cwd

        prompt = build_translation_prompt(text, target)
        try:
            res = kernel.run_oneshot(
                prompt=prompt,
                persona=self._persona,
                cwd=str(cwd),
                context_memory="",          # 不注入任何上下文记忆（读关）
                record_experience=False,    # 不沉淀经验（写关）
            )
        except Exception as e:  # noqa: BLE001 - 翻译失败降级为「返回原文」
            return TranslationResult(translated="", target_lang=target,
                                     error=f"翻译执行失败：{e}")

        raw = str(getattr(res, "output", "") or "")
        cleaned = raw.strip()
        if not cleaned:
            return TranslationResult(translated="", target_lang=target,
                                     error="翻译返回空结果", raw=raw)
        return TranslationResult(translated=cleaned, target_lang=target,
                                 changed=True, raw=raw)

    def translate_text(self, text: str, *, target_lang: str | None = None,
                       skip_if_target: bool = True) -> str:
        """便捷入口：只返回译文字符串；失败/跳过时返回原文（调用方总能拿到可用文本）。

        kernel 自动翻译路径用此方法：无论翻译成功与否，主结果都不受影响。
        """
        r = self.translate(text, target_lang=target_lang, skip_if_target=skip_if_target)
        return r.translated or text
