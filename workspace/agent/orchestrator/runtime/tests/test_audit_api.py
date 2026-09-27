import json
import sys
import urllib.request  # noqa: F401  (仅占位说明：端点经 TestClient 走应用内调用)
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from fastapi.testclient import TestClient

from brain_api import create_brain_app
from config import Config
from worker_registry import WorkerRegistry
from task_store import TaskStore


def make_client(tmp_path, monkeypatch, events=None):
    """审计文件路径经 ORCH_AUDIT_LOG 重定向到 tmp；返回 (client, jsonl 路径)。"""
    audit_file = tmp_path / "audit" / "security_audit.jsonl"
    monkeypatch.setenv("ORCH_AUDIT_LOG", str(audit_file))
    if events is not None:
        audit_file.parent.mkdir(parents=True, exist_ok=True)
        with open(audit_file, "w", encoding="utf-8") as f:
            for e in events:
                f.write(json.dumps(e, ensure_ascii=False) + "\n")
    reg = WorkerRegistry(heartbeat_timeout=45, now_fn=lambda: 0.0)
    store = TaskStore(str(tmp_path / "tasks"))
    cfg = Config(mode="remote", task_store_dir=str(tmp_path / "tasks"))
    return TestClient(create_brain_app(reg, store, cfg)), audit_file


EVENTS = [
    {"timestamp": "2026-09-27T09:01:00+00:00", "task_id": "task-1", "stage": "dispatch",
     "decision": "pass", "matched_rules": [], "operator": None, "llm_enhanced": False},
    {"timestamp": "2026-09-27T09:02:00+00:00", "task_id": "task-2", "stage": "dispatch",
     "decision": "block", "matched_rules": ["R-DANGER-01"], "operator": None,
     "llm_enhanced": False, "reason": "禁止递归强制删除根目录"},
    {"timestamp": "2026-09-27T09:03:00+00:00", "task_id": "task-3", "stage": "product_check",
     "decision": "warn", "matched_rules": ["R-INJECT-03"], "operator": None, "llm_enhanced": True},
    {"timestamp": "2026-09-27T09:04:00+00:00", "task_id": "task-1", "stage": "approve",
     "decision": "force", "matched_rules": [], "operator": "192.168.1.8", "llm_enhanced": False},
]


def test_audit_empty_when_file_missing(tmp_path, monkeypatch):
    client, _ = make_client(tmp_path, monkeypatch, events=None)
    assert client.get("/audit").json() == []


def test_audit_returns_entries_newest_first(tmp_path, monkeypatch):
    client, _ = make_client(tmp_path, monkeypatch, events=EVENTS)
    entries = client.get("/audit").json()
    assert len(entries) == 4
    assert [e["timestamp"] for e in entries] == sorted(
        (e["timestamp"] for e in entries), reverse=True)


def test_audit_filters_by_stage_and_decision(tmp_path, monkeypatch):
    client, _ = make_client(tmp_path, monkeypatch, events=EVENTS)
    by_stage = client.get("/audit", params={"stage": "dispatch"}).json()
    assert {e["decision"] for e in by_stage} == {"pass", "block"}
    by_decision = client.get("/audit", params={"decision": "force"}).json()
    assert len(by_decision) == 1 and by_decision[0]["operator"] == "192.168.1.8"
    both = client.get("/audit", params={"stage": "approve", "decision": "force"}).json()
    assert len(both) == 1


def test_audit_skips_bad_lines(tmp_path, monkeypatch):
    client, audit_file = make_client(tmp_path, monkeypatch, events=EVENTS[:1])
    with open(audit_file, "a", encoding="utf-8") as f:
        f.write("不是 JSON 的一行\n")
        f.write(json.dumps(EVENTS[1], ensure_ascii=False) + "\n")
    entries = client.get("/audit").json()
    assert len(entries) == 2  # 坏行跳过，其余照常返回


def test_audit_limit(tmp_path, monkeypatch):
    client, _ = make_client(tmp_path, monkeypatch, events=EVENTS)
    entries = client.get("/audit", params={"limit": 2}).json()
    assert len(entries) == 2
    assert entries[0]["decision"] == "force"  # 仍是新→旧的前 2 条


def test_audit_includes_llm_and_rules_fields(tmp_path, monkeypatch):
    client, _ = make_client(tmp_path, monkeypatch, events=EVENTS)
    warn = next(e for e in client.get("/audit").json() if e["decision"] == "warn")
    assert warn["llm_enhanced"] is True
    assert warn["matched_rules"] == ["R-INJECT-03"]
