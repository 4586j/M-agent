"""API Key 认证中间件。通过 ORCH_API_KEY 环境变量启用。

安全策略（阶段一收敛）：
- 配置了 key：所有非 public 请求须携带正确的 X-API-Key（或 api_key 查询参数）。
- 未配置 key：不再完全开放——默认仅放行环回地址（127.0.0.1/::1 及其双栈映射）
  请求，非环回请求返回 403，并打醒目警告；本机开发不受影响，跨主机访问须配置 key。
- ORCH_ALLOW_UNAUTHENTICATED=1 可显式恢复全开放（兼容旧开发模式与现有测试）。
"""
import ipaddress
import logging
import os

from fastapi import FastAPI
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

log = logging.getLogger(__name__)

# 环回来源白名单：IPv4/IPv6 环回 + localhost 别名；
# "testclient" 是 Starlette TestClient 的默认来源（测试请求视同本机）。
_LOOPBACK_HOSTS = {"127.0.0.1", "::1", "localhost", "testclient"}

_ALLOW_UNAUTH_ENV = "ORCH_ALLOW_UNAUTHENTICATED"
_TRUTHY = ("1", "true", "True", "yes", "on")


def is_loopback(host: str | None) -> bool:
    """判断请求来源 host 是否为环回地址（本机请求豁免鉴权）。

    兼容双栈监听下的 IPv4-mapped IPv6 地址（如 ::ffff:127.0.0.1）：优先按
    IP 字面量解析判定（映射的 IPv4 按其自身环回属性判断）；解析失败
    （localhost/testclient 等别名）回退环回白名单集合。
    """
    if not host:
        return False
    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        return host in _LOOPBACK_HOSTS
    mapped = getattr(addr, "ipv4_mapped", None)
    if mapped is not None:
        return mapped.is_loopback
    return addr.is_loopback


def allow_unauthenticated() -> bool:
    """是否显式开放无鉴权访问（ORCH_ALLOW_UNAUTHENTICATED=1，仅限受控环境）。"""
    return os.environ.get(_ALLOW_UNAUTH_ENV, "") in _TRUTHY


def _client_host(request: Request) -> str | None:
    """取请求来源 host；client 不可得（如部分代理场景）返回 None。"""
    client = getattr(request, "client", None)
    return getattr(client, "host", None) if client is not None else None


class ApiKeyMiddleware(BaseHTTPMiddleware):
    """API Key 校验；loopback_only=True 时（未配置 key）仅放行环回请求。"""

    def __init__(self, app, api_key: str,
                 public_paths: set[str] | None = None,
                 loopback_only: bool = False):
        super().__init__(app)
        self.api_key = api_key
        self.public_paths = public_paths or set()
        self.loopback_only = loopback_only

    async def dispatch(self, request: Request, call_next):
        if request.url.path in self.public_paths or request.url.path.startswith("/static"):
            return await call_next(request)
        if self.loopback_only:
            # 未配置 key：环回豁免；非环回默认拒绝（显式开关已在挂载时拦截）
            if is_loopback(_client_host(request)):
                return await call_next(request)
            return JSONResponse(status_code=403, content={
                "detail": "API key not configured; only loopback access is allowed. "
                          "Set ORCH_API_KEY, or ORCH_ALLOW_UNAUTHENTICATED=1 to override."})
        key = request.headers.get("X-API-Key") or request.query_params.get("api_key")
        if key != self.api_key:
            return JSONResponse(status_code=401, content={"detail": "invalid or missing API key"})
        return await call_next(request)


def add_auth_middleware(app: FastAPI, api_key: str | None,
                        public_paths: set[str] | None = None) -> None:
    """挂载 API Key 中间件（brain_api / worker_api 共用入口）。

    - 配置了 api_key：校验 X-API-Key。
    - 未配置：默认仅放行环回请求（本机开发不受影响），非环回拒绝，
      并打醒目警告；ORCH_ALLOW_UNAUTHENTICATED=1 显式恢复全开放（不挂中间件）。
    """
    if api_key:
        app.add_middleware(ApiKeyMiddleware, api_key=api_key,
                           public_paths=public_paths or set())
        return
    if allow_unauthenticated():
        log.warning("鉴权未启用：ORCH_ALLOW_UNAUTHENTICATED=1 已显式开放无鉴权访问，"
                    "仅限开发/受控环境使用！")
        return
    log.warning(
        "⚠ 鉴权未启用（未设置 ORCH_API_KEY）：默认仅放行环回地址（127.0.0.1/::1），"
        "非环回请求将被拒绝（403）。跨主机访问请设置 ORCH_API_KEY；"
        "如需恢复旧的全开放模式，请显式设置 ORCH_ALLOW_UNAUTHENTICATED=1。")
    app.add_middleware(ApiKeyMiddleware, api_key="",
                       public_paths=public_paths or set(), loopback_only=True)
