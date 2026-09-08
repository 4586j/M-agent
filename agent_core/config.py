"""统一 Agent 配置。

单机 CLI、本地服务、集群 worker、大脑编排共用同一份配置形状，避免各处各写一套。
字段来源合并自 worker/worker_config.py（模型与执行控制、MCP、路径）与
runtime/config.py（记忆目录）。全部从环境变量加载，前缀优先 AGENT_，
向后兼容 ORCH_；缺省值基于本文件位置定位项目根，避免相对路径随 cwd 漂移。
"""
import os
from dataclasses import dataclass
from pathlib import Path

# agent_core/ 的上一级即项目根（本包放在 <project_root>/agent_core/ 下）
_PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _env(default: str, *names: str) -> str:
    """按顺序取第一个已设置且非空的环境变量；都没有则返回 default。

    统一 AGENT_ 优先、ORCH_ 兜底，让新旧环境变量并存时行为可预期。
    """
    for name in names:
        val = os.environ.get(name)
        if val is not None and val != "":
            return val
    return default


def _default_project_root() -> str:
    """项目根：本包所在目录的上一级。executor 据此定位 skills 等资产。"""
    return str(_PROJECT_ROOT)


def _default_work_dir() -> str:
    """内核默认工作区，基于项目根定位，避免相对路径随 cwd 漂移。"""
    return str(_PROJECT_ROOT / ".agent_work")


def _default_persona_dir() -> str:
    """.claude/agents/ 目录：人设（executor / security-guard 等）定义所在。"""
    return str(_PROJECT_ROOT / ".claude" / "agents")


def _default_memory_dir() -> Path:
    """框架约定的记忆根目录，与大脑主会话共用一套。"""
    return (_PROJECT_ROOT / "workspace" / "agent" / "skills"
            / "orchestrator" / "memory")


def _default_runtime_dir() -> Path:
    """大脑运行时目录（经验库等落地位置）。"""
    return _PROJECT_ROOT / "workspace" / "agent" / "orchestrator" / "runtime"


def _default_experience_store_dir() -> str:
    """经验库目录（runtime/.experiences）。"""
    return str(_default_runtime_dir() / ".experiences")


@dataclass
class AgentConfig:
    """统一 Agent 配置。所有字段可从环境变量覆盖（见 load_agent_config）。"""

    # 模型与执行控制
    model: str | None = None
    max_turns: int = 50                 # 单任务最大轮次，防失控循环烧 token
    max_thinking_tokens: int | None = 10000  # 思考 token 上限（最大成本项）；None/0=不限制
    task_budget_usd: float | None = None     # 单任务美元硬预算；None/0=不限制
    task_timeout: int = 900             # 单任务墙钟超时（秒）
    permission_mode: str = "bypassPermissions"
    # 路径
    project_root: str = ""              # 项目根目录
    work_dir: str = ""                  # 任务工作目录
    persona_dir: str = ""               # .claude/agents/ 目录
    # MCP 与工具
    mcp_config_path: str = ""           # ORCH_MCP_CONFIG 指向的 JSON
    mcp_servers: dict | None = None     # 预加载的 MCP 服务器配置（优先于 mcp_config_path）
    # 记忆
    memory_core_dir: str = ""
    memory_context_dir: str = ""
    experience_store_dir: str = ""
    memory_backend: str = "local"       # local | remote
    brain_url: str = "http://127.0.0.1:7000"
    api_key: str = ""
    # 增强功能
    planning_enabled: bool = False
    reflection_enabled: bool = False
    rag_enabled: bool = False
    streaming: bool = True
    auto_record_experience: bool = True


def _to_bool(raw: str, default: bool) -> bool:
    """把环境变量字符串解析为布尔。空串按 default，其余 truthy 词表判定。"""
    if raw == "":
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _to_optional_int(raw: str) -> int | None:
    """解析可选整型：空串或非法值返回 None（表示不限制）。"""
    raw = raw.strip()
    if not raw:
        return None
    try:
        val = int(raw)
    except ValueError:
        return None
    return val or None  # 0 视为不限制


def _to_optional_float(raw: str) -> float | None:
    """解析可选浮点：空串或非法值返回 None；0 视为不限制。"""
    raw = raw.strip()
    if not raw:
        return None
    try:
        val = float(raw)
    except ValueError:
        return None
    return val or None


def load_agent_config(**overrides) -> AgentConfig:
    """从环境变量装配 AgentConfig，overrides 显式传入的键优先级最高。

    环境变量前缀 AGENT_ 优先、ORCH_ 兜底，保证与既有 worker/runtime 配置并存。
    关键字参数 overrides 用于代码内直接指定（如 worker 传入自己的 work_dir）。
    """
    mem_dir = _default_memory_dir()
    cfg = AgentConfig(
        model=_env("", "AGENT_MODEL", "ORCH_MODEL") or None,
        max_turns=int(_env("50", "AGENT_MAX_TURNS", "ORCH_MAX_TURNS")),
        max_thinking_tokens=_to_optional_int(
            _env("10000", "AGENT_MAX_THINKING_TOKENS", "ORCH_MAX_THINKING_TOKENS")),
        task_budget_usd=_to_optional_float(
            _env("", "AGENT_TASK_BUDGET_USD", "ORCH_TASK_BUDGET_USD")),
        task_timeout=int(_env("900", "AGENT_TASK_TIMEOUT", "ORCH_TASK_TIMEOUT")),
        permission_mode=_env("bypassPermissions",
                             "AGENT_PERMISSION_MODE", "ORCH_PERMISSION_MODE"),
        project_root=_env(_default_project_root(),
                          "AGENT_PROJECT_ROOT", "ORCH_PROJECT_ROOT"),
        work_dir=_env(_default_work_dir(), "AGENT_WORK_DIR", "ORCH_WORK_DIR"),
        persona_dir=_env(_default_persona_dir(),
                         "AGENT_PERSONA_DIR", "ORCH_PERSONA_DIR"),
        mcp_config_path=_env("", "AGENT_MCP_CONFIG", "ORCH_MCP_CONFIG"),
        memory_core_dir=_env(str(mem_dir / "core"),
                             "AGENT_MEMORY_CORE_DIR", "ORCH_MEMORY_CORE_DIR"),
        memory_context_dir=_env(str(mem_dir / "context"),
                                "AGENT_MEMORY_CONTEXT_DIR", "ORCH_MEMORY_CONTEXT_DIR"),
        experience_store_dir=_env(_default_experience_store_dir(),
                                  "AGENT_EXPERIENCE_STORE_DIR",
                                  "ORCH_EXPERIENCE_STORE_DIR"),
        memory_backend=_env("local", "AGENT_MEMORY_BACKEND", "ORCH_MODE"),
        brain_url=_env("http://127.0.0.1:7000",
                       "AGENT_BRAIN_URL", "ORCH_BRAIN_URL"),
        api_key=_env("", "AGENT_API_KEY", "ORCH_API_KEY"),
        planning_enabled=_to_bool(_env("", "AGENT_PLANNING_ENABLED"), False),
        reflection_enabled=_to_bool(_env("", "AGENT_REFLECTION_ENABLED"), False),
        rag_enabled=_to_bool(_env("", "AGENT_RAG_ENABLED"), False),
        streaming=_to_bool(_env("1", "AGENT_STREAMING"), True),
        auto_record_experience=_to_bool(
            _env("1", "AGENT_AUTO_RECORD_EXPERIENCE"), True),
    )
    # 显式 overrides 覆盖环境变量结果（None 值也尊重调用方意图，直接赋值）
    for key, value in overrides.items():
        if hasattr(cfg, key):
            setattr(cfg, key, value)
    return cfg
