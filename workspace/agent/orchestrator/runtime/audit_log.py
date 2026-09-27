"""安全裁决审计日志：把 security-guard 各环节的裁决追加落盘为 JSONL。

职责单一：记录「谁在哪个环节做了什么安全决定」，供事后追溯与合规检查。
写入失败绝不阻断主流程（吞异常 + log 警告）——宁可少一条审计，
不能拖垮派发链路。

文件位置：默认 runtime/audit/security_audit.jsonl（相对本模块所在目录），
可用环境变量 ORCH_AUDIT_LOG 覆盖为任意文件路径。

每行一个 JSON 对象，字段：
- timestamp:     ISO8601 时间戳（UTC，秒级）
- task_id:       关联任务 id（dispatch 前置拦截时可能尚无 task_id，为 null）
- stage:         裁决环节：dispatch（指令前置）/ product_check（产物后置）/
                 approve（审批环节：人工放行/拒绝/修订重检）
- decision:      block / warn / pass / force（force=人工强制放行）/
                 approve / reject（审批环节人工裁决）
- matched_rules: 命中的规则 id 列表
- operator:      操作者/来源（force 放行时尽力记录：X-Operator 头或客户端 host）
- llm_enhanced:  本次裁决是否启用了 LLM 增强（security_llm）
- reason:        可选，裁决一句话依据（便于人工阅读）
"""
import json
import logging
import os
import threading
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger(__name__)

# 默认审计目录/文件：与本模块同级 runtime/audit/ 下，基于文件位置定位不随 cwd 漂移
_DEFAULT_AUDIT_FILE = Path(__file__).resolve().parent / "audit" / "security_audit.jsonl"

_AUDIT_FILE_ENV = "ORCH_AUDIT_LOG"

# 写入互斥锁：Windows 下 O_APPEND 跨句柄非原子，dispatcher 线程池/事件循环/
# sweeper 可能并发写同一 JSONL，不串行化会丢行/串行交错。
_WRITE_LOCK = threading.Lock()


def audit_file_path() -> Path:
    """审计文件路径：ORCH_AUDIT_LOG 覆盖，默认 runtime/audit/security_audit.jsonl。"""
    override = os.environ.get(_AUDIT_FILE_ENV, "")
    return Path(override) if override else _DEFAULT_AUDIT_FILE


def record_security_event(task_id: str | None, stage: str, decision: str,
                          matched_rules: list | None = None,
                          operator: str | None = None,
                          llm_enhanced: bool = False,
                          reason: str = "") -> dict | None:
    """追加一条安全裁决审计事件；任何失败只记日志，不抛出不阻断主流程。

    参数：
    - task_id: 关联任务 id（前置拦截时可能还没有，传 None）
    - stage: "dispatch"（指令前置）| "product_check"（产物后置）| "approve"（审批环节）
    - decision: "block" | "warn" | "pass" | "force" | "approve" | "reject"
    - matched_rules: 命中的规则 id 列表
    - operator: 操作者/来源（force/approve/reject 放行或裁决时尽力记录，可得则记录否则 None）
    - llm_enhanced: 本次裁决是否启用了 LLM 增强
    - reason: 裁决一句话依据（可选）

    返回写入的事件 dict；写入失败返回 None。
    """
    event: dict = {
        "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "task_id": task_id,
        "stage": stage,
        "decision": decision,
        "matched_rules": list(matched_rules or []),
        "operator": operator,
        "llm_enhanced": bool(llm_enhanced),
    }
    if reason:
        event["reason"] = reason
    try:
        path = audit_file_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        # open+write 全程持锁：跨句柄 O_APPEND 非原子（Windows 尤甚），并发写会丢行
        with _WRITE_LOCK:
            with path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(event, ensure_ascii=False) + "\n")
        return event
    except Exception as e:  # noqa: BLE001 - 审计失败不阻断主流程，但必须高声量报警
        log.error("安全审计写入失败（task_id=%s，stage=%s，不阻断主流程）: %s",
                  task_id, stage, e)
        return None


def record_verdict_event(task_id: str | None, verdict: dict | None,
                         stage: str = "product_check",
                         operator: str | None = None) -> dict | None:
    """把 security-guard 裁决 dict 映射为审计事件（dispatcher 产物后置关卡用）。

    verdict 形如 {verdict, reason, matched_rules, llm_enhanced}（见 security_guard）：
    - verdict=block -> decision=block
    - verdict=pass 且命中 warn 规则 -> decision=warn
    - verdict=pass 无规则命中 -> decision=pass
    - verdict 为 None（未做产物检验）不记录
    """
    if verdict is None:
        return None
    decision = verdict.get("verdict") or "pass"
    if decision == "pass" and verdict.get("matched_rules"):
        decision = "warn"
    return record_security_event(
        task_id=task_id, stage=stage, decision=decision,
        matched_rules=verdict.get("matched_rules"),
        operator=operator,
        llm_enhanced=bool(verdict.get("llm_enhanced", False)),
        reason=str(verdict.get("reason") or ""),
    )
