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


@dataclass
class Config:
    mode: str = "local"
    brain_url: str = "http://127.0.0.1:7000"
    heartbeat_interval: int = 15
    heartbeat_timeout: int = 45
    task_store_dir: str = _default_task_store_dir()
    experience_store_dir: str = _default_experience_store_dir()
    schedule_store_dir: str = _default_schedule_store_dir()
    security_llm: bool = False  # security-guard LLM 增强（需 brain 侧 claude-agent-sdk）
    max_retries: int = 3
    retry_base_delay: float = 1.0
    sweeper_interval: int = 15
    async_poll_interval: float = 1.0
    async_poll_max: int = 600


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
        security_llm=os.environ.get("ORCH_SECURITY_LLM", "0") in ("1", "true", "True"),
        max_retries=int(os.environ.get("ORCH_MAX_RETRIES", "3")),
        retry_base_delay=float(os.environ.get("ORCH_RETRY_BASE_DELAY", "1.0")),
        sweeper_interval=int(os.environ.get("ORCH_SWEEPER_INTERVAL", "15")),
        async_poll_interval=float(os.environ.get("ORCH_ASYNC_POLL_INTERVAL", "1.0")),
        async_poll_max=int(os.environ.get("ORCH_ASYNC_POLL_MAX", "600")),
    )
