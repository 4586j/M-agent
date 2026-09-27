"""审计流水只读 API（/audit）：独立 router，避免与 brain_api 主文件交叉改动。

数据源：audit_log JSONL（security_audit.jsonl，ORCH_AUDIT_LOG 可覆盖）。
只读、无状态；过滤参数 stage/decision 可组合，返回按时间新→旧。
"""
import json

from fastapi import APIRouter

from audit_log import audit_file_path


def create_audit_router() -> APIRouter:
    router = APIRouter()

    @router.get("/audit")
    def list_audit(stage: str | None = None, decision: str | None = None,
                   limit: int = 200):
        """安全裁决审计流水（新→旧）。

        - stage: dispatch（指令前置）/ product_check（产物后置）/ approve（审批环节）
        - decision: block / warn / pass / force / approve / reject
        - limit: 返回上限（默认 200，防大文件全量传输）
        """
        path = audit_file_path()
        if not path.exists():
            return []
        entries: list[dict] = []
        try:
            with open(path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entries.append(json.loads(line))
                    except ValueError:
                        continue  # 坏行跳过，不拖垮整个流水
        except OSError:
            return []
        entries.reverse()  # 追加写文件 → 倒序即新→旧
        if stage:
            entries = [e for e in entries if e.get("stage") == stage]
        if decision:
            entries = [e for e in entries if e.get("decision") == decision]
        return entries[: max(0, limit)]

    return router
