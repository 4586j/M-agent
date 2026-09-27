"""测试 auth：有 key 校验、无 key 环回豁免/远程拒绝、显式开关恢复全开放。

不依赖 TestClient 的网络栈：直接构造 Request 调中间件 dispatch，
用假 call_next 断言放行/拒绝，聚焦中间件自身的裁决逻辑。
"""
import asyncio
import sys
from pathlib import Path

from starlette.requests import Request
from starlette.responses import PlainTextResponse

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from auth import (ApiKeyMiddleware, add_auth_middleware, allow_unauthenticated,
                  is_loopback)


def _make_request(host: str | None = "127.0.0.1", path: str = "/workers",
                  headers: dict[str, str] | None = None) -> Request:
    """构造带 client host 的最小 Request。"""
    raw = [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()]
    scope = {
        "type": "http", "method": "GET", "path": path,
        "headers": raw, "query_string": b"",
        "client": (host, 50000) if host else None,
    }
    return Request(scope)


async def _call_next(request):
    return PlainTextResponse("ok")


def _run(mw: ApiKeyMiddleware, request: Request):
    return asyncio.run(mw.dispatch(request, _call_next))


# ---- is_loopback ----

def test_is_loopback():
    assert is_loopback("127.0.0.1") is True
    assert is_loopback("::1") is True
    assert is_loopback("localhost") is True
    assert is_loopback("192.168.1.5") is False
    assert is_loopback("10.0.0.9") is False
    assert is_loopback(None) is False
    assert is_loopback("") is False


def test_is_loopback_dual_stack_mapped_ipv4():
    """双栈监听下的 IPv4-mapped IPv6 环回地址（如 ::ffff:127.0.0.1）应豁免。"""
    assert is_loopback("::ffff:127.0.0.1") is True
    assert is_loopback("::ffff:7f00:1") is True    # 同一地址的十六进制写法
    assert is_loopback("::FFFF:127.0.0.1") is True  # 大小写不敏感
    # 映射的非环回 IPv4 仍拒绝
    assert is_loopback("::ffff:192.168.1.5") is False
    assert is_loopback("::ffff:10.0.0.9") is False
    # 非 IP 字面量回退白名单集合；无法解析的伪映射地址拒绝
    assert is_loopback("testclient") is True
    assert is_loopback("::ffff:not-an-ip") is False


# ---- 有 key：X-API-Key 校验 ----

def test_with_key_correct_passes():
    mw = ApiKeyMiddleware(app=None, api_key="secret")
    assert _run(mw, _make_request(headers={"X-API-Key": "secret"})).status_code == 200


def test_with_key_wrong_or_missing_401():
    mw = ApiKeyMiddleware(app=None, api_key="secret")
    assert _run(mw, _make_request(headers={"X-API-Key": "wrong"})).status_code == 401
    assert _run(mw, _make_request()).status_code == 401


def test_public_paths_and_static_bypass():
    mw = ApiKeyMiddleware(app=None, api_key="secret", public_paths={"/health"})
    assert _run(mw, _make_request(path="/health")).status_code == 200
    assert _run(mw, _make_request(path="/static/app.js")).status_code == 200


# ---- 无 key：默认仅放行环回，远程拒绝 ----

def test_without_key_loopback_allowed_remote_rejected():
    mw = ApiKeyMiddleware(app=None, api_key="", loopback_only=True)
    assert _run(mw, _make_request(host="127.0.0.1")).status_code == 200
    assert _run(mw, _make_request(host="::1")).status_code == 200
    assert _run(mw, _make_request(host="localhost")).status_code == 200
    # 双栈映射环回同样豁免（修复前 ::ffff:127.0.0.1 会被误拒）
    assert _run(mw, _make_request(host="::ffff:127.0.0.1")).status_code == 200
    # 非环回与来源不可得一概拒绝
    assert _run(mw, _make_request(host="192.168.1.5")).status_code == 403
    assert _run(mw, _make_request(host="10.0.0.9")).status_code == 403
    assert _run(mw, _make_request(host=None)).status_code == 403


def test_without_key_rejection_message_mentions_override():
    mw = ApiKeyMiddleware(app=None, api_key="", loopback_only=True)
    resp = _run(mw, _make_request(host="10.0.0.9"))
    assert "ORCH_ALLOW_UNAUTHENTICATED" in resp.body.decode()


# ---- 显式开关：ORCH_ALLOW_UNAUTHENTICATED=1 恢复全开放 ----

def test_allow_unauthenticated_flag_parsing(monkeypatch):
    monkeypatch.delenv("ORCH_ALLOW_UNAUTHENTICATED", raising=False)
    assert allow_unauthenticated() is False
    monkeypatch.setenv("ORCH_ALLOW_UNAUTHENTICATED", "1")
    assert allow_unauthenticated() is True
    monkeypatch.setenv("ORCH_ALLOW_UNAUTHENTICATED", "0")
    assert allow_unauthenticated() is False


def test_add_auth_middleware_no_key_mounts_loopback_only(monkeypatch):
    """未配置 key 且未开显式开关：挂载环回豁免中间件（不再完全开放）。"""
    from fastapi import FastAPI
    monkeypatch.delenv("ORCH_ALLOW_UNAUTHENTICATED", raising=False)
    app = FastAPI()
    add_auth_middleware(app, api_key=None)
    assert len(app.user_middleware) == 1
    assert app.user_middleware[0].kwargs.get("loopback_only") is True


def test_add_auth_middleware_allow_flag_skips_middleware(monkeypatch):
    """ORCH_ALLOW_UNAUTHENTICATED=1：不挂中间件（兼容旧开发模式与现有测试）。"""
    from fastapi import FastAPI
    monkeypatch.setenv("ORCH_ALLOW_UNAUTHENTICATED", "1")
    app = FastAPI()
    add_auth_middleware(app, api_key=None)
    assert len(app.user_middleware) == 0


def test_add_auth_middleware_with_key_mounts_apikey_check(monkeypatch):
    from fastapi import FastAPI
    app = FastAPI()
    add_auth_middleware(app, api_key="secret")
    assert len(app.user_middleware) == 1
    assert app.user_middleware[0].kwargs.get("api_key") == "secret"
