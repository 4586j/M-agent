"""Prompt Injection 检测器：对用户输入做轻量级注入攻击检测。

检测规则（纯规则，不依赖 LLM）：
- 角色劫持：含 "ignore previous instructions" / "忽略之前指令" / "你现在是" 等
- 分隔符伪造：含伪造的 system/user/assistant 分隔标记
- 指令覆盖：含 "new instructions" / "覆盖指令" / "forget everything" 等
- 编码绕过：含大段 base64 / hex 编码文本（试图绕过文本匹配）

设计原则：保守检测，宁可漏判不误拦——误拦会阻断正常任务，
因此阈值设得较高（需多信号叠加才触发拦截）。
"""
import logging
import re
from dataclasses import dataclass, field

log = logging.getLogger(__name__)


@dataclass
class InjectionVerdict:
    """注入检测结果。

    is_injection: 是否判定为注入（True 时调用方应拒绝执行）。
    confidence: 置信度 "low" | "medium" | "high"。
    signals: 命中的检测信号名列表。
    score: 0.0~1.0 综合得分（越高越可能是注入）。
    """

    is_injection: bool = False
    confidence: str = "low"
    signals: list[str] = field(default_factory=list)
    score: float = 0.0

    def to_dict(self) -> dict:
        return {
            "is_injection": self.is_injection,
            "confidence": self.confidence,
            "signals": self.signals,
            "score": round(self.score, 3),
        }


# ---- 检测规则 ----

# 角色劫持模式（中英文）—— 要求匹配较长片段，降低误判
_ROLE_HIJACK_PATTERNS = [
    re.compile(r"ignore\s+(all\s+)?previous\s+instructions", re.IGNORECASE),
    re.compile(r"忽略(之前|以上|所有).{0,4}(指令|指示|设定|规则)"),
    re.compile(r"你现在是\s*\S+"),  # "你现在是" 后须跟内容
    re.compile(r"you\s+are\s+now\s+(a|an)\s+\S+", re.IGNORECASE),
    re.compile(r"forget\s+(all\s+)?(your\s+)?(previous\s+)?(instructions|rules|guidelines)",
               re.IGNORECASE),
    re.compile(r"忘记(所有|之前|你的)?.{0,4}(指令|规则|设定|一切)"),
    re.compile(r"disregard\s+(all\s+)?(previous|prior|above)\s+(instructions|rules|context)",
               re.IGNORECASE),
]

# 分隔符伪造模式
_DELIMITER_PATTERNS = [
    re.compile(r"^```?\s*(system|human|user|assistant|ai)\s*```?\s*$", re.IGNORECASE | re.MULTILINE),
    re.compile(r"^<\|?(system|human|user|assistant)\|?>\s*$", re.MULTILINE),
    re.compile(r"^\[INST\]|\[/INST\]|<s>\s*\[", re.MULTILINE),
    re.compile(r"^###\s*(system|instruction|role)\s*:", re.IGNORECASE | re.MULTILINE),
    re.compile(r"^---+\s*(system|SYSTEM|指令|规则)\s*---+\s*$", re.MULTILINE),
]

# 指令覆盖模式
_OVERRIDE_PATTERNS = [
    re.compile(r"new\s+instructions?\s*:", re.IGNORECASE),
    re.compile(r"覆盖(所有|之前|原来的).{0,4}(指令|规则|设定)"),
    re.compile(r"your\s+new\s+(role|persona|identity|task)\s+is", re.IGNORECASE),
    re.compile(r"(act|behave|respond)\s+(as|like)\s+(if\s+)?(a\s+)?(different|new)\s+",
               re.IGNORECASE),
    re.compile(r"from\s+now\s+on\s*,?\s*(you|ignore|forget|do)", re.IGNORECASE),
]

# 编码绕过检测：连续 base64 字符超过阈值
_BASE64_PATTERN = re.compile(
    r"[A-Za-z0-9+/=]{80,}"  # 连续 80+ 字符的 base64 候选
)
# Hex 编码检测
_HEX_PATTERN = re.compile(
    r"(?:0x)?[0-9a-fA-F]{60,}"  # 连续 60+ 字符的 hex 候选
)

# 越狱关键词组合（需要同时命中多个才触发；高门槛避免误拦）
_JAILBREAK_COMBOS = [
    # DAN 模式：需要 "DAN" 或 "do anything now" 明确出现
    (re.compile(r"\bDAN\b|do\s+anything\s+now", re.IGNORECASE),
     None),  # 单信号即可（关键词本身已足够明确）
    # 假装 + 无限制：需要两个信号同时出现
    (re.compile(r"pretend\b", re.IGNORECASE),
     re.compile(r"(没有|不受|无视).{0,4}(规则|限制|约束|安全)")),
    # 中文假装模式（单独 combo，不需要 \b）
    (re.compile(r"假装(你是|作为一个)"),
     re.compile(r"(没有|不受|无视).{0,4}(规则|限制|约束|安全)")),
]


def _check_role_hijack(prompt: str) -> tuple[bool, float]:
    """检查角色劫持模式。返回 (命中, 得分)。"""
    for pat in _ROLE_HIJACK_PATTERNS:
        if pat.search(prompt):
            return True, 0.7
    return False, 0.0


def _check_delimiter_inject(prompt: str) -> tuple[bool, float]:
    """检查分隔符伪造。返回 (命中, 得分)。"""
    for pat in _DELIMITER_PATTERNS:
        if pat.search(prompt):
            return True, 0.6
    return False, 0.0


def _check_instruction_override(prompt: str) -> tuple[bool, float]:
    """检查指令覆盖模式。返回 (命中, 得分)。"""
    for pat in _OVERRIDE_PATTERNS:
        if pat.search(prompt):
            return True, 0.5
    return False, 0.0


def _check_encoding_bypass(prompt: str) -> tuple[bool, float]:
    """检查编码绕过。返回 (命中, 得分)。

    仅当编码段占 prompt 比例较高时才触发（避免误拦含少量 hash/ID 的正常输入）。
    """
    prompt_len = max(len(prompt), 1)
    # Base64 检测
    b64_matches = _BASE64_PATTERN.findall(prompt)
    b64_chars = sum(len(m) for m in b64_matches)
    if b64_chars > prompt_len * 0.5 and b64_chars > 100:
        return True, 0.6
    # Hex 检测
    hex_matches = _HEX_PATTERN.findall(prompt)
    hex_chars = sum(len(m) for m in hex_matches)
    if hex_chars > prompt_len * 0.5 and hex_chars > 80:
        return True, 0.5
    return False, 0.0


def _check_jailbreak_combo(prompt: str) -> tuple[bool, float]:
    """检查越狱关键词组合。需要多信号叠加才触发。返回 (命中, 得分)。"""
    for combo in _JAILBREAK_COMBOS:
        # 过滤掉 None 占位符，只检查实际正则
        pats = [p for p in combo if p is not None]
        matched = sum(1 for p in pats if p.search(prompt))
        # 全部模式都命中才算通过（单模式 combo 只需命中 1 个）
        if matched >= len(pats):
            return True, 0.65
    return False, 0.0


def detect_injection(prompt: str) -> InjectionVerdict:
    """对用户输入做注入攻击检测。

    返回 InjectionVerdict。is_injection=True 时建议拒绝执行。
    检测策略：多规则打分，综合得分超过阈值才判定为注入。
    任何检查异常不阻断（异常隔离），降级放行。
    """
    if not prompt or not prompt.strip():
        return InjectionVerdict()

    verdict = InjectionVerdict()
    checks = [
        ("role_hijack", _check_role_hijack),
        ("delimiter_inject", _check_delimiter_inject),
        ("instruction_override", _check_instruction_override),
        ("encoding_bypass", _check_encoding_bypass),
        ("jailbreak_combo", _check_jailbreak_combo),
    ]

    total_score = 0.0
    for signal_name, check_fn in checks:
        try:
            hit, score = check_fn(prompt)
            if hit:
                verdict.signals.append(signal_name)
                total_score = max(total_score, score)
        except Exception:  # noqa: BLE001 - 单规则异常不影响其他
            log.warning("注入检测规则 %s 异常，跳过", signal_name, exc_info=True)

    verdict.score = total_score

    # 判定逻辑：
    # - 2+ 信号命中 或 得分 >= 0.65 → 高置信度拦截
    # - 1 信号命中 且 得分 >= 0.5 → 中置信度（记录但不拦截）
    # - 仅低分信号 → 不拦截
    n_signals = len(verdict.signals)
    if n_signals >= 2 or verdict.score >= 0.65:
        verdict.is_injection = True
        verdict.confidence = "high"
    elif n_signals == 1 and verdict.score >= 0.5:
        verdict.is_injection = False  # 保守：单信号不拦截
        verdict.confidence = "medium"
    elif n_signals == 1:
        verdict.is_injection = False
        verdict.confidence = "low"
    else:
        verdict.confidence = "low"

    return verdict
