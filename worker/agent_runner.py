"""AgentRunner：执行体的抽象。Fake 用于测试，Sdk 接真实 Agent SDK。"""
import threading
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
    # threading.Event：worker 收到 cancel 时 set，runner 在 turn 边界协作退出
    cancel_event: Any = None


@dataclass
class TaskResult:
    task_id: str
    status: str  # done | failed | cancelled
    output: Any = None
    artifacts: list = field(default_factory=list)
    error: Any = None
    memory_candidates: list = field(default_factory=list)
    meta: dict = field(default_factory=dict)


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


def _run_async(coro):
    """同步执行协程；若当前线程已在事件循环内（如 async 端点直接调用），
    借独立线程起新 loop，避免 asyncio.run 抛 RuntimeError。"""
    import asyncio
    import concurrent.futures
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
        return ex.submit(asyncio.run, coro).result()


def scan_skills(project_root: str) -> list[dict]:
    """扫描 <project_root>/workspace/agent/skills/ 下带 SKILL.md 的技能，
    解析 frontmatter 的 name/description/triggers。加新技能无需改 worker。
    orchestrator 是大脑自身的工作流定义，不属于 executor 的执行能力，排除。"""
    excluded = {"orchestrator"}
    skills_dir = Path(project_root) / "workspace" / "agent" / "skills"
    if not skills_dir.is_dir():
        return []
    skills: list[dict] = []
    for skill_md in sorted(skills_dir.glob("*/SKILL.md")):
        if skill_md.parent.name in excluded:
            continue
        try:
            text = skill_md.read_text(encoding="utf-8")
            meta: dict = {}
            if text.startswith("---"):
                parts = text.split("---", 2)
                if len(parts) >= 3:
                    import yaml
                    meta = yaml.safe_load(parts[1]) or {}
            triggers = meta.get("triggers") or []
            skills.append({
                "name": meta.get("name", skill_md.parent.name),
                "description": (meta.get("description") or "").strip().split("\n")[0],
                "path": str(skill_md.parent.resolve()),
                "triggers": [str(t) for t in triggers][:6],
            })
        except Exception:
            continue  # 单个技能定义损坏不拖垮执行体
    return skills


def build_env_block(project_root: str, skills: list[dict]) -> str:
    """运行环境注入块：告诉 executor 项目根与技能位置（绝对路径）。

    只含**跨任务稳定**的内容（技能清单不随任务变），拼在 system prompt
    末尾可让前缀缓存跨任务复用；任务工作目录是每任务变化的信息，
    必须放用户消息（见 run()），不能进 system prompt。
    executor.md 中的相对路径从任务工作目录出发不可达，以本块为准。
    """
    lines = [
        "",
        "---",
        "## 运行环境（worker 注入，路径以本块为准）",
        f"- 项目根（技能资产所在，只读）：{Path(project_root).resolve()}",
        "- 当前任务的工作目录见任务消息开头的「任务工作目录」，所有产物写到那里",
    ]
    if skills:
        lines.append("- 可用技能（调用时一律用下列绝对路径）：")
        for s in skills:
            trigger_hint = f"（触发词：{'、'.join(s['triggers'])}）" if s["triggers"] else ""
            lines.append(f"  - {s['name']}：{s['path']} —— {s['description']}{trigger_hint}")
    else:
        lines.append("- 可用技能：无（未扫描到带 SKILL.md 的技能目录）")
    return "\n".join(lines)


def load_mcp_servers(path: str) -> dict | None:
    """加载 MCP 服务器配置（ORCH_MCP_CONFIG 指向的 JSON）。

    格式与 .mcp.json 一致：{"mcpServers": {"<名称>": {"command": ..., "args": [...],
    "env": {...}}} 或 {"url": ..., "headers": {...}}}。返回
    {名称: 配置dict}（即 SDK mcp_servers 形状）；文件缺失/为空/格式错返回 None，
    不让 MCP 配置问题拖垮任务执行。
    """
    if not path:
        return None
    p = Path(path)
    if not p.is_file():
        return None
    try:
        import json
        data = json.loads(p.read_text(encoding="utf-8"))
        servers = data.get("mcpServers") or {}
        if not isinstance(servers, dict) or not servers:
            return None
        cleaned = {}
        for name, cfg in servers.items():
            if not isinstance(cfg, dict):
                continue
            if not (cfg.get("command") or cfg.get("url")):
                continue  # 缺 command/url 的条目跳过
            cleaned[name] = cfg
        return cleaned or None
    except Exception:
        return None


class SdkAgentRunner(AgentRunner):
    """真实执行：用 claude-agent-sdk 起 executor 会话。

    解析 executor.md frontmatter（name/description/model/tools/system prompt），
    通过 query() 异步执行后同步收集 ResultMessage。工作目录隔离到 task 子目录。
    会话带墙钟超时（task_timeout）；cancel_event 置位后在 turn 边界协作退出。
    """

    def __init__(self, work_dir: str, executor_def_path: str,
                 permission_mode: str = "bypassPermissions",
                 project_root: str | None = None,
                 task_timeout: int = 900,
                 max_turns: int = 50,
                 max_thinking_tokens: int | None = 10000,
                 task_budget_usd: float | None = None,
                 mcp_servers: dict | None = None):
        self.work_dir = Path(work_dir)
        self.work_dir.mkdir(parents=True, exist_ok=True)
        self.executor_def_path = executor_def_path
        self.permission_mode = permission_mode
        self.project_root = project_root or str(self.work_dir.parent)
        self.task_timeout = task_timeout
        self.max_turns = max_turns
        self.max_thinking_tokens = max_thinking_tokens
        self.task_budget_usd = task_budget_usd
        self.mcp_servers = mcp_servers

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

        full_prompt = f"[任务工作目录（所有产物写到这里）]\n{task_dir.resolve()}"
        if req.context_memory:
            full_prompt += f"\n\n[上下文记忆]\n{req.context_memory}"
        full_prompt += f"\n\n[任务]\n{req.prompt}"

        env_block = build_env_block(self.project_root, scan_skills(self.project_root))
        option_kwargs = dict(
            cwd=str(task_dir),
            model=req.model or meta["model"],
            system_prompt=meta["prompt"] + env_block,
            permission_mode=self.permission_mode,
            max_turns=self.max_turns,
        )
        # 成本控制：思考 token 是最大成本项（业界实测限到 ~10k 可省 30-40%），
        # max_budget_usd 是单任务硬预算熔断；0/None 表示不限制
        if self.max_thinking_tokens:
            option_kwargs["max_thinking_tokens"] = self.max_thinking_tokens
        if self.task_budget_usd:
            option_kwargs["max_budget_usd"] = self.task_budget_usd
        if self.mcp_servers:
            option_kwargs["mcp_servers"] = self.mcp_servers
        options = sdk.ClaudeAgentOptions(**option_kwargs)
        if meta["tools"]:
            options.allowed_tools = [t.strip() for t in meta["tools"].split(",")]

        async def _run():
            outputs: list[str] = []
            turns = 0
            is_error = False
            usage: dict | None = None
            cost_usd: float | None = None
            duration_ms = 0
            async for msg in sdk.query(prompt=full_prompt, options=options):
                if isinstance(msg, sdk.ResultMessage):
                    outputs.append(msg.result or "")
                    turns = getattr(msg, "num_turns", None) or turns
                    is_error = bool(getattr(msg, "is_error", False))
                    # 用量指标（ResultMessage 为累计值，取最后一条即可）
                    u = getattr(msg, "usage", None)
                    if isinstance(u, dict):
                        usage = u
                    c = getattr(msg, "total_cost_usd", None)
                    if c is not None:
                        cost_usd = c
                    duration_ms = int(getattr(msg, "duration_ms", 0) or 0)
                # 协作式取消：在消息边界检查，当前 turn 结束即停
                if req.cancel_event is not None and req.cancel_event.is_set():
                    return "\n".join(outputs), turns, is_error, True, usage, cost_usd, duration_ms
            return "\n".join(outputs), turns, is_error, False, usage, cost_usd, duration_ms

        async def _run_with_timeout():
            return await asyncio.wait_for(_run(), timeout=self.task_timeout)

        try:
            output, turns, is_error, cancelled, usage, cost_usd, duration_ms = \
                _run_async(_run_with_timeout())
        except asyncio.TimeoutError:
            return TaskResult(req.task_id, "failed",
                              error=f"task timeout after {self.task_timeout}s",
                              meta={"timeout": self.task_timeout})
        except Exception as e:
            return TaskResult(req.task_id, "failed", error=f"sdk error: {e}")

        def _base_meta(extra: dict) -> dict:
            m = {"num_turns": turns, **extra}
            if usage:
                m["usage"] = {k: usage.get(k) for k in
                              ("input_tokens", "output_tokens",
                               "cache_read_input_tokens",
                               "cache_creation_input_tokens") if k in usage}
            if cost_usd is not None:
                m["cost_usd"] = cost_usd
            if duration_ms:
                m["duration_ms"] = duration_ms
            return m

        if cancelled:
            return TaskResult(req.task_id, "cancelled",
                              output=output or None, error="cancelled by request",
                              meta=_base_meta({}))
        if is_error:
            return TaskResult(req.task_id, "failed", output=output or None,
                              error="sdk reported is_error",
                              meta=_base_meta({}))

        artifacts = [str(p.relative_to(task_dir)) for p in task_dir.rglob("*") if p.is_file()]
        return TaskResult(req.task_id, "done", output=output or "(无输出)",
                          artifacts=artifacts, error=None,
                          meta=_base_meta({}))

