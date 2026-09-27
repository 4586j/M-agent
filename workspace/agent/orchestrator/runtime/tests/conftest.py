"""runtime 测试共享夹具。

阶段一安全加固后 dispatcher/brain_api 会写安全审计日志；本夹具把审计
落盘路径隔离到每个用例的临时目录，避免测试污染真实 runtime/audit/。
"""
import pytest


@pytest.fixture(autouse=True)
def _isolate_audit_log(tmp_path, monkeypatch):
    """所有 runtime 测试的审计写入都落到独立临时路径（ORCH_AUDIT_LOG）。"""
    monkeypatch.setenv("ORCH_AUDIT_LOG",
                       str(tmp_path / "audit" / "security_audit.jsonl"))
    yield
