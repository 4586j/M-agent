"""大脑侧运行时配置。mode=local 保留 V1 单机行为，mode=remote 启用跨主机。"""
import os
from dataclasses import dataclass
from pathlib import Path

VALID_MODES = ("local", "remote")


def _default_task_store_dir() -> str:
    """基于本文件位置定位 .tasks，避免相对路径随 cwd 漂移到嵌套目录。"""
    return str(Path(__file__).resolve().parent / ".tasks")


@dataclass
class Config:
    mode: str = "local"
    brain_url: str = "http://127.0.0.1:7000"
    heartbeat_interval: int = 15
    heartbeat_timeout: int = 45
    task_store_dir: str = _default_task_store_dir()


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
    )
