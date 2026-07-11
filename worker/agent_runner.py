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

class SdkAgentRunner(AgentRunner):
    """真实执行：用 claude-agent-sdk 起 executor 会话。

    解析 executor.md frontmatter（name/description/model/tools/system prompt），
    通过 query() 异步执行后同步收集 ResultMessage。工作目录隔离到 task 子目录。
    """

    def __init__(self, work_dir: str, executor_def_path: str,
                 permission_mode: str = "bypassPermissions"):
        self.work_dir = Path(work_dir)
        self.work_dir.mkdir(parents=True, exist_ok=True)
        self.executor_def_path = executor_def_path
        self.permission_mode = permission_mode

    def _parse_executor_def(self) -> dict:
        """解析 .claude/agents/executor.md 的 frontmatter + body。"""
        text = Path(self.executor_def_path).read_text(encoding="utf-8")
        parts = text.split("---", 2)
        if len(parts) < 3:
            return {"name": "executor", "description": "", "model": None,
                    "tools": None, "prompt": text}
        import yaml
        meta = yaml.safe_load(parts[1]) or {}
        return {
            "name": meta.get("name", "executor"),
            "description": meta.get("description", ""),
            "model": meta.get("model"),
            "tools": meta.get("tools"),  # None=全工具
            "prompt": parts[2].strip(),
        }

    def run(self, req: TaskRequest) -> TaskResult:
        import asyncio
        import claude_agent_sdk as sdk

        meta = self._parse_executor_def()
        task_dir = self.work_dir / req.task_id
        task_dir.mkdir(parents=True, exist_ok=True)

        full_prompt = req.prompt
        if req.context_memory:
            full_prompt = f"[上下文记忆]\n{req.context_memory}\n\n[任务]\n{req.prompt}"

        options = sdk.ClaudeAgentOptions(
            cwd=str(task_dir),
            model=req.model or meta["model"],
            system_prompt=meta["prompt"],
            permission_mode=self.permission_mode,
            max_turns=50,
        )
        if meta["tools"]:
            options.allowed_tools = [t.strip() for t in meta["tools"].split(",")]

        async def _run():
            outputs = []
            async for msg in sdk.query(prompt=full_prompt, options=options):
                if isinstance(msg, sdk.ResultMessage):
                    outputs.append(msg.result or "")
            return "\n".join(outputs)

        try:
            output = asyncio.run(_run())
        except Exception as e:
            return TaskResult(req.task_id, "failed", error=f"sdk error: {e}")

        artifacts = [str(p.relative_to(task_dir)) for p in task_dir.rglob("*") if p.is_file()]
        return TaskResult(req.task_id, "done", output=output or "(无输出)",
                          artifacts=artifacts, error=None)

