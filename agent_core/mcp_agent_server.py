"""单机 Agent MCP 服务。

用 FastMCP（stdio 传输）把本 agent 的执行能力暴露为标准 MCP 工具，让外部 MCP
客户端（Claude Code、其他 agent 运行时）可以把「本单机 agent」当作一个工具调用。

与 runtime/mcp_server.py（大脑服务，经 HTTP 调 brain_api）不同：本服务**直接**在
进程内驱动 ``agent_core.kernel.AgentKernel``，不依赖 brain，是单机自足的执行入口。

启动：python -m agent_core.mcp_agent_server   （stdio 传输）
工具面刻意收敛（8 个），避免工具过多烧 token：
  agent_run              单发执行，返回结果 dict
  agent_session_create   创建多轮会话，返回 {session_id}
  agent_session_send     向会话发一轮消息
  agent_session_close    关闭会话并释放底层子进程（防泄漏）
  agent_memory_list      列出核心记忆名（只读）
  agent_memory_read      读取一条核心记忆（只读）
  agent_tools_list       列出可用工具/技能/MCP
  agent_status           内核状态（活跃会话数、累计用量）

设计约束：SDK 延迟导入（内核内部按需 import）；mcp 为硬依赖（项目已装 1.28.1）。
"""
import asyncio
import logging
import uuid
from dataclasses import asdict
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP

from agent_core.config import AgentConfig, load_agent_config
from agent_core.kernel import AgentKernel
from agent_core.persona import (
    Persona,
    list_persona_names,
    load_persona_by_name,
    parse_subagents,
    strip_frontmatter,
)
from agent_core.tools import load_mcp_servers, scan_skills

logger = logging.getLogger("agent.mcp")

mcp = FastMCP(
    "agent",
    instructions="单机 Agent 执行服务。agent_run 做一次性任务；需要多轮上下文时"
                 "先 agent_session_create 再 agent_session_send。所有产物写入服务"
                 "端分配的工作目录。",
)

# ---- 进程内共享状态 ----

_CONFIG: AgentConfig | None = None
_KERNEL: AgentKernel | None = None
_PERSONAS: dict[str, Persona] = {}
# session_id -> {"session": AgentSession, "cwd": str, "persona": str}
_SESSIONS: dict[str, dict] = {}
_DEFAULT_PERSONA = "executor"


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
    """按名称解析并缓存人设；orchestrator 无定义文件回退大脑人设，
    其余未知名给最小人设兜底并告警提示可用人设名。"""
    if name in _PERSONAS:
        return _PERSONAS[name]
    config = get_config()
    try:
        persona = load_persona_by_name(name, config.persona_dir)
    except FileNotFoundError:
        available = list_persona_names(config.persona_dir)
        if name == "orchestrator":
            logger.warning("persona '%s' 无定义文件，已回退内置大脑人设；可用人设：%s",
                           name, ", ".join(available) or "（无）")
            persona = _build_orchestrator_persona(config)
        else:
            logger.warning(
                "persona '%s' 无定义文件，已使用最小人设兜底；可用人设：%s"
                "（目录：%s）", name, ", ".join(available) or "（无）",
                config.persona_dir)
            persona = Persona(name=name,
                              description=f"{name}（未找到定义文件，使用最小人设）",
                              system_prompt=f"你是 {name}。请按用户指令完成任务。")
    _PERSONAS[name] = persona
    return persona


def _prepare_cwd(tag: str) -> str:
    """在内核 work_dir 下开一个带 tag 的独立子目录作为任务工作目录。"""
    config = get_config()
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
        "cost_usd": info.get("cost_usd"),
        "session_id": session.session_id,
    }


# ---- MCP 工具 ----

@mcp.tool()
async def agent_run(prompt: str, persona: str = "executor",
                    context_memory: str = "") -> dict:
    """单发执行一个任务并返回最终结果（output/turns/usage/cost/artifacts）。

    prompt 是完整任务说明；persona 指定人设（默认 executor）；context_memory
    可传入历史上下文/参考资料。产物写入服务端分配的临时工作目录，路径见
    返回的 artifacts。
    """
    p = resolve_persona(persona or _DEFAULT_PERSONA)
    cwd = _prepare_cwd("mcp-run")
    kernel = get_kernel()
    # run_oneshot 同步阻塞（内部自起事件循环），放线程池避免卡住 MCP 服务 loop
    result = await asyncio.to_thread(kernel.run_oneshot, prompt, p, cwd, context_memory or "")
    return asdict(result)


@mcp.tool()
async def agent_session_create(persona: str = "executor") -> dict:
    """创建一个多轮会话并连接底层客户端，返回 {session_id}。

    需要跨多轮维持上下文时用本工具，随后用 agent_session_send 逐轮对话。
    """
    persona_name = persona or _DEFAULT_PERSONA
    p = resolve_persona(persona_name)
    cwd = _prepare_cwd("mcp-session")
    kernel = get_kernel()
    session = kernel.open_session(p, cwd)
    try:
        await session.connect()
    except Exception as e:  # noqa: BLE001 - 连接失败返回错误 dict（MCP 工具不抛异常）
        return {"status": "error", "detail": f"会话连接失败：{e}"}
    sid = uuid.uuid4().hex[:12]
    _SESSIONS[sid] = {"session": session, "cwd": cwd, "persona": persona_name}
    return {"session_id": sid, "persona": persona_name, "cwd": cwd}


@mcp.tool()
async def agent_session_send(session_id: str, prompt: str) -> dict:
    """向已存在会话发一轮消息，聚合完整响应后返回。"""
    entry = _SESSIONS.get(session_id)
    if entry is None:
        return {"status": "error", "detail": f"session not found: {session_id}"}
    return await _run_session_turn(entry["session"], prompt)


@mcp.tool()
async def agent_session_close(session_id: str) -> dict:
    """关闭一个多轮会话并释放底层 SDK 子进程（会话结束后务必调用，防泄漏）。"""
    entry = _SESSIONS.pop(session_id, None)
    if entry is None:
        return {"status": "error", "detail": f"session not found: {session_id}"}
    try:
        await entry["session"].close()
    except Exception as e:  # noqa: BLE001 - 关闭失败也视为已移除，不让工具抛异常
        return {"status": "closed_with_warning", "session_id": session_id,
                "detail": f"底层连接关闭异常（会话已移除）：{e}"}
    return {"status": "closed", "session_id": session_id}


@mcp.tool()
def agent_memory_list() -> dict:
    """列出核心记忆名（只读，来自 config.memory_core_dir 下的 *.md）。"""
    config = get_config()
    d = Path(config.memory_core_dir)
    names: list[str] = []
    if d.is_dir():
        names = sorted(p.stem for p in d.glob("*.md"))
    return {"memories": names, "count": len(names),
            "backend": config.memory_backend, "dir": str(d)}


@mcp.tool()
async def agent_memory_read(name: str) -> dict:
    """按名读取一条核心记忆的正文（只读）；不存在返回 status=not_found。"""
    from agent_core.tools import _make_local_backend, _make_remote_backend
    config = get_config()
    backend = (_make_remote_backend(config) if config.memory_backend == "remote"
               else _make_local_backend(config))
    try:
        data = await backend["read_core_memory"](name)
    except Exception as e:  # noqa: BLE001 - 后端异常收敛为错误 dict
        return {"status": "error", "detail": f"读取记忆失败：{e}"}
    content = data.get("content") if isinstance(data, dict) else None
    if not content:
        return {"status": "not_found", "name": name,
                "detail": f"记忆 '{name}' 不存在或为空；可用 agent_memory_list 查看全部记忆名"}
    return {"status": "ok", "name": name, "content": content}


@mcp.tool()
def agent_tools_list(persona: str = "executor") -> list:
    """列出指定（或默认）人设的可用工具、技能、MCP 服务与记忆工具。"""
    p = resolve_persona(persona or _DEFAULT_PERSONA)
    config = get_config()
    skills = [s["name"] for s in scan_skills(config.project_root)]
    mcp_servers = list((load_mcp_servers(config.mcp_config_path) or {}).keys())
    tools = list(p.tools) if p.tools else ["all"]
    memory_tools = ["search_experiences", "read_core_memory",
                    "write_core_memory", "write_task_context"]
    return [
        {"category": "persona_tools", "items": tools},
        {"category": "skills", "items": skills},
        {"category": "mcp_servers", "items": mcp_servers},
        {"category": "memory_tools", "items": memory_tools},
    ]


@mcp.tool()
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


def main() -> None:
    """stdio 传输启动 MCP 服务。"""
    mcp.run()


if __name__ == "__main__":
    main()
