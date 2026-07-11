"""worker 配置。"""
import os
from dataclasses import dataclass


@dataclass
class WorkerConfig:
    brain_url: str = "http://127.0.0.1:7000"
    worker_id: str = "worker-local"
    role: str = "executor"
    listen_host: str = "127.0.0.1"
    listen_port: int = 8000
    executor_def_path: str = ".claude/agents/executor.md"
    work_dir: str = "workspace/agent/orchestrator/runtime/.worker_work"
    heartbeat_interval: int = 15
    auto_register: bool = True


def load_worker_config() -> WorkerConfig:
    return WorkerConfig(
        brain_url=os.environ.get("ORCH_BRAIN_URL", "http://127.0.0.1:7000"),
        worker_id=os.environ.get("ORCH_WORKER_ID", "worker-local"),
        role="executor",
        listen_host=os.environ.get("ORCH_WORKER_HOST", "127.0.0.1"),
        listen_port=int(os.environ.get("ORCH_WORKER_PORT", "8000")),
        executor_def_path=os.environ.get("ORCH_EXECUTOR_DEF", ".claude/agents/executor.md"),
        work_dir=os.environ.get("ORCH_WORK_DIR", "workspace/agent/orchestrator/runtime/.worker_work"),
        heartbeat_interval=int(os.environ.get("ORCH_HEARTBEAT_INTERVAL", "15")),
        auto_register=os.environ.get("ORCH_AUTO_REGISTER", "1") not in ("0", "false", "False"),
    )
