"""AgentRunner：执行体的抽象。Fake 用于测试，Sdk 接真实 Agent SDK。"""
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class TaskRequest:
    task_id: str
    prompt: str
    context_memory: str
    model: str | None


@dataclass
class TaskResult:
    task_id: str
    status: str  # done | failed
    output: Any = None
    artifacts: list = field(default_factory=list)
    error: Any = None
    memory_candidates: list = field(default_factory=list)


class AgentRunner(ABC):
    @abstractmethod
    def run(self, req: TaskRequest) -> TaskResult: ...


class FakeAgentRunner(AgentRunner):
    """测试用：prompt 含 FORCE_ERROR 则失败，否则 done。"""

    def __init__(self, work_dir: str):
        self.work_dir = Path(work_dir)
        self.work_dir.mkdir(parents=True, exist_ok=True)

    def run(self, req: TaskRequest) -> TaskResult:
        if "FORCE_ERROR" in req.prompt:
            return TaskResult(req.task_id, "failed", error="forced error for test")
        out = f"fake 执行完成：{req.prompt}"
        return TaskResult(req.task_id, "done", output=out, artifacts=[], error=None)


class SdkAgentRunner(AgentRunner):
    """真实执行：用 claude-agent-sdk 加载 executor.md 起会话。

    V2 骨架：真实 SDK 接入需根据具体 SDK 版本实现 query() 调用与流式产出收集，
    在集成环境验证。单测不覆盖此实现。
    """

    def __init__(self, work_dir: str, executor_def_path: str):
        self.work_dir = work_dir
        self.executor_def_path = executor_def_path

    def run(self, req: TaskRequest) -> TaskResult:
        raise NotImplementedError(
            "SdkAgentRunner 待接入 claude-agent-sdk；当前用 FakeAgentRunner 验证协议"
        )
