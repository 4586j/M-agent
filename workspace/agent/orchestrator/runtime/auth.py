"""API Key 认证中间件。通过 ORCH_API_KEY 环境变量启用。未设置时跳过认证（开发模式）。"""
from fastapi import FastAPI
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse


class ApiKeyMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, api_key: str, public_paths: set[str] | None = None):
        super().__init__(app)
        self.api_key = api_key
        self.public_paths = public_paths or set()

    async def dispatch(self, request: Request, call_next):
        if request.url.path in self.public_paths or request.url.path.startswith("/static"):
            return await call_next(request)
        key = request.headers.get("X-API-Key") or request.query_params.get("api_key")
        if key != self.api_key:
            return JSONResponse(status_code=401, content={"detail": "invalid or missing API key"})
        return await call_next(request)


def add_auth_middleware(app: FastAPI, api_key: str | None,
                        public_paths: set[str] | None = None) -> None:
    """挂载 API Key 中间件。api_key 为空时不挂载（开发模式）。"""
    if not api_key:
        return
    app.add_middleware(ApiKeyMiddleware, api_key=api_key, public_paths=public_paths or set())
