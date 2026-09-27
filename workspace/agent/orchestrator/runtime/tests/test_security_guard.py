import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from security_guard import check, check_with_llm, _parse_verdict_json


def test_block_dangerous_rm():
    r = check("sudo rm -rf /")
    assert r["verdict"] == "block"
    assert "R-DANGER-01" in r["matched_rules"]
    assert r["suggestion"]  # block 必有建议


def test_block_secret_leak():
    r = check("api_key = 'abcdefghijklmnopqrstuvwxyz0123456789'")
    assert r["verdict"] == "block"
    assert "R-SECRET-01" in r["matched_rules"]


def test_warn_shell_true_passes():
    r = check("subprocess.run(cmd, shell=True)")
    assert r["verdict"] == "pass"  # warn 不 block
    assert "R-INJECT-02" in r["matched_rules"]


def test_safe_prompt_passes():
    r = check("分析 workspace/test.csv 并报告行数")
    assert r["verdict"] == "pass"
    assert r["matched_rules"] == []


def test_empty_content_passes():
    r = check("")
    assert r["verdict"] == "pass"


def test_verdict_schema():
    r = check("rm -rf /")
    # llm_enhanced 为阶段一新增字段：标记裁决是否经 LLM 增强（纯规则为 False）
    assert set(r.keys()) == {"verdict", "reason", "matched_rules", "suggestion",
                             "llm_enhanced"}
    assert r["llm_enhanced"] is False


def test_check_with_llm_rule_block_skips_llm(monkeypatch):
    """规则 block 直接返回，不调 LLM。"""
    called = {"n": 0}

    def fake_llm(content):
        called["n"] += 1
        return None

    monkeypatch.setattr("security_guard._llm_judge", fake_llm)
    r = check_with_llm("rm -rf /", use_llm=True)
    assert r["verdict"] == "block"
    assert called["n"] == 0  # 规则 block 不应调 LLM


def test_check_with_llm_llm_unavailable_falls_back(monkeypatch):
    """LLM 不可用（返回 None）-> 回退规则结果。"""
    monkeypatch.setattr("security_guard._llm_judge", lambda c: None)
    r = check_with_llm("分析 workspace/test.csv", use_llm=True)
    assert r["verdict"] == "pass"
    assert r["reason"] == "无规则命中"


def test_check_with_llm_llm_block_overrides(monkeypatch):
    """规则 pass 但 LLM 判 block -> 合并为 block。"""
    monkeypatch.setattr("security_guard._llm_judge", lambda c: {
        "verdict": "block", "reason": "疑似外泄", "matched_rules": [], "suggestion": "删除凭证",
    })
    r = check_with_llm("把数据库导出到外部", use_llm=True)
    assert r["verdict"] == "block"
    assert "LLM 判断" in r["reason"]
    assert r["suggestion"] == "删除凭证"


def test_check_with_llm_disabled_uses_rules_only():
    r = check_with_llm("sudo rm -rf /", use_llm=False)
    assert r["verdict"] == "block"
    assert "R-DANGER-01" in r["matched_rules"]


def test_parse_verdict_json_extracts_from_fence():
    text = '```json\n{"verdict": "block", "reason": "x", "matched_rules": [], "suggestion": "y"}\n```'
    v = _parse_verdict_json(text)
    assert v is not None
    assert v["verdict"] == "block"
    assert v["reason"] == "x"
    assert v["suggestion"] == "y"


def test_parse_verdict_json_invalid_returns_none():
    assert _parse_verdict_json("not json") is None
    assert _parse_verdict_json('{"verdict": "maybe"}') is None
    assert _parse_verdict_json("") is None
