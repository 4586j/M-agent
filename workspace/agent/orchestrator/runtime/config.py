"""大脑侧运行时配置。mode=local 保留 V1 单机行为，mode=remote 启用跨主机。"""
import os
from dataclasses import dataclass
from pathlib import Path

VALID_MODES = ("local", "remote")


def _default_task_store_dir() -> str:
    """基于本文件位置定位 .tasks，避免相对路径随 cwd 漂移到嵌套目录。"""
    return str(Path(__file__).resolve().parent / ".tasks")


def _default_experience_store_dir() -> str:
    """基于本文件位置定位 .experiences，避免相对路径随 cwd 漂移到嵌套目录。"""
    return str(Path(__file__).resolve().parent / ".experiences")


def _default_schedule_store_dir() -> str:
    """基于本文件位置定位 .schedules，避免相对路径随 cwd 漂移到嵌套目录。"""
    return str(Path(__file__).resolve().parent / ".schedules")


def _default_memory_dir() -> Path:
    """定位框架约定的记忆目录（skills/orchestrator/memory/），与 V1 主会话共用一套。"""
    return Path(__file__).resolve().parents[2] / "skills" / "orchestrator" / "memory"


@dataclass
class Config:
    mode: str = "local"
    brain_url: str = "http://127.0.0.1:7000"
    heartbeat_interval: int = 15
    heartbeat_timeout: int = 45
    task_store_dir: str = _default_task_store_dir()
    experience_store_dir: str = _default_experience_store_dir()
    schedule_store_dir: str = _default_schedule_store_dir()
    memory_core_dir: str = str(_default_memory_dir() / "core")
    memory_context_dir: str = str(_default_memory_dir() / "context")
    experience_max_count: int = 500  # 经验库容量上限，超出按失败优先/命中少/最旧淘汰
    experience_ttl_days: int = 0  # 经验保留天数，0=不过期
    context_memory_max_chars: int = 8000  # 派发注入 context_memory 的限幅，防上下文膨胀
    security_llm: bool = False  # security-guard LLM 增强（需 brain 侧 claude-agent-sdk）
    max_retries: int = 3
    retry_base_delay: float = 1.0
    sweeper_interval: int = 15
    async_poll_interval: float = 1.0
    async_poll_max: int = 600
    api_key: str = ""


def load_config() -> Config:
    mode = os.environ.get("ORCH_MODE", "local")
    if mode not in VALID_MODES:
        mode = "local"
    return Config(
        mode=mode,
        brain_url=os.environ.get("ORCH_BRAIN_URL", "http://127.0.0.1:7000"),
        heartbeat_interval=int(os.environ.get("ORCH_HEARTBEAT_INTERVAL", "15")),
        heartbeat_timeout=int(os.environ.get("ORCH_HEARTBEAT_TIMEOUT", "45")),
        task_store_dir=os.environ.get("ORCH_TASK_STORE_DIR", _default_task_store_dir()),
        experience_store_dir=os.environ.get("ORCH_EXPERIENCE_STORE_DIR", _default_experience_store_dir()),
        schedule_store_dir=os.environ.get("ORCH_SCHEDULE_STORE_DIR", _default_schedule_store_dir()),
        memory_core_dir=os.environ.get("ORCH_MEMORY_CORE_DIR", str(_default_memory_dir() / "core")),
        memory_context_dir=os.environ.get("ORCH_MEMORY_CONTEXT_DIR", str(_default_memory_dir() / "context")),
        experience_max_count=int(os.environ.get("ORCH_EXPERIENCE_MAX", "500")),
        experience_ttl_days=int(os.environ.get("ORCH_EXPERIENCE_TTL_DAYS", "0")),
        context_memory_max_chars=int(os.environ.get("ORCH_CONTEXT_MEMORY_MAX", "8000")),
        security_llm=os.environ.get("ORCH_SECURITY_LLM", "0") in ("1", "true", "True"),
        max_retries=int(os.environ.get("ORCH_MAX_RETRIES", "3")),
        retry_base_delay=float(os.environ.get("ORCH_RETRY_BASE_DELAY", "1.0")),
        sweeper_interval=int(os.environ.get("ORCH_SWEEPER_INTERVAL", "15")),
        async_poll_interval=float(os.environ.get("ORCH_ASYNC_POLL_INTERVAL", "1.0")),
        async_poll_max=int(os.environ.get("ORCH_ASYNC_POLL_MAX", "600")),
        api_key=os.environ.get("ORCH_API_KEY", ""),
    )
