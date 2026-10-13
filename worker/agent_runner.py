"""AgentRunner：执行体的抽象。Fake 用于测试，Sdk 接 agent_core 统一内核。

本模块只保留 worker 侧的接口契约（TaskRequest / TaskResult / AgentRunner /
FakeAgentRunner / SdkAgentRunner）与向后兼容的 re-export（scan_skills /
build_env_block / load_mcp_servers）；真实 SDK 消费逻辑已下沉到
agent_core.kernel.AgentKernel，SdkAgentRunner 退化为其薄 adapter。
"""
import logging
import sys
import threading  # noqa: F401  保留：worker_api 等历史调用方可能引用本模块命名空间
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

# 项目根 + runtime 目录入 sys.path（worker_api 已注入，此处自足保证直接 import
# 也成立）：agent_core 提供内核/工具，dispatch_common 提供 USAGE_KEYS 等共享常量。
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_RUNTIME_DIR = _PROJECT_ROOT / "workspace" / "agent" / "orchestrator" / "runtime"
for _p in (str(_PROJECT_ROOT), str(_RUNTIME_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from agent_core.tools import scan_skills, load_mcp_servers  # noqa: F401
from agent_core.context import build_env_block  # noqa: F401
from dispatch_common import USAGE_KEYS  # noqa: E402


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
    # 运行时 Provider 四字段（brain 解析后透传；密钥只走内存，不落日志/持久化）：
    # provider 名进 meta 对账；base_url/auth_token 本次任务生效；provider_pool 故障切换候选
    provider: str | None = None
    provider_base_url: str | None = None
    provider_auth_token: str | None = None
    provider_pool: list[dict] | None = None


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
            return TaskResult(req.task_id, "failed", error="forced error for test", meta={"cost_usd": 0})
        out = f"fake 执行完成：{req.prompt}"
        return TaskResult(req.task_id, "done", output=out, artifacts=[], error=None, meta={"cost_usd": 0})


def build_worker_kernel(cfg, mcp_servers: dict | None = None) -> Any:
    """从 WorkerConfig 装配统一 AgentKernel（worker 面板与任务执行共用同一配方）。

    与 SdkAgentRunner._get_kernel 原是同一段 WorkerConfig → AgentConfig 装配，提成
    模块级函数后，worker 面板（agent_panel.KernelPanelExecutor）与 brain 派发的
    任务执行不再各写一份配置映射，保证「同一节点、同一模型/权限/预算/经验库」。

    mcp_servers：预加载的 MCP 服务器 dict（优先于 mcp_config_path，与 worker_api
    现有预加载 - 传递约定一致）；None 时尝试按 mcp_config_path 加载。
    """
    from agent_core.config import AgentConfig
    from agent_core.kernel import AgentKernel
    from worker_config import _default_experience_store_dir

    # 0/None 一律视为「不限制」，与原有成本控制语义一致
    config = AgentConfig(
        max_turns=cfg.max_turns,
        max_thinking_tokens=cfg.max_thinking_tokens if cfg.max_thinking_tokens else None,
        task_budget_usd=cfg.task_budget_usd if cfg.task_budget_usd else None,
        task_timeout=cfg.task_timeout,
        permission_mode=cfg.permission_mode,
        project_root=cfg.project_root,
        mcp_servers=mcp_servers or load_mcp_servers(cfg.mcp_config_path),
        mcp_config_path="",  # 预加载 dict 优先，配置路径清空避免二重加载
        # 经验库目录兜底：缺省时退回 worker 约定默认（与大脑共享的
        # runtime/.experiences，基于 __file__ 锚定，与进程 CWD 无关），
        # 避免 AgentConfig 默认空串导致 ExperienceStore(Path("")) 落到 CWD、
        # 在进程工作目录散落 exp-local-<ts>.json（历史缺陷）
        experience_store_dir=cfg.experience_store_dir or _default_experience_store_dir(),
        # 输出质量守卫三配置透传（此前缺失导致集群下 guard 无法关闭、
        # fallback_model 不生效）
        output_guard_enabled=cfg.output_guard_enabled,
        output_guard_max_retries=cfg.output_guard_max_retries,
        fallback_model=cfg.fallback_model,
        # 运行时 Provider 全局默认 + 故障切换池（persona/任务级覆盖优先）
        provider_base_url=cfg.provider_base_url,
        provider_auth_token=cfg.provider_auth_token,
        provider_pool=cfg.provider_pool,
    )
    return AgentKernel(config)


class SdkAgentRunner(AgentRunner):
    """真实执行：委托 agent_core.kernel.AgentKernel 统一内核。

    保持原有构造函数签名与行为契约，内部用 kernel 替代直接的 SDK 调用：
    - persona 定义文件（默认 .claude/agents/{role}.md，role 即人设名）的
      frontmatter/body 解析交给 persona.load_persona；
    - system prompt / 运行环境块 / 用户消息前缀由 context 模块拼装；
    - SDK query() 消费、超时、取消、usage/cost 收集全部由 kernel 负责；
    - 本类只做 TaskRequest -> kernel 入参、KernelResult -> TaskResult 的映射。
    """

    def __init__(self, work_dir: str, persona_def_path: str,
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
                 provider_base_url: str = "",
                 provider_auth_token: str = "",
                 provider_pool: list[dict] | None = None,
                 kernel: Any = None):
        self.work_dir = Path(work_dir)
        self.work_dir.mkdir(parents=True, exist_ok=True)
        self.persona_def_path = persona_def_path
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
        # 运行时 Provider 全局默认 + 故障切换池（任务/人设覆盖优先）
        self.provider_base_url = provider_base_url
        self.provider_auth_token = provider_auth_token
        self.provider_pool = provider_pool
        self._kernel = kernel  # 可注入（测试替身）；None 时 run() 内延迟自建

    def _get_kernel(self):
        """延迟构建 AgentKernel，避免 __init__ 阶段就触发 SDK/内核导入。"""
        if self._kernel is not None:
            return self._kernel
        from worker_config import WorkerConfig
        cfg = WorkerConfig(
            max_turns=self.max_turns,
            max_thinking_tokens=self.max_thinking_tokens,
            task_budget_usd=self.task_budget_usd,
            task_timeout=self.task_timeout,
            permission_mode=self.permission_mode,
            project_root=self.project_root,
            experience_store_dir=self.experience_store_dir,
            output_guard_enabled=self.output_guard_enabled,
            output_guard_max_retries=self.output_guard_max_retries,
            fallback_model=self.fallback_model,
            provider_base_url=self.provider_base_url,
            provider_auth_token=self.provider_auth_token,
            provider_pool=self.provider_pool,
        )
        self._kernel = build_worker_kernel(cfg, mcp_servers=self.mcp_servers)
        return self._kernel

    def run(self, req: TaskRequest) -> TaskResult:
        from agent_core.persona import load_persona
        from agent_core.kernel import KernelResult

        # 加载 persona（默认 .claude/agents/{role}.md；等价于原 _parse_executor_def）
        from agent_core.persona import replace_persona
        persona = load_persona(self.persona_def_path)
        # 任务级覆盖（四层优先级最高，次之 persona frontmatter）：
        # model 与 provider 三元组一律经 replace_persona 整体复制，
        # 保留未覆盖字段。密钥只进内存 Persona，不落日志/持久化。
        overrides: dict = {}
        if req.model:
            overrides["model"] = req.model
        if req.provider:
            overrides["provider"] = req.provider
        if req.provider_base_url:
            overrides["base_url"] = req.provider_base_url
        if req.provider_auth_token:
            overrides["auth_token"] = req.provider_auth_token
        if req.provider_pool:
            overrides["provider_pool"] = req.provider_pool
        if overrides:
            persona = replace_persona(persona, **overrides)

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
            meta["usage"] = {k: result.usage.get(k) for k in USAGE_KEYS
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
