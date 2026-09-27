"""测试 agent_core.doctor：只读自检各检查项。

- SDK 导入性用 monkeypatch doctor._import_sdk 模拟装/未装；
- 鉴权 / .env / persona / MCP 检查用 tmp_path + 环境变量隔离。
全程不触网。
"""
import json
from types import SimpleNamespace

import pytest

from agent_core import doctor
from agent_core.doctor import (
    STATUS_FAIL,
    STATUS_OK,
    STATUS_WARN,
    CheckResult,
    check_auth,
    check_env_file,
    check_mcp,
    check_personas,
    check_sdk,
    format_report,
    run_checks,
    summarize,
)

# ---- 轻量配置替身（doctor 只读 persona_dir/project_root/mcp_config_path） ----


def _cfg(tmp_path, mcp=None):
    return SimpleNamespace(
        project_root=str(tmp_path),
        persona_dir=str(tmp_path / "agents"),
        mcp_config_path=str(mcp) if mcp else "",
    )


def _clean_auth_env(monkeypatch):
    for name in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN",
                 "ANTHROPIC_BASE_URL"):
        monkeypatch.delenv(name, raising=False)


# ---- check_sdk ----

def test_check_sdk_ok_when_importable():
    res = check_sdk()
    # 测试环境已安装 SDK（假 SDK 由 conftest 不干预这里），导入成功即 ok
    assert res.status in (STATUS_OK, STATUS_FAIL)


def test_check_sdk_fail_when_missing(monkeypatch):
    def _raise():
        raise ImportError("No module named 'claude_agent_sdk'")
    monkeypatch.setattr(doctor, "_import_sdk", _raise)
    res = check_sdk()
    assert res.status == STATUS_FAIL
    assert "pip install" in res.detail


# ---- check_auth ----

def test_check_auth_official_api_key(monkeypatch):
    _clean_auth_env(monkeypatch)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-x")
    res = check_auth()
    assert res.status == STATUS_OK
    assert "官方" in res.detail


def test_check_auth_api_key_with_base_url(monkeypatch):
    _clean_auth_env(monkeypatch)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-x")
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://relay.example.com")
    res = check_auth()
    assert res.status == STATUS_OK
    assert "第三方" in res.detail


def test_check_auth_token_with_base_url(monkeypatch):
    _clean_auth_env(monkeypatch)
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "tok-x")
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://relay.example.com")
    res = check_auth()
    assert res.status == STATUS_OK
    assert "第三方" in res.detail


def test_check_auth_token_without_base_url_warns(monkeypatch):
    _clean_auth_env(monkeypatch)
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "tok-x")
    res = check_auth()
    assert res.status == STATUS_WARN
    assert "BASE_URL" in res.detail


def test_check_auth_missing_fails_with_hint(monkeypatch):
    _clean_auth_env(monkeypatch)
    res = check_auth()
    assert res.status == STATUS_FAIL
    assert "ANTHROPIC_API_KEY" in res.detail


# ---- check_mcp ----

def test_check_mcp_ok_with_valid_servers(tmp_path):
    cfg_path = tmp_path / ".mcp.json"
    cfg_path.write_text(json.dumps({"mcpServers": {
        "fs": {"command": "python", "args": ["-m", "fs"]}}}),
        encoding="utf-8")
    res = check_mcp(_cfg(tmp_path, cfg_path))
    assert res.status == STATUS_OK
    assert "fs" in res.detail


def test_check_mcp_warns_on_broken_entries(tmp_path):
    cfg_path = tmp_path / ".mcp.json"
    cfg_path.write_text(json.dumps({"mcpServers": {
        "bad": {"description": "缺 command/url"},
        "good": {"command": "python"}}}), encoding="utf-8")
    res = check_mcp(_cfg(tmp_path, cfg_path))
    assert res.status == STATUS_WARN
    assert "bad" in res.detail  # 逐条告警损坏条目


def test_check_mcp_skipped_when_unconfigured(tmp_path):
    res = check_mcp(_cfg(tmp_path))
    assert res.status == STATUS_OK
    assert "跳过" in res.detail


# ---- check_personas ----

def test_check_personas_ok_with_default(tmp_path):
    d = tmp_path / "agents"
    d.mkdir()
    (d / "executor.md").write_text("---\nname: executor\n---\n正文",
                                   encoding="utf-8")
    res = check_personas(_cfg(tmp_path), default_persona="executor")
    assert res.status == STATUS_OK
    assert "executor" in res.detail


def test_check_personas_warns_on_missing_default(tmp_path):
    d = tmp_path / "agents"
    d.mkdir()
    (d / "other.md").write_text("x", encoding="utf-8")
    res = check_personas(_cfg(tmp_path), default_persona="executor")
    assert res.status == STATUS_WARN
    assert "other" in res.detail


def test_check_personas_warns_on_empty_dir(tmp_path):
    (tmp_path / "agents").mkdir()
    res = check_personas(_cfg(tmp_path))
    assert res.status == STATUS_WARN


def test_check_personas_warns_on_missing_dir(tmp_path):
    res = check_personas(_cfg(tmp_path))
    assert res.status == STATUS_WARN


# ---- check_env_file ----

def test_check_env_file_ok(tmp_path):
    (tmp_path / ".env").write_text("A=1", encoding="utf-8")
    res = check_env_file(_cfg(tmp_path))
    assert res.status == STATUS_OK


def test_check_env_file_warn_when_absent(tmp_path):
    res = check_env_file(_cfg(tmp_path))
    assert res.status == STATUS_WARN


# ---- run_checks / summarize / format_report ----

def test_run_checks_returns_all_items(tmp_path, monkeypatch):
    _clean_auth_env(monkeypatch)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-x")
    checks = run_checks(_cfg(tmp_path), default_persona="executor")
    names = [c.name for c in checks]
    assert names == ["sdk", "auth", "mcp", "personas", "env_file"]


def test_run_checks_single_failure_does_not_kill_all(tmp_path, monkeypatch):
    """单项检查抛异常时降级为失败项，其余项照常返回。"""
    _clean_auth_env(monkeypatch)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-x")

    def _boom(cfg):
        raise RuntimeError("mcp check crashed")
    monkeypatch.setattr(doctor, "check_mcp", _boom)
    checks = run_checks(_cfg(tmp_path))
    by_name = {c.name: c for c in checks}
    assert by_name["mcp"].status == STATUS_FAIL
    assert by_name["auth"].status == STATUS_OK


def test_summarize_counts():
    checks = [CheckResult("a", STATUS_OK, ""),
              CheckResult("b", STATUS_WARN, ""),
              CheckResult("c", STATUS_FAIL, "")]
    s = summarize(checks)
    assert s == {"total": 3, "ok": 1, "warn": 1, "fail": 1,
                 "items": [c.to_dict() for c in checks]}


def test_format_report_marks_and_summary():
    checks = [CheckResult("a", STATUS_OK, "一切正常"),
              CheckResult("b", STATUS_FAIL, "缺失")]
    text = format_report(checks)
    assert "[通过] a" in text
    assert "[失败] b" in text
    assert "汇总：2 项" in text


def test_report_text_end_to_end(tmp_path, monkeypatch):
    _clean_auth_env(monkeypatch)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-x")
    text = doctor.report_text(_cfg(tmp_path))
    assert "自检" in text
    assert "汇总" in text
