"""AgentRunner：执行体的抽象。Fake 用于测试，Sdk 接 agent_core 统一内核。

本模块只保留 worker 侧的接口契约（TaskRequest / TaskResult / AgentRunner /
FakeAgentRunner / SdkAgentRunner）与向后兼容的 re-export（scan_skills /
build_env_block / load_mcp_servers）；真实 SDK 消费逻辑已下沉到
agent_core.kernel.AgentKernel，SdkAgentRunner 退化为其薄 adapter。
"""
import logging
import threading  # noqa: F401  保留：worker_api 等历史调用方可能引用本模块命名空间
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

# re-export：保持向后兼容（测试与 orch_cli / worker_api 从本模块 import 这些）
from agent_core.tools import scan_skills, load_mcp_servers  # noqa: F401
from agent_core.context import build_env_block  # noqa: F401


@dataclass
class TaskRequest:
    task_id: str
    prompt: str
    context_memory: str
    model: str | None
    # threading.Event：worker 收到 cancel 时 set，runner 在 turn 边界协作退出
    cancel_event: Any = None
    # 链路追踪 ID：brain 派发 payload 下发，贯穿 worker 日志与结果 callback（顶层字段）
    trace_id: str | None = None


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


class SdkAgentRunner(AgentRunner):
    """真实执行：委托 agent_core.kernel.AgentKernel 统一内核。

    保持原有构造函数签名与行为契约，内部用 kernel 替代直接的 SDK 调用：
    - executor.md 的 frontmatter/body 解析交给 persona.load_persona；
    - system prompt / 运行环境块 / 用户消息前缀由 context 模块拼装；
    - SDK query() 消费、超时、取消、usage/cost 收集全部由 kernel 负责；
    - 本类只做 TaskRequest -> kernel 入参、KernelResult -> TaskResult 的映射。
    """

    def __init__(self, work_dir: str, executor_def_path: str,
                 permission_mode: str = "default",  # 默认收敛：bypassPermissions 须显式传入
                 project_root: str | None = None,
                 task_timeout: int = 900,
                 max_turns: int = 50,
                 max_thinking_tokens: int | None = 10000,
                 task_budget_usd: float | None = None,
                 mcp_servers: dict | None = None,
                 experience_store_dir: str | None = None,
                 output_guard_enabled: bool = True,
                 output_guard_max_retries: int = 1,
                 fallback_model: str = "",
                 kernel: Any = None):
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
        # 经验库目录：None 时 _get_kernel 退回 worker 约定默认（与大脑
        # runtime/.experiences 共享）；显式传入可隔离（如测试用 tmp 目录）
        self.experience_store_dir = experience_store_dir
        # 输出质量守卫三配置（集群模式下经 WorkerConfig 透传进来）
        self.output_guard_enabled = output_guard_enabled
        self.output_guard_max_retries = output_guard_max_retries
        self.fallback_model = fallback_model
        self._kernel = kernel  # 可注入（测试替身）；None 时 run() 内延迟自建

    def _get_kernel(self):
        """延迟构建 AgentKernel，避免 __init__ 阶段就触发 SDK/内核导入。"""
        if self._kernel is not None:
            return self._kernel
        from agent_core.config import AgentConfig
        from agent_core.kernel import AgentKernel
        # 经验库目录兜底：缺省时退回 worker 约定默认（与大脑共享的
        # runtime/.experiences，基于 __file__ 锚定，与进程 CWD 无关），
        # 避免 AgentConfig 默认空串导致 ExperienceStore(Path("")) 落到 CWD、
        # 在进程工作目录散落 exp-local-<ts>.json（历史缺陷）
        from worker_config import _default_experience_store_dir
        config = AgentConfig(
            max_turns=self.max_turns,
            # 0/None 一律视为「不限制」，与原有成本控制语义一致
            max_thinking_tokens=self.max_thinking_tokens if self.max_thinking_tokens else None,
            task_budget_usd=self.task_budget_usd if self.task_budget_usd else None,
            task_timeout=self.task_timeout,
            permission_mode=self.permission_mode,
            project_root=self.project_root,
            mcp_servers=self.mcp_servers,  # 预加载的 dict，优先于 mcp_config_path
            mcp_config_path="",
            experience_store_dir=self.experience_store_dir or _default_experience_store_dir(),
            # 输出质量守卫三配置透传（此前缺失导致集群下 guard 无法关闭、
            # fallback_model 不生效）
            output_guard_enabled=self.output_guard_enabled,
            output_guard_max_retries=self.output_guard_max_retries,
            fallback_model=self.fallback_model,
        )
        self._kernel = AgentKernel(config)
        return self._kernel

    def run(self, req: TaskRequest) -> TaskResult:
        from agent_core.persona import load_persona, Persona
        from agent_core.kernel import KernelResult

        # 加载 executor persona（等价于原 _parse_executor_def）
        persona = load_persona(self.executor_def_path)
        # 请求级 model 覆盖：req.model 优先，其次 executor.md frontmatter
        if req.model:
            persona = Persona(
                name=persona.name, description=persona.description,
                model=req.model, tools=persona.tools,
                system_prompt=persona.system_prompt,
                sub_agents=persona.sub_agents,
            )

        # 任务工作目录隔离（每任务独立子目录，产物扫描以此为根）
        task_dir = self.work_dir / req.task_id
        task_dir.mkdir(parents=True, exist_ok=True)

        # 委托内核执行：system prompt / 用户消息前缀 / 超时 / 取消 / usage 全由 kernel 负责
        kernel = self._get_kernel()
        log.info("[trace=%s] task=%s kernel run start (model=%s)",
                 req.trace_id, req.task_id, persona.model)
        result: KernelResult = kernel.run_oneshot(
            prompt=req.prompt, persona=persona, cwd=str(task_dir),
            context_memory=req.context_memory, cancel_event=req.cancel_event,
        )
        log.info("[trace=%s] task=%s kernel run end (is_error=%s cancelled=%s)",
                 req.trace_id, req.task_id, result.is_error, result.cancelled)

        # 映射 KernelResult -> TaskResult 的 meta（用量/成本/时长可观测）
        meta: dict = {"num_turns": result.turns}
        if result.usage:
            meta["usage"] = {k: result.usage.get(k) for k in
                             ("input_tokens", "output_tokens",
                              "cache_read_input_tokens",
                              "cache_creation_input_tokens")
                             if k in result.usage}
        if result.cost_usd is not None:
            meta["cost_usd"] = result.cost_usd
        if result.duration_ms:
            meta["duration_ms"] = result.duration_ms
        # 阶段四反思回流：kernel 产出的 meta（reflection、plan_status、plan 等）
        # 并入 TaskResult.meta，经 callback 入库 TaskRecord.meta，供 retry_manager
        # 重试时把失败教训注入重试任务的 context
        if result.meta:
            meta.update(result.meta)

        # 协作式取消：kernel 在消息边界检测到 cancel_event 置位后返回 cancelled=True
        if result.cancelled:
            return TaskResult(req.task_id, "cancelled",
                              output=result.output or None,
                              error="cancelled by request", meta=meta)

        # 墙钟超时：kernel 以 error_type="timeout" 精确标识，映射为原有 timeout 错误形状
        if result.error_type == "timeout":
            meta["timeout"] = self.task_timeout
            return TaskResult(req.task_id, "failed",
                              error=f"task timeout after {self.task_timeout}s",
                              meta=meta)

        # SDK 异常：kernel 收敛为 error_type="sdk_error"，透传其 output 作为 error
        if result.error_type == "sdk_error":
            return TaskResult(req.task_id, "failed", error=result.output, meta=meta)

        # ResultMessage 自报 is_error：视为任务失败，保留 output 便于排障
        if result.is_error:
            return TaskResult(req.task_id, "failed",
                              output=result.output or None,
                              error=result.output or "sdk reported is_error",
                              meta=meta)

        return TaskResult(req.task_id, "done",
                          output=result.output or "(无输出)",
                          artifacts=result.artifacts, error=None, meta=meta)
