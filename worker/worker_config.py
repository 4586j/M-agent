"""worker 配置。"""
import os
from dataclasses import dataclass
from pathlib import Path

_HERE = Path(__file__).resolve().parent


def _env(default: str, *names: str) -> str:
    """按顺序取第一个已设置且非空的环境变量；都没有则返回 default。

    与 agent_core/config.py 同约定：AGENT_ 优先、ORCH_ 兜底。
    """
    for name in names:
        val = os.environ.get(name)
        if val is not None and val != "":
            return val
    return default


def _default_work_dir() -> str:
    """worker 自己的工作区，基于本文件位置，避免相对路径随 cwd 漂移。"""
    return str(_HERE / ".worker_work")


def _default_executor_def() -> str:
    """定位到项目根 .claude/agents/executor.md，避免相对路径随 cwd 漂移。"""
    return str(_HERE.parent / ".claude" / "agents" / "executor.md")


def _default_project_root() -> str:
    """项目根（worker/ 的上一级），executor 据此定位 skills 等资产。"""
    return str(_HERE.parent)


def _default_experience_store_dir() -> str:
    """经验库默认目录：与大脑 runtime/.experiences 同一目录（共享语义）。

    双方各自基于 __file__ 锚定项目根，落点一致且与进程 CWD 无关；worker
    任务沉淀的经验进入大脑经验库，agent_core 的经验检索工具/RAG 双向可
    见。环境变量与 runtime/config.py、agent_core/config.py 同名同义
    （ORCH_EXPERIENCE_STORE_DIR），一处覆盖全局生效。
    """
    return str(_HERE.parent / "workspace" / "agent" / "orchestrator" / "runtime"
               / ".experiences")


@dataclass
class WorkerConfig:
    brain_url: str = "http://127.0.0.1:7000"
    worker_id: str = "worker-local"
    role: str = "executor"
    listen_host: str = "127.0.0.1"
    listen_port: int = 8000
    executor_def_path: str = _default_executor_def()
    work_dir: str = _default_work_dir()
    project_root: str = _default_project_root()
    task_timeout: int = 900
    max_turns: int = 50  # 单任务最大轮次，防失控循环烧 token
    max_thinking_tokens: int = 10000  # 思考 token 上限（最大成本项）；0=不限制
    task_budget_usd: float = 0.0  # 单任务美元硬预算；0=不限制
    mcp_config_path: str = ""  # MCP 服务器配置 JSON 路径；空=不挂载 MCP 工具
    # 经验库目录：默认与大脑 runtime/.experiences 共享（见
    # _default_experience_store_dir 注释）；空串视为未配置，由
    # SdkAgentRunner._get_kernel 退回同一默认，避免落进进程 CWD
    experience_store_dir: str = _default_experience_store_dir()
    heartbeat_interval: int = 15
    # 默认 default（SDK 权限门控生效）；bypassPermissions 须显式设置（仅限开发/受控环境）
    permission_mode: str = "default"
    auto_register: bool = True
    api_key: str = ""
    artifact_max_bytes: int = 20 * 1024 * 1024
    # 输出质量守卫三配置（与 AgentConfig 同名同义）：此前集群模式下未
    # 透传，guard 无法关闭、fallback_model 不生效；AGENT_ 优先 ORCH_ 兜底
    output_guard_enabled: bool = True
    output_guard_max_retries: int = 1
    fallback_model: str = ""


def load_worker_config() -> WorkerConfig:
    return WorkerConfig(
        brain_url=os.environ.get("ORCH_BRAIN_URL", "http://127.0.0.1:7000"),
        worker_id=os.environ.get("ORCH_WORKER_ID", "worker-local"),
        role="executor",
        listen_host=os.environ.get("ORCH_WORKER_HOST", "127.0.0.1"),
        listen_port=int(os.environ.get("ORCH_WORKER_PORT", "8000")),
        executor_def_path=os.environ.get("ORCH_EXECUTOR_DEF", _default_executor_def()),
        work_dir=os.environ.get("ORCH_WORK_DIR", _default_work_dir()),
        project_root=os.environ.get("ORCH_PROJECT_ROOT", _default_project_root()),
        task_timeout=int(os.environ.get("ORCH_TASK_TIMEOUT", "900")),
        max_turns=int(os.environ.get("ORCH_MAX_TURNS", "50")),
        max_thinking_tokens=int(os.environ.get("ORCH_MAX_THINKING_TOKENS", "10000")),
        task_budget_usd=float(os.environ.get("ORCH_TASK_BUDGET_USD", "0")),
        mcp_config_path=os.environ.get("ORCH_MCP_CONFIG", ""),
        experience_store_dir=os.environ.get("ORCH_EXPERIENCE_STORE_DIR",
                                            _default_experience_store_dir()),
        heartbeat_interval=int(os.environ.get("ORCH_HEARTBEAT_INTERVAL", "15")),
        # 默认收敛为 default：权限门控生效；bypassPermissions 须显式环境变量指定
        permission_mode=os.environ.get("ORCH_PERMISSION_MODE", "default"),
        auto_register=os.environ.get("ORCH_AUTO_REGISTER", "1") not in ("0", "false", "False"),
        api_key=os.environ.get("ORCH_API_KEY", ""),
        artifact_max_bytes=int(os.environ.get("ORCH_ARTIFACT_MAX_BYTES",
                                              str(20 * 1024 * 1024))),
        # 输出质量守卫三配置：AGENT_ 优先、ORCH_ 兜底（与 agent_core 同约定；
        # 布尔解析同 _to_bool 的 truthy 词表）
        output_guard_enabled=_env(
            "1", "AGENT_OUTPUT_GUARD_ENABLED", "ORCH_OUTPUT_GUARD_ENABLED"
        ).strip().lower() in ("1", "true", "yes", "on"),
        output_guard_max_retries=int(_env(
            "1", "AGENT_OUTPUT_GUARD_MAX_RETRIES",
            "ORCH_OUTPUT_GUARD_MAX_RETRIES")),
        fallback_model=_env("", "AGENT_FALLBACK_MODEL", "ORCH_FALLBACK_MODEL"),
    )
