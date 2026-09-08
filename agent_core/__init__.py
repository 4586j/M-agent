"""agent_core：统一 Agent 内核，单机CLI/本地服务/集群worker/大脑编排共用。"""
from .config import AgentConfig, load_agent_config
from .persona import Persona, load_persona, parse_subagents
from .context import build_system_prompt, build_env_block, load_core_memories_block
from .tools import scan_skills, load_mcp_servers, build_memory_tools
from .kernel import AgentKernel, KernelResult, AgentSession, AgentEvent

__all__ = [
    "AgentConfig", "load_agent_config",
    "Persona", "load_persona", "parse_subagents",
    "build_system_prompt", "build_env_block", "load_core_memories_block",
    "scan_skills", "load_mcp_servers", "build_memory_tools",
    "AgentKernel", "KernelResult", "AgentSession", "AgentEvent",
]
