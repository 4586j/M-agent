"""security-guard 指令检验（接入 brain_api/dispatch）。

基于 security-rules 的确定性正则规则匹配，返回 pass/block 裁决，
格式与 .claude/agents/security-guard.md 一致：{verdict, reason, matched_rules, suggestion}。

LLM 版 security-guard 的"规则之外可疑情况判断"作为后续增强；当前覆盖
default.yaml + overrides 的全部 block/warn 正则规则。
"""
import re
import sys
from pathlib import Path

# 定位 security-rules 目录：runtime -> orchestrator -> agent -> skills/orchestrator/security-rules
_RULES_DIR = (Path(__file__).resolve().parent.parent.parent
              / "skills" / "orchestrator" / "security-rules")
if str(_RULES_DIR) not in sys.path:
    sys.path.insert(0, str(_RULES_DIR))
from rules import load_effective_rules


def check(content: str) -> dict:
    """对 content 逐条规则匹配，返回裁决。"""
    if not content:
        return {"verdict": "pass", "reason": "空内容", "matched_rules": [], "suggestion": ""}
    matched_block: list[dict] = []
    matched_warn: list[dict] = []
    for r in load_effective_rules():
        try:
            if re.search(r["pattern"], content):
                (matched_block if r["severity"] == "block" else matched_warn).append(r)
        except re.error:
            continue
    if matched_block:
        return {
            "verdict": "block",
            "reason": "命中 block 规则: " + "; ".join(r["description"] for r in matched_block),
            "matched_rules": [r["id"] for r in matched_block],
            "suggestion": "修改指令移除危险内容，或确认后强制放行",
        }
    if matched_warn:
        return {
            "verdict": "pass",
            "reason": "命中 warn 规则（已放行）: " + "; ".join(r["description"] for r in matched_warn),
            "matched_rules": [r["id"] for r in matched_warn],
            "suggestion": "",
        }
    return {"verdict": "pass", "reason": "无规则命中", "matched_rules": [], "suggestion": ""}
