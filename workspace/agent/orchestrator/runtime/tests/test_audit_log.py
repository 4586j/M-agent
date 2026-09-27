"""测试 audit_log：正常写入、路径覆盖、写入失败不阻断、force 记录、verdict 映射。

审计落盘路径由 tests/conftest.py 的 autouse 夹具统一隔离到临时目录。
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from audit_log import audit_file_path, record_security_event, record_verdict_event


def _read_events(path: Path) -> list[dict]:
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines()
            if x.strip()]


# ---- 正常写入 ----

def test_record_security_event_writes_jsonl():
    ev = record_security_event(
        task_id="task-1", stage="dispatch", decision="block",
        matched_rules=["rm-dash-rf"], llm_enhanced=False, reason="命中 block 规则")
    path = audit_file_path()
    assert path.exists()
    lines = _read_events(path)
    assert len(lines) == 1
    rec = lines[0]
    # 每行 JSON 含约定字段：timestamp/task_id/stage/decision/matched_rules/
    # operator/llm_enhanced（+ 可选 reason）
    assert rec["task_id"] == "task-1"
    assert rec["stage"] == "dispatch"
    assert rec["decision"] == "block"
    assert rec["matched_rules"] == ["rm-dash-rf"]
    assert rec["operator"] is None
    assert rec["llm_enhanced"] is False
    assert rec["reason"] == "命中 block 规则"
    assert "T" in rec["timestamp"]  # ISO8601 时间戳
    assert ev == rec


def test_record_appends_multiple_events():
    record_security_event(None, "dispatch", "pass")  # 前置拦截时无 task_id
    record_security_event("task-2", "product_check", "warn",
                          matched_rules=["warn-1"], llm_enhanced=True)
    lines = _read_events(audit_file_path())
    assert [r["decision"] for r in lines] == ["pass", "warn"]
    assert lines[0]["task_id"] is None
    assert lines[1]["stage"] == "product_check"
    assert lines[1]["llm_enhanced"] is True


def test_env_override_audit_file_path(tmp_path, monkeypatch):
    """ORCH_AUDIT_LOG 覆盖落盘路径。"""
    custom = tmp_path / "elsewhere" / "audit.jsonl"
    monkeypatch.setenv("ORCH_AUDIT_LOG", str(custom))
    record_security_event("task-9", "dispatch", "pass")
    assert custom.exists()
    assert _read_events(custom)[0]["task_id"] == "task-9"


# ---- force 记录 ----

def test_force_event_records_operator():
    record_security_event(None, "dispatch", "force", operator="web:127.0.0.1")
    rec = _read_events(audit_file_path())[0]
    assert rec["decision"] == "force"
    assert rec["operator"] == "web:127.0.0.1"


def test_force_event_operator_null_when_unknown():
    """操作者不可得时 operator 记为 null。"""
    record_security_event(None, "dispatch", "force")
    rec = _read_events(audit_file_path())[0]
    assert rec["decision"] == "force"
    assert rec["operator"] is None


# ---- 写入失败不阻断 ----

def test_write_failure_does_not_raise(tmp_path, monkeypatch):
    """审计路径指向目录（open 必失败）时吞异常返回 None，不阻断主流程。"""
    as_dir = tmp_path / "audit_dir"
    as_dir.mkdir()
    monkeypatch.setenv("ORCH_AUDIT_LOG", str(as_dir))
    ev = record_security_event("task-3", "dispatch", "pass")
    assert ev is None  # 不抛异常


def test_write_failure_logs_error_with_task_id(tmp_path, monkeypatch, caplog):
    """写入失败升级为 log.error（含 task_id），不再只是 warning。"""
    import logging
    as_dir = tmp_path / "audit_dir"
    as_dir.mkdir()
    monkeypatch.setenv("ORCH_AUDIT_LOG", str(as_dir))
    with caplog.at_level(logging.ERROR, logger="audit_log"):
        ev = record_security_event("task-42", "approve", "reject")
    assert ev is None
    assert any(r.levelno == logging.ERROR and "task-42" in r.getMessage()
               for r in caplog.records)


# ---- 并发写入不丢行 ----

def test_concurrent_writes_no_lost_lines():
    """多线程并发写入：模块级锁串行化 open+write，所有行都落盘且合法。"""
    import threading
    n_threads, n_writes = 8, 25

    def work(i):
        for j in range(n_writes):
            record_security_event(f"task-{i}-{j}", "dispatch", "pass")

    threads = [threading.Thread(target=work, args=(i,)) for i in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    lines = _read_events(audit_file_path())
    assert len(lines) == n_threads * n_writes  # 不丢行
    assert {r["task_id"] for r in lines} == {
        f"task-{i}-{j}" for i in range(n_threads) for j in range(n_writes)}


# ---- verdict -> 审计事件映射（产物后置关卡） ----

def test_verdict_event_mapping_block():
    record_verdict_event("task-4", {"verdict": "block", "reason": "危险",
                                    "matched_rules": ["r1"], "llm_enhanced": True})
    rec = _read_events(audit_file_path())[0]
    assert rec["stage"] == "product_check"
    assert rec["decision"] == "block"
    assert rec["matched_rules"] == ["r1"]
    assert rec["llm_enhanced"] is True


def test_verdict_event_pass_with_warn_rules_maps_to_warn():
    """规则 pass 且命中 warn 规则 -> 审计 decision=warn（放行留痕）。"""
    record_verdict_event("task-5", {"verdict": "pass", "reason": "命中 warn",
                                    "matched_rules": ["w1"], "llm_enhanced": False})
    rec = _read_events(audit_file_path())[0]
    assert rec["decision"] == "warn"


def test_verdict_event_plain_pass():
    record_verdict_event("task-7", {"verdict": "pass", "reason": "无规则命中",
                                    "matched_rules": [], "llm_enhanced": False})
    rec = _read_events(audit_file_path())[0]
    assert rec["decision"] == "pass"


def test_verdict_event_none_verdict_skipped():
    """verdict 为 None（未做产物检验）不产生审计行。"""
    assert record_verdict_event("task-6", None) is None
    assert not audit_file_path().exists()
