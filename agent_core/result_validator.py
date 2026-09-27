"""结果验证器：对 Agent 输出做结构化验证（自纠错闭环的核心组件）。

验证策略按输出内容自动选择：
- 含 ```python 围栏 → ast.parse 语法检查
- 含 JSON 块 → json.loads 格式检查
- 通用 → 输出长度/prompt 覆盖度（关键词命中）

验证结果用于驱动修正循环：验证失败时将 issues 注入 prompt 让 Agent 自我修正。
设计原则：保守验证，宁可漏判不误伤——只捕获明确的结构性错误，不做主观质量评判。
"""
import ast
import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger(__name__)

# 验证器检测的代码块语言标记
_CODE_BLOCK_RE = re.compile(r"```(\w+)?\s*\n(.*?)```", re.DOTALL)
# JSON 块检测（```json 围栏 或 独立 {...} / [...] 块）
_JSON_FENCE_RE = re.compile(r"```json\s*\n(.*?)```", re.DOTALL)
_JSON_BARE_RE = re.compile(r"(\{[^{}]{10,}(?:\{[^{}]*\}[^{}]*)*\})", re.DOTALL)

# prompt 中的步骤/要求关键词（用于覆盖度检查）
_REQUIREMENT_PATTERNS = [
    re.compile(r"(?:请|需要|必须|要求|步骤)\s*[：:]?\s*(.+?)(?:[。\n；;]|$)"),
    re.compile(r"(?:\d+[\.\)、])\s*(.+?)(?:[。\n]|$)"),
]


@dataclass
class ValidationVerdict:
    """验证结果。

    is_valid: 验证是否通过（无明确错误）。
    issues: 发现的问题列表（空 = 无问题）。
    severity: 严重程度 "none" | "low" | "medium" | "high"。
    checks_run: 实际执行的检查名列表（便于 trace/debug）。
    """

    is_valid: bool = True
    issues: list[str] = field(default_factory=list)
    severity: str = "none"
    checks_run: list[str] = field(default_factory=list)


def _extract_code_blocks(output: str) -> list[tuple[str, str]]:
    """提取输出中的代码块，返回 [(language, code), ...]。"""
    return _CODE_BLOCK_RE.findall(output)


def _extract_json_blocks(output: str) -> list[str]:
    """提取输出中的 JSON 块（围栏 + 裸 JSON），返回原始字符串列表。"""
    candidates = []
    # ```json 围栏
    for m in _JSON_FENCE_RE.finditer(output):
        candidates.append(m.group(1).strip())
    # 裸 JSON（仅当没有围栏 JSON 时）
    if not candidates:
        for m in _JSON_BARE_RE.finditer(output):
            candidates.append(m.group(1).strip())
    return candidates


def _check_python_syntax(blocks: list[tuple[str, str]]) -> list[str]:
    """检查 Python 代码块的语法正确性（ast.parse）。"""
    issues = []
    for lang, code in blocks:
        if lang not in ("python", "py", ""):
            continue
        if not code.strip():
            continue
        try:
            ast.parse(code)
        except SyntaxError as e:
            issues.append(
                f"Python 语法错误（行 {e.lineno}）：{e.msg}"
            )
    return issues


def _check_json_format(blocks: list[str]) -> list[str]:
    """检查 JSON 块的格式正确性。"""
    issues = []
    for i, block in enumerate(blocks):
        try:
            json.loads(block)
        except json.JSONDecodeError as e:
            issues.append(
                f"JSON 格式错误（块 {i + 1}，行 {e.lineno}）：{e.msg}"
            )
    return issues


def _check_completeness(output: str, prompt: str) -> list[str]:
    """检查输出是否回应了 prompt 中的主要要求（关键词覆盖度）。

    仅当 prompt 含明确步骤/要求时检查；简单问答跳过。
    提取 prompt 中的要求关键词，检查 output 中是否出现。
    覆盖度低于 50% 时报告问题（弱信号，severity=low）。
    """
    requirements: list[str] = []
    for pat in _REQUIREMENT_PATTERNS:
        for m in pat.finditer(prompt):
            req = m.group(1).strip()
            if len(req) >= 4:  # 过短的片段不计入
                requirements.append(req)
    if len(requirements) < 2:
        return []  # 要求太少，不值得检查
    output_lower = output.lower()
    missed = []
    for req in requirements:
        # 提取要求中的核心关键词（取前 20 字符中的 ASCII 词或 CJK 段）
        keywords = re.findall(r"[a-zA-Z_]{3,}|[\u4e00-\u9fff]{2,}", req.lower())
        if not keywords:
            continue
        # 至少命中一个关键词视为覆盖
        if not any(kw in output_lower for kw in keywords):
            missed.append(req[:60])
    if len(missed) > len(requirements) * 0.5:
        return [f"输出可能未覆盖以下要求：{'; '.join(missed[:3])}"]
    return []


def validate_output(output: str, prompt: str = "", cwd: str = ""
                    ) -> ValidationVerdict:
    """对 Agent 输出执行全部适用检查，返回综合 ValidationVerdict。

    检查策略：
    - 始终执行：Python 语法（如有代码块）、JSON 格式（如有 JSON 块）
    - prompt 非空时执行：覆盖度检查
    - 任何检查异常不阻断其他检查（异常隔离）
    """
    if not output or not output.strip():
        return ValidationVerdict(is_valid=True, checks_run=["empty_skip"])

    checks_run: list[str] = []
    all_issues: list[str] = []
    severity = "none"

    # Python 语法检查
    code_blocks = _extract_code_blocks(output)
    if code_blocks:
        checks_run.append("python_syntax")
        try:
            py_issues = _check_python_syntax(code_blocks)
            if py_issues:
                all_issues.extend(py_issues)
                severity = "high"
        except Exception:  # noqa: BLE001
            log.warning("Python 语法检查异常，跳过", exc_info=True)

    # JSON 格式检查
    json_blocks = _extract_json_blocks(output)
    if json_blocks:
        checks_run.append("json_format")
        try:
            json_issues = _check_json_format(json_blocks)
            if json_issues:
                all_issues.extend(json_issues)
                severity = _max_severity(severity, "medium")
        except Exception:  # noqa: BLE001
            log.warning("JSON 格式检查异常，跳过", exc_info=True)

    # 覆盖度检查（prompt 非空时）
    if prompt and len(prompt.strip()) > 50:
        checks_run.append("completeness")
        try:
            comp_issues = _check_completeness(output, prompt)
            if comp_issues:
                all_issues.extend(comp_issues)
                severity = _max_severity(severity, "low")
        except Exception:  # noqa: BLE001
            log.warning("覆盖度检查异常，跳过", exc_info=True)

    return ValidationVerdict(
        is_valid=len(all_issues) == 0,
        issues=all_issues,
        severity=severity,
        checks_run=checks_run,
    )


_SEVERITY_ORDER = {"none": 0, "low": 1, "medium": 2, "high": 3}


def _max_severity(a: str, b: str) -> str:
    """返回两个严重程度中较高的那个。"""
    return a if _SEVERITY_ORDER.get(a, 0) >= _SEVERITY_ORDER.get(b, 0) else b


def build_correction_hint(verdict: ValidationVerdict) -> str:
    """根据验证结果构造修正提示（注入 prompt 引导 Agent 自我修正）。"""
    if verdict.is_valid:
        return ""
    lines = ["[验证反馈] 上次输出存在以下问题，请修正后重新输出："]
    for i, issue in enumerate(verdict.issues, 1):
        lines.append(f"  {i}. {issue}")
    lines.append("\n请针对上述问题逐一修正，保持其他内容不变。")
    return "\n".join(lines)
