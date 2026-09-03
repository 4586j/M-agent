"""worker 配置。"""
import os
from dataclasses import dataclass
from pathlib import Path

_HERE = Path(__file__).resolve().parent


def _default_work_dir() -> str:
    """worker 自己的工作区，基于本文件位置，避免相对路径随 cwd 漂移。"""
    return str(_HERE / ".worker_work")


def _default_executor_def() -> str:
    """定位到项目根 .claude/agents/executor.md，避免相对路径随 cwd 漂移。"""
    return str(_HERE.parent / ".claude" / "agents" / "executor.md")


@dataclass
class WorkerConfig:
    brain_url: str = "http://127.0.0.1:7000"
    worker_id: str = "worker-local"
    role: str = "executor"
    listen_host: str = "127.0.0.1"
    listen_port: int = 8000
    executor_def_path: str = _default_executor_def()
    work_dir: str = _default_work_dir()
    heartbeat_interval: int = 15
    permission_mode: str = "bypassPermissions"
    auto_register: bool = True
    api_key: str = ""


def load_worker_config() -> WorkerConfig:
    return WorkerConfig(
        brain_url=os.environ.get("ORCH_BRAIN_URL", "http://127.0.0.1:7000"),
        worker_id=os.environ.get("ORCH_WORKER_ID", "worker-local"),
        role="executor",
        listen_host=os.environ.get("ORCH_WORKER_HOST", "127.0.0.1"),
        listen_port=int(os.environ.get("ORCH_WORKER_PORT", "8000")),
        executor_def_path=os.environ.get("ORCH_EXECUTOR_DEF", _default_executor_def()),
        work_dir=os.environ.get("ORCH_WORK_DIR", _default_work_dir()),
        heartbeat_interval=int(os.environ.get("ORCH_HEARTBEAT_INTERVAL", "15")),
        permission_mode=os.environ.get("ORCH_PERMISSION_MODE", "bypassPermissions"),
        auto_register=os.environ.get("ORCH_AUTO_REGISTER", "1") not in ("0", "false", "False"),
        api_key=os.environ.get("ORCH_API_KEY", ""),
    )
