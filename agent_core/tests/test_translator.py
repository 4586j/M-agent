"""TranslatorService 单元测试：隔离契约、优雅降级、语言启发式、模型优先级。

核心红线：翻译运行于独立上下文，绝不污染任务上下文——
独立 cwd（tr-*）、context_memory=""、record_experience=False、不传 resume（全新会话）、
不回写记忆/经验，且隔离配置把 result_translate_enabled 关成 False（杜绝递归翻译）。
这些断言用假 kernel 捕获 run_oneshot 的入参来验证。
"""
from pathlib import Path
from types import SimpleNamespace

from agent_core.config import load_agent_config
from agent_core.translator import (TranslatorService, TranslationResult,
                                   build_translation_prompt, _cjk_ratio,
                                   _is_target_lang)

_EN_TEXT = "The task finished successfully and produced 42 rows."
_ZH_TEXT = "任务已成功完成，共产出 42 行结果。"


class _FakeKernel:
    """假内核：捕获 run_oneshot 入参与所用配置，返回预设输出。"""

    def __init__(self, config, output="任务成功完成。", raises=None):
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


def _factory(kernels: list, output="任务成功完成。", raises=None):
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
        translate_work_dir=str(tmp_path / "tr"),
    )
    base.update(overrides)
    return load_agent_config(**base)


# ---- 隔离契约（核心要求）----

def test_translate_runs_in_isolated_context(tmp_path):
    cfg = _cfg(tmp_path)
    kernels: list = []
    svc = TranslatorService(cfg, kernel_factory=_factory(kernels))
    res = svc.translate(_EN_TEXT)

    assert isinstance(res, TranslationResult)
    assert res.changed is True
    k = kernels[0]
    call = k.calls[0]
    # 不读上下文记忆、不写经验
    assert call["context_memory"] == ""
    assert call["record_experience"] is False
    # 全新会话：从不传 resume / cancel_event
    assert "resume" not in call["extra"]
    # 独立 cwd：位于 translate_work_dir 下、以 tr- 开头，绝不复用任务目录
    cwd = Path(call["cwd"])
    assert cwd.parent == Path(cfg.translate_work_dir)
    assert cwd.name.startswith("tr-")
    # 隔离配置兜底：关经验沉淀 + 关自动翻译（杜绝递归）+ 关规划
    assert k.config.auto_record_experience is False
    assert k.config.result_translate_enabled is False
    assert k.config.planning_enabled is False


def test_isolated_config_does_not_mutate_shared_config(tmp_path):
    cfg = _cfg(tmp_path, result_translate_enabled=True, planning_enabled=True)
    kernels: list = []
    svc = TranslatorService(cfg, kernel_factory=_factory(kernels))
    svc.translate(_EN_TEXT)
    # 隔离内核拿到的是关闭翻译/规划的副本
    assert kernels[0].config.result_translate_enabled is False
    assert kernels[0].config.planning_enabled is False
    # 共享配置对象未被改动（无跨任务串味）
    assert cfg.result_translate_enabled is True
    assert cfg.planning_enabled is True


# ---- 跳过与降级 ----

def test_empty_text_skips_without_kernel_call(tmp_path):
    cfg = _cfg(tmp_path)
    kernels: list = []
    svc = TranslatorService(cfg, kernel_factory=_factory(kernels))
    res = svc.translate("   ")
    assert res.skipped == "empty"
    assert res.changed is False
    assert kernels == []  # 未发起任何模型调用


def test_already_target_lang_skips(tmp_path):
    """结果本已是中文 -> 跳过再翻译（省成本、避免越翻越乱），不发起模型调用。"""
    cfg = _cfg(tmp_path)
    kernels: list = []
    svc = TranslatorService(cfg, kernel_factory=_factory(kernels))
    res = svc.translate(_ZH_TEXT)
    assert res.skipped == "already_target_lang"
    assert res.changed is False
    assert res.translated == _ZH_TEXT  # 原样返回
    assert kernels == []


def test_skip_can_be_disabled(tmp_path):
    """skip_if_target=False 时即便已是中文也照翻（按需翻译语义）。"""
    cfg = _cfg(tmp_path)
    kernels: list = []
    svc = TranslatorService(cfg, kernel_factory=_factory(kernels, output="已翻译"))
    res = svc.translate(_ZH_TEXT, skip_if_target=False)
    assert res.skipped == ""
    assert res.changed is True
    assert len(kernels) == 1


def test_kernel_exception_degrades_gracefully(tmp_path):
    cfg = _cfg(tmp_path)
    svc = TranslatorService(cfg, kernel_factory=_factory([], raises=RuntimeError("boom")))
    res = svc.translate(_EN_TEXT)
    assert res.changed is False
    assert res.translated == ""
    assert "翻译执行失败" in res.error


def test_empty_model_output_degrades(tmp_path):
    cfg = _cfg(tmp_path)
    svc = TranslatorService(cfg, kernel_factory=_factory([], output="   "))
    res = svc.translate(_EN_TEXT)
    assert res.changed is False
    assert "空" in res.error


def test_translate_text_returns_original_on_failure(tmp_path):
    """便捷入口 translate_text：翻译失败时返回原文（调用方总能拿到可用文本）。"""
    cfg = _cfg(tmp_path)
    svc = TranslatorService(cfg, kernel_factory=_factory([], raises=RuntimeError("x")))
    assert svc.translate_text(_EN_TEXT) == _EN_TEXT


def test_translate_text_returns_translation_on_success(tmp_path):
    cfg = _cfg(tmp_path)
    svc = TranslatorService(cfg, kernel_factory=_factory([], output="  译文内容  "))
    assert svc.translate_text(_EN_TEXT) == "译文内容"  # 去空白


# ---- 模型优先级：translate_model > advisor_model > model ----

def test_model_priority_translate_wins(tmp_path):
    cfg = _cfg(tmp_path, model="opus", advisor_model="sonnet", translate_model="haiku")
    svc = TranslatorService(cfg, kernel_factory=_factory([]))
    assert svc._translate_model() == "haiku"
    assert svc._isolated_config().model == "haiku"


def test_model_priority_falls_back_to_advisor(tmp_path):
    cfg = _cfg(tmp_path, model="opus", advisor_model="sonnet", translate_model="")
    svc = TranslatorService(cfg, kernel_factory=_factory([]))
    assert svc._translate_model() == "sonnet"


def test_model_priority_falls_back_to_model(tmp_path):
    cfg = _cfg(tmp_path, model="opus", advisor_model="", translate_model="")
    svc = TranslatorService(cfg, kernel_factory=_factory([]))
    assert svc._translate_model() == "opus"


# ---- 目标语言 ----

def test_target_lang_default_and_override(tmp_path):
    cfg = _cfg(tmp_path)
    svc = TranslatorService(cfg, kernel_factory=_factory([], output="hi"))
    assert svc._target_lang() == "zh"
    # 显式 target_lang 覆盖配置默认
    res = svc.translate(_EN_TEXT, target_lang="en")
    assert res.target_lang == "en"


def test_non_zh_target_never_skipped(tmp_path):
    """目标语言非中文时，中文文本也应被翻译（不做 CJK 跳过）。"""
    cfg = _cfg(tmp_path)
    kernels: list = []
    svc = TranslatorService(cfg, kernel_factory=_factory(kernels, output="Done."))
    res = svc.translate(_ZH_TEXT, target_lang="en")
    assert res.skipped == ""
    assert res.changed is True
    assert len(kernels) == 1


# ---- 提示构造与语言启发式 ----

def test_build_translation_prompt_contains_text_and_lang():
    p = build_translation_prompt("Hello world", "zh")
    assert "Hello world" in p
    assert "简体中文" in p
    assert "只输出译文" in p
    # 未列出的语言代码原样传入
    assert "fr" in build_translation_prompt("Hi", "fr")


def test_cjk_ratio_and_is_target_lang():
    assert _cjk_ratio("") == 0.0
    assert _cjk_ratio("中文") == 1.0
    assert _cjk_ratio("abcd") == 0.0
    assert _is_target_lang(_ZH_TEXT, "zh") is True
    assert _is_target_lang(_EN_TEXT, "zh") is False
    # 非中文目标：从不做 CJK 跳过判定
    assert _is_target_lang(_ZH_TEXT, "en") is False


# ---- 兜底人设（只读、无写/执行工具）----

def test_fallback_persona_is_read_only(tmp_path):
    cfg = _cfg(tmp_path)  # persona_dir 下无 translator.md -> 走兜底人设
    svc = TranslatorService(cfg, kernel_factory=_factory([]))
    p = svc._persona
    assert p.name == "translator"
    tools = set(p.tools or [])
    assert not (tools & {"Write", "Edit", "Bash", "NotebookEdit", "Agent"})
