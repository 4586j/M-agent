"""runtime 测试共享夹具。

阶段一安全加固后 dispatcher/brain_api 会写安全审计日志；本夹具把审计
落盘路径隔离到每个用例的临时目录，避免测试污染真实 runtime/audit/。
"""
from pathlib import Path

import pytest


def pytest_configure(config):
    config.addinivalue_line("markers", "sdk_smoke: opt-in real SDK calls with an explicit budget")


@pytest.fixture(autouse=True)
def _isolate_audit_log(tmp_path, monkeypatch):
    """所有 runtime 测试的审计写入都落到独立临时路径（ORCH_AUDIT_LOG）。"""
    monkeypatch.setenv("ORCH_AUDIT_LOG",
                       str(tmp_path / "audit" / "security_audit.jsonl"))
    yield


@pytest.fixture(autouse=True)
def _isolate_default_budget_ledger(tmp_path, monkeypatch):
    """Default Config paths must not migrate or mutate the operator's live ledger."""
    from budget_store import BudgetStore
    default_path = Path(__file__).resolve().parents[1] / "budgets.json"
    original = BudgetStore.__init__

    def isolated(self, budgets_file, *args, **kwargs):
        if Path(budgets_file).resolve() == default_path:
            budgets_file = str(tmp_path / "isolated-budget.json")
        original(self, budgets_file, *args, **kwargs)
    monkeypatch.setattr(BudgetStore, "__init__", isolated)
