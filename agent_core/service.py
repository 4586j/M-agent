"""单机 Agent HTTP 服务。

把 ``agent_core`` 内核的 Agent 能力暴露为 HTTP API，供前端 / 脚本 / 其他进程调用。
**不含** brain 注册/心跳逻辑——那是集群 worker 的职责；本服务只专注单机 Agent 执行。

端点：
  POST   /agent/run                    单发执行，返回 KernelResult JSON
  POST   /agent/session                创建多轮会话，返回 {session_id}
  POST   /agent/session/{id}/message   向会话发消息，返回本轮结果 JSON
  DELETE /agent/session/{id}           关闭会话
  GET    /agent/tools                  列出可用工具/技能/MCP
  GET    /agent/status                 内核状态（活跃会话数、累计用量）
  GET    /health                       健康检查

启动：
  python -m agent_core.service --port 8000 --persona executor
环境变量：AGENT_API_KEY（可选，设置后除 /health 外需 X-API-Key 头）；
其余模型/路径/MCP 变量见 agent_core.config.load_agent_config。

设计约束：SDK 延迟导入（内核内部按需 import），FastAPI/uvicorn 为本服务硬依赖。
"""
import argparse
import asyncio
import os
import uuid
from dataclasses import asdict
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from agent_core.config import AgentConfig, load_agent_config
from agent_core.kernel import AgentKernel
from agent_core.persona import Persona, load_persona_by_name, parse_subagents, strip_frontmatter
from agent_core.tools import load_mcp_servers, scan_skills

# ---- 进程内共享状态（单进程服务，模块级缓存即可） ----

_CONFIG: AgentConfig | None = None
_KERNEL: AgentKernel | None = None
_PERSONAS: dict[str, Persona] = {}
# session_id -> {"session": AgentSession, "cwd": str, "persona": str}
_SESSIONS: dict[str, dict] = {}
# 服务级默认人设（--persona 参数）
_DEFAULT_PERSONA = "executor"


# ---- 请求体模型 ----

class RunReq(BaseModel):
    prompt: str
    persona: str | None = None
    context_memory: str | None = None
    cwd: str | None = None


class SessionCreateReq(BaseModel):
    persona: str | None = None
    cwd: str | None = None


class SessionMessageReq(BaseModel):
    prompt: str


# ---- 配置 / 内核 / 人设 装配 ----

def get_config() -> AgentConfig:
    """惰性装配并缓存 AgentConfig。"""
    global _CONFIG
    if _CONFIG is None:
        _CONFIG = load_agent_config()
    return _CONFIG


def get_kernel() -> AgentKernel:
    """惰性创建并缓存 AgentKernel。"""
    global _KERNEL
    if _KERNEL is None:
        _KERNEL = AgentKernel(get_config())
    return _KERNEL


def _build_orchestrator_persona(config: AgentConfig) -> Persona:
    """orchestrator（大脑）人设兜底：SKILL.md 正文 + 子 agent 注册。"""
    skill_md = (Path(config.project_root) / "workspace" / "agent" / "skills"
                / "orchestrator" / "SKILL.md")
    body = "你是 orchestrator，多 agent 编排框架的大脑。"
    if skill_md.is_file():
        body += "\n\n" + strip_frontmatter(skill_md.read_text(encoding="utf-8"))
    return Persona(name="orchestrator",
                   description="编排大脑：拆解目标、派发子 agent、汇总裁决",
                   system_prompt=body,
                   sub_agents=parse_subagents(config.persona_dir))


def resolve_persona(name: str) -> Persona:
    """按名称解析并缓存人设；orchestrator 无定义文件时回退大脑人设，
    其余未知名给最小人设兜底。"""
    if name in _PERSONAS:
        return _PERSONAS[name]
    config = get_config()
    try:
        persona = load_persona_by_name(name, config.persona_dir)
    except FileNotFoundError:
        if name == "orchestrator":
            persona = _build_orchestrator_persona(config)
        else:
            persona = Persona(name=name,
                              description=f"{name}（未找到定义文件，使用最小人设）",
                              system_prompt=f"你是 {name}。请按用户指令完成任务。")
    _PERSONAS[name] = persona
    return persona


def _prepare_cwd(explicit: str | None, tag: str) -> str:
    """确定任务工作目录：显式指定则用之（并确保存在），否则在内核 work_dir
    下开一个带 tag 的独立子目录，避免多次调用的产物互相污染。"""
    config = get_config()
    if explicit:
        p = Path(explicit)
        p.mkdir(parents=True, exist_ok=True)
        return str(p)
    base = Path(config.work_dir or (config.project_root or ".") + "/.agent_work")
    p = base / f"{tag}-{uuid.uuid4().hex[:8]}"
    p.mkdir(parents=True, exist_ok=True)
    return str(p)


async def _run_session_turn(session: Any, prompt: str) -> dict:
    """消费一轮会话事件，聚合为可 JSON 化的结果 dict。"""
    texts: list[str] = []
    info: dict = {}
    async for ev in session.send(prompt):
        if ev.type == "text_delta":
            texts.append(ev.data or "")
        elif ev.type == "result":
            info = ev.data if isinstance(ev.data, dict) else {}
        elif ev.type == "error":
            info = {"is_error": True, "error": ev.data}
    output = info.get("result") or "".join(texts) or "(无输出)"
    return {
        "output": output,
        "turns": info.get("num_turns", 0) or 0,
        "is_error": bool(info.get("is_error") or info.get("error")),
        "usage": info.get("usage"),
        "cost_usd": info.get("cost_usd"),
        "duration_ms": info.get("duration_ms", 0) or 0,
        "session_id": session.session_id,
        "error": info.get("error"),
    }


# ---- API Key 认证中间件（可选） ----

class _ApiKeyMiddleware:
    """极简 ASGI 中间件：设置 AGENT_API_KEY 后校验 X-API-Key 头，/health 放行。"""

    def __init__(self, app, api_key: str):
        self.app = app
        self.api_key = api_key

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        path = scope.get("path", "")
        if path == "/health":
            await self.app(scope, receive, send)
            return
        headers = dict(scope.get("headers") or [])
        key = headers.get(b"x-api-key", b"").decode()
        if key != self.api_key:
            from starlette.responses import JSONResponse
            resp = JSONResponse(status_code=401,
                                content={"detail": "invalid or missing API key"})
            await resp(scope, receive, send)
            return
        await self.app(scope, receive, send)


# ---- 应用工厂 ----

def create_app(default_persona: str = "executor") -> FastAPI:
    """装配 FastAPI 应用。default_persona 为未显式指定人设时的默认值。"""
    global _DEFAULT_PERSONA
    _DEFAULT_PERSONA = default_persona

    app = FastAPI(title="agent_core service", version="1.0.0",
                  description="单机 Agent HTTP 服务（无 brain 注册/心跳）")
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    api_key = os.environ.get("AGENT_API_KEY", "")
    if api_key:
        app.add_middleware(_ApiKeyMiddleware, api_key=api_key)

    @app.get("/health")
    def health() -> dict:
        """健康检查。"""
        return {"status": "ok", "service": "agent_core"}

    @app.post("/agent/run")
    async def agent_run(req: RunReq) -> dict:
        """单发执行：临时工作目录跑一轮，返回 KernelResult JSON。"""
        persona = resolve_persona(req.persona or _DEFAULT_PERSONA)
        cwd = _prepare_cwd(req.cwd, tag="run")
        kernel = get_kernel()
        # run_oneshot 是同步阻塞（内部自起事件循环），放线程池避免卡住服务 loop
        result = await asyncio.to_thread(
            kernel.run_oneshot, req.prompt, persona, cwd, req.context_memory or ""
        )
        return asdict(result)

    @app.post("/agent/session")
    async def agent_session_create(req: SessionCreateReq) -> dict:
        """创建多轮会话并连接底层客户端，返回本地 session_id。"""
        persona_name = req.persona or _DEFAULT_PERSONA
        persona = resolve_persona(persona_name)
        cwd = _prepare_cwd(req.cwd, tag="session")
        kernel = get_kernel()
        session = kernel.open_session(persona, cwd)
        try:
            await session.connect()
        except Exception as e:  # noqa: BLE001 - 连接失败给出可读错误
            raise HTTPException(status_code=500, detail=f"会话连接失败：{e}")
        sid = uuid.uuid4().hex[:12]
        _SESSIONS[sid] = {"session": session, "cwd": cwd, "persona": persona_name}
        return {"session_id": sid, "persona": persona_name, "cwd": cwd}

    @app.post("/agent/session/{session_id}/message")
    async def agent_session_message(session_id: str, req: SessionMessageReq) -> dict:
        """向已存在会话发一轮消息，聚合完整响应后返回。"""
        entry = _SESSIONS.get(session_id)
        if entry is None:
            raise HTTPException(status_code=404, detail="session not found")
        return await _run_session_turn(entry["session"], req.prompt)

    @app.delete("/agent/session/{session_id}")
    async def agent_session_close(session_id: str) -> dict:
        """关闭会话并释放底层连接。"""
        entry = _SESSIONS.pop(session_id, None)
        if entry is None:
            raise HTTPException(status_code=404, detail="session not found")
        try:
            await entry["session"].close()
        except Exception:  # noqa: BLE001 - 关闭失败不影响移除
            pass
        return {"status": "closed", "session_id": session_id}

    @app.get("/agent/tools")
    def agent_tools(persona: str | None = None) -> dict:
        """列出指定（或默认）人设的可用工具、技能、MCP 服务与记忆工具。"""
        p = resolve_persona(persona or _DEFAULT_PERSONA)
        config = get_config()
        skills = [s["name"] for s in scan_skills(config.project_root)]
        mcp = list((load_mcp_servers(config.mcp_config_path) or {}).keys())
        return {
            "persona": p.name,
            "tools": p.tools if p.tools else "all",
            "skills": skills,
            "mcp_servers": mcp,
            "memory_tools": ["search_experiences", "read_core_memory",
                             "write_core_memory", "write_task_context"],
        }

    @app.get("/agent/status")
    def agent_status() -> dict:
        """内核状态：活跃会话数、累计用量、默认人设与模型。"""
        config = get_config()
        total_cost = 0.0
        total_turns = 0
        for entry in _SESSIONS.values():
            s = entry["session"]
            total_cost += getattr(s, "total_cost", 0.0) or 0.0
            total_turns += getattr(s, "total_turns", 0) or 0
        return {
            "active_sessions": len(_SESSIONS),
            "total_cost_usd": round(total_cost, 6),
            "total_turns": total_turns,
            "default_persona": _DEFAULT_PERSONA,
            "model": config.model,
            "memory_backend": config.memory_backend,
        }

    return app


# 模块级 app：供 `uvicorn agent_core.service:app` 直接引用
app = create_app(os.environ.get("AGENT_PERSONA", "executor"))


def main(argv: list[str] | None = None) -> int:
    """命令行入口：python -m agent_core.service --port 8000 --persona executor。"""
    parser = argparse.ArgumentParser(description="单机 Agent HTTP 服务")
    parser.add_argument("--host", default=os.environ.get("AGENT_HOST", "127.0.0.1"),
                        help="监听地址（默认 127.0.0.1）")
    parser.add_argument("--port", type=int,
                        default=int(os.environ.get("AGENT_PORT", "8000")),
                        help="监听端口（默认 8000）")
    parser.add_argument("--persona",
                        default=os.environ.get("AGENT_PERSONA", "executor"),
                        help="默认人设（默认 executor）")
    parser.add_argument("--reload", action="store_true", help="开发热重载")
    args = parser.parse_args(argv)

    import uvicorn

    # 指定 persona 时重建 app（模块级 app 已按 AGENT_PERSONA 装配一次）
    target = create_app(args.persona)
    uvicorn.run(target, host=args.host, port=args.port, reload=args.reload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
