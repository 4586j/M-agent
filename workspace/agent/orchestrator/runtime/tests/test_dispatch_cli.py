"""测试 dispatch_cli：鉴权部署下请求携带 X-API-Key（ORCH_API_KEY/AGENT_API_KEY）。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from dispatch_cli import dispatch as cli_dispatch


class _FakeResp:
    def __init__(self, payload):
        self._payload = payload
        self.text = str(payload)

    def json(self):
        return self._payload


def _patch_post(monkeypatch, captured):
    import requests

    def fake_post(url, json=None, headers=None, timeout=None):
        captured["url"] = url
        captured["body"] = json
        captured["headers"] = headers
        return _FakeResp({"status": "done", "task_id": "task-x"})

    monkeypatch.setattr(requests, "post", fake_post)


def test_dispatch_sends_api_key_header(monkeypatch):
    """配置 ORCH_API_KEY 时请求头携带 X-API-Key。"""
    captured = {}
    _patch_post(monkeypatch, captured)
    monkeypatch.setenv("ORCH_API_KEY", "secret-key")
    monkeypatch.delenv("AGENT_API_KEY", raising=False)

    res = cli_dispatch("hello")
    assert res["status"] == "done"
    assert captured["headers"]["X-API-Key"] == "secret-key"
    assert captured["url"].endswith("/dispatch")


def test_dispatch_falls_back_to_agent_api_key(monkeypatch):
    """ORCH_API_KEY 缺省时回退 AGENT_API_KEY。"""
    captured = {}
    _patch_post(monkeypatch, captured)
    monkeypatch.delenv("ORCH_API_KEY", raising=False)
    monkeypatch.setenv("AGENT_API_KEY", "agent-key")

    cli_dispatch("hello")
    assert captured["headers"]["X-API-Key"] == "agent-key"


def test_dispatch_prefers_orch_api_key(monkeypatch):
    """两个变量都配置时 ORCH_API_KEY 优先。"""
    captured = {}
    _patch_post(monkeypatch, captured)
    monkeypatch.setenv("ORCH_API_KEY", "orch-key")
    monkeypatch.setenv("AGENT_API_KEY", "agent-key")

    cli_dispatch("hello")
    assert captured["headers"]["X-API-Key"] == "orch-key"


def test_dispatch_no_key_no_header(monkeypatch):
    """未配置任何 key：不带 X-API-Key 头（本地环回部署无需鉴权）。"""
    captured = {}
    _patch_post(monkeypatch, captured)
    monkeypatch.delenv("ORCH_API_KEY", raising=False)
    monkeypatch.delenv("AGENT_API_KEY", raising=False)

    cli_dispatch("hello")
    assert "X-API-Key" not in captured["headers"]


def test_main_returns_nonzero_on_failure(monkeypatch, capsys):
    """main：status 非 done 返回 1（脚本退出码契约不变）。"""
    import requests
    import dispatch_cli

    def fake_post(url, json=None, headers=None, timeout=None):
        return _FakeResp({"status": "failed", "error": "boom"})

    monkeypatch.setattr(requests, "post", fake_post)
    monkeypatch.setattr(sys, "argv", ["dispatch_cli.py", "hello"])
    assert dispatch_cli.main() == 1
