import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from security_guard import check


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
    assert set(r.keys()) == {"verdict", "reason", "matched_rules", "suggestion"}
