"""AgentKernel：统一 agent 执行内核。单机 CLI、本地服务、集群 worker 共用。

对外暴露两种运行方式：
- run_oneshot(): 单发任务执行（底层 sdk.query()），同步阻塞返回 KernelResult
- open_session(): 多轮交互会话（底层 ClaudeSDKClient），返回 AgentSession
- stream(): 异步流式执行，边消费边 yield AgentEvent，供 CLI 打印与 HTTP SSE 使用

设计约束：
- SDK 延迟导入（函数内 ``import claude_agent_sdk as sdk``），避免包级强依赖；
- 消息类型判断一律用 ``type(msg).__name__`` 弱耦合（与 orch_cli.py 风格一致），
  不直接 isinstance 绑定 SDK 具体类，降低 SDK 版本升级的破坏面。
"""
import asyncio
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, AsyncIterator

from agent_core.config import AgentConfig
from agent_core.context import build_system_prompt, build_user_message_prefix
from agent_core.persona import Persona
from agent_core.tools import build_memory_tools, load_mcp_servers, scan_skills


@dataclass
class AgentEvent:
    """流式事件。

    type 取值：
    - ``text_delta``：模型输出的一段文本（data 为 str）
    - ``tool_use``：模型发起工具调用（data 为 {"name": ..., "input": ...}）
    - ``tool_result``：工具执行结果回填（data 为结果内容）
    - ``result``：本轮/本任务终结（data 为统计 dict）
    - ``error``：执行异常（data 为错误字符串）
    """

    type: str  # "text_delta" | "tool_use" | "tool_result" | "result" | "error"
    data: Any = None


@dataclass
class KernelResult:
    """统一执行结果。run_oneshot 的返回值，吸收 usage/cost/duration 等指标。"""

    output: str = ""
    turns: int = 0
    is_error: bool = False
    usage: dict | None = None
    cost_usd: float | None = None
    duration_ms: int = 0
    session_id: str | None = None
    artifacts: list[str] = field(default_factory=list)
    cancelled: bool = False
    # 错误类型标识："timeout"（墙钟超时）| "sdk_error"（SDK 异常）| ""（无）。
    # 供上层 adapter 精确映射超时与一般错误，避免靠 output 文本猜测。
    error_type: str = ""


def _msg_to_events(msg: Any) -> list[AgentEvent]:
    """把一条 SDK 消息翻译成 0..n 个 AgentEvent。

    stream() 与 AgentSession.send() 共用，保证单发流式与多轮会话的事件语义一致。
    用 ``type(...).__name__`` 做弱耦合判断，兼容不同 SDK 版本的类命名。
    """
    events: list[AgentEvent] = []
    name = type(msg).__name__
    if name == "AssistantMessage":
        for block in getattr(msg, "content", []) or []:
            kind = type(block).__name__
            if kind == "TextBlock":
                events.append(AgentEvent("text_delta", getattr(block, "text", "") or ""))
            elif kind == "ToolUseBlock":
                events.append(AgentEvent("tool_use", {
                    "name": getattr(block, "name", None),
                    "input": getattr(block, "input", None),
                }))
    elif name == "UserMessage":
        # 工具执行结果通常以 UserMessage(ToolResultBlock) 形式回流
        for block in getattr(msg, "content", []) or []:
            if type(block).__name__ == "ToolResultBlock":
                events.append(AgentEvent("tool_result", getattr(block, "content", None)))
    elif name == "ResultMessage":
        events.append(AgentEvent("result", {
            "result": getattr(msg, "result", None),
            "num_turns": getattr(msg, "num_turns", 0) or 0,
            "is_error": bool(getattr(msg, "is_error", False)),
            "usage": getattr(msg, "usage", None),
            "cost_usd": getattr(msg, "total_cost_usd", None),
            "duration_ms": int(getattr(msg, "duration_ms", 0) or 0),
            "session_id": getattr(msg, "session_id", None),
        }))
    return events


class AgentSession:
    """多轮交互会话。包装 ClaudeSDKClient，维持 session_id 与累计用量。"""

    def __init__(self, kernel: "AgentKernel", persona: Persona, cwd: str):
        self._kernel = kernel
        self._persona = persona
        self._cwd = cwd
        self._client: Any = None
        self._session_id: str | None = None
        self._total_cost: float = 0.0
        self._total_turns: int = 0

    async def connect(self) -> None:
        """建立底层 SDK 客户端连接。可携带已有 session_id 续接历史会话。"""
        import claude_agent_sdk as sdk

        options = self._kernel._build_options(
            self._persona, self._cwd, resume_session_id=self._session_id
        )
        self._client = sdk.ClaudeSDKClient(options=options)
        await self._client.connect()

    async def send(self, prompt: str) -> AsyncIterator[AgentEvent]:
        """发送一轮消息并流式 yield AgentEvent。

        ResultMessage 到达时更新累计 session_id / turns / cost。
        """
        if self._client is None:
            raise RuntimeError("会话未连接，请先 await connect()")
        async for msg in self._client.query(prompt=prompt, session_id=self._session_id):
            for ev in _msg_to_events(msg):
                if ev.type == "result" and isinstance(ev.data, dict):
                    d = ev.data
                    if d.get("session_id"):
                        self._session_id = d["session_id"]
                    self._total_turns += d.get("num_turns") or 0
                    if d.get("cost_usd") is not None:
                        self._total_cost += d["cost_usd"]
                yield ev

    async def interrupt(self) -> None:
        """中断当前正在执行的一轮（协作式，当前 turn 边界停下）。"""
        if self._client is not None:
            await self._client.interrupt()

    async def close(self) -> None:
        """断开底层客户端连接。"""
        if self._client is not None:
            await self._client.disconnect()
            self._client = None

    @property
    def session_id(self) -> str | None:
        return self._session_id

    @property
    def total_cost(self) -> float:
        return self._total_cost

    @property
    def total_turns(self) -> int:
        return self._total_turns


class AgentKernel:
    """统一 Agent 执行内核。

    对外暴露三种运行方式：
    - run_oneshot(): 单发任务执行（底层 sdk.query()），同步返回 KernelResult
    - open_session(): 多轮交互会话（底层 ClaudeSDKClient），返回 AgentSession
    - stream(): 异步流式执行，yield AgentEvent
    """

    def __init__(self, config: AgentConfig):
        self.config = config

    def _build_options(self, persona: Persona, cwd: str,
                       resume_session_id: str | None = None) -> Any:
        """构造 SDK ClaudeAgentOptions。

        组装：system_prompt（context.build_system_prompt）、model、cwd、
        permission_mode、max_turns、max_thinking_tokens、max_budget_usd、
        mcp_servers（tools.load_mcp_servers）、agents（persona.sub_agents）、
        tools（memory_tools）、resume。
        model 优先 persona.model，其次 config.model；
        max_thinking_tokens / max_budget_usd 为 0/None 时不传（表示不限制）。
        """
        import claude_agent_sdk as sdk

        skills = scan_skills(self.config.project_root)
        system_prompt = build_system_prompt(
            persona, self.config.project_root, self.config.memory_core_dir, skills,
            memory_backend=self.config.memory_backend,
        )
        model = persona.model or self.config.model

        kwargs: dict[str, Any] = dict(
            cwd=cwd,
            system_prompt=system_prompt,
            permission_mode=self.config.permission_mode,
            max_turns=self.config.max_turns,
        )
        if model:
            kwargs["model"] = model
        # 成本控制：思考 token 是最大成本项；max_budget_usd 是单任务硬预算熔断
        if self.config.max_thinking_tokens:
            kwargs["max_thinking_tokens"] = self.config.max_thinking_tokens
        if self.config.task_budget_usd:
            kwargs["max_budget_usd"] = self.config.task_budget_usd

        mcp_servers = self.config.mcp_servers or load_mcp_servers(self.config.mcp_config_path)
        if mcp_servers:
            kwargs["mcp_servers"] = mcp_servers
        if persona.sub_agents:
            kwargs["agents"] = persona.sub_agents
        # 记忆工具构建可能因 runtime 不可达/依赖缺失而报错，不能拖垮整个选项构建
        try:
            memory_tools = build_memory_tools(self.config)
            if memory_tools:
                kwargs["tools"] = memory_tools
        except Exception:  # noqa: BLE001 - 记忆工具失败不阻断主执行
            pass
        if resume_session_id:
            kwargs["resume"] = resume_session_id

        options = sdk.ClaudeAgentOptions(**kwargs)
        # persona 显式限定工具集时收窄 allowed_tools（None=全工具）
        if persona.tools:
            options.allowed_tools = [str(t).strip() for t in persona.tools]
        return options

    def run_oneshot(self, prompt: str, persona: Persona, cwd: str,
                    context_memory: str = "",
                    cancel_event: Any = None) -> KernelResult:
        """同步单发执行。

        内部调用 _run_async(_execute_oneshot(...))，并施加墙钟超时（task_timeout）。
        - asyncio.wait_for 超时控制；
        - cancel_event 协作式取消（在消息边界检查，见 _execute_oneshot）；
        - 收集 ResultMessage 的 usage/cost/duration；
        - 成功完成后扫描 cwd 下的文件作为 artifacts。
        """
        async def _run_with_timeout() -> KernelResult:
            return await asyncio.wait_for(
                self._execute_oneshot(prompt, persona, cwd, context_memory, cancel_event),
                timeout=self.config.task_timeout,
            )

        try:
            result = self._run_async(_run_with_timeout())
        except asyncio.TimeoutError:
            return KernelResult(
                output=f"任务超时（>{self.config.task_timeout}s）", is_error=True,
                duration_ms=self.config.task_timeout * 1000, error_type="timeout",
            )
        except Exception as e:  # noqa: BLE001 - SDK 异常统一收敛为错误结果
            return KernelResult(output=f"sdk error: {e}", is_error=True,
                                error_type="sdk_error")

        # artifacts 收集：仅在未取消/未报错且 cwd 存在时扫描产物文件
        if not result.cancelled and not result.is_error:
            cwd_path = Path(cwd)
            if cwd_path.is_dir():
                result.artifacts = [
                    str(p.relative_to(cwd_path))
                    for p in cwd_path.rglob("*") if p.is_file()
                ]
        # 经验自动沉淀（config.auto_record_experience 控制）：失败静默，不拖垮主流程
        if self.config.auto_record_experience and not result.cancelled:
            self._record_experience(prompt, persona, result, cwd)
        # 规划模式后处理：尝试从输出解析结构化计划并持久化（默认关闭，失败静默）
        result = self._apply_planning(prompt, result, cwd)
        # 自我反思：任务成功后额外一轮质量自评（默认关闭，失败静默）
        reflection = self._run_reflection(result, persona, cwd)
        if reflection:
            try:
                self._write_reflection_to_memory(result.session_id, reflection)
            except Exception:  # noqa: BLE001 - 反思写入失败不影响任务结果
                pass
        return result

    def _apply_planning(self, prompt: str, result: KernelResult,
                        cwd: str) -> KernelResult:
        """规划模式后处理：尝试从输出中解析计划并持久化。

        仅在 config.planning_enabled 为真时生效；解析不到计划或任何异常都静默，
        原样返回 result，不影响主流程。
        """
        if not self.config.planning_enabled:
            return result
        try:
            from agent_core.planning import PlanningManager, parse_plan_from_output
            plan = parse_plan_from_output(result.output)
            if plan:
                progress_dir = Path(self.config.project_root) / ".orch_progress"
                mgr = PlanningManager(progress_dir)
                session_id = result.session_id or f"task-{int(time.time())}"
                mgr.save_plan(session_id, plan)
        except Exception:  # noqa: BLE001 - 规划后处理失败不影响任务结果
            pass
        return result

    def _run_reflection(self, result: KernelResult, persona: Persona,
                        cwd: str) -> str | None:
        """自我反思：任务成功后用额外一轮评估产出质量。返回反思文本或 None。

        仅在 config.reflection_enabled 且任务未报错/未取消时触发；用同一 persona
        做一次单轮轻量查询，任何异常都静默返回 None，不拖垮主流程。
        """
        if not self.config.reflection_enabled or result.is_error or result.cancelled:
            return None
        try:
            from agent_core.planning import build_reflection_prompt
            # 限幅避免反思输入过长烧 token
            reflection_prompt = build_reflection_prompt(result.output[:4000])
            options = self._build_options(persona, cwd)
            # 覆盖为单轮：反思不需要多轮工具循环
            try:
                options.max_turns = 1
            except Exception:  # noqa: BLE001 - 某些 options 实现可能只读
                pass
            reflection_result = self._run_async(
                self._execute_reflection(reflection_prompt, options))
            return reflection_result or None
        except Exception:  # noqa: BLE001 - 反思失败不影响任务结果
            return None

    async def _execute_reflection(self, prompt: str, options: Any) -> str:
        """执行反思查询，仅收集文本输出（ResultMessage.result）。"""
        import claude_agent_sdk as sdk
        texts: list[str] = []
        async for msg in sdk.query(prompt=prompt, options=options):
            if type(msg).__name__ == "ResultMessage":
                r = getattr(msg, "result", None)
                if r:
                    texts.append(r)
        return "\n".join(texts) if texts else ""

    def _write_reflection_to_memory(self, session_id: str | None,
                                    reflection: str) -> None:
        """把反思文本写入任务上下文记忆（memory_context_dir/<session_id>/reflection.md）。

        延迟导入 runtime 的 FileMemoryStore，与 _record_experience 一致的解耦姿态；
        无 session_id 或任何异常都静默跳过。
        """
        if not session_id:
            return
        import sys
        rt = (Path(self.config.project_root) / "workspace" / "agent"
              / "orchestrator" / "runtime")
        if str(rt) not in sys.path:
            sys.path.insert(0, str(rt))
        from memory_store import FileMemoryStore
        mem = FileMemoryStore(self.config.memory_core_dir,
                              self.config.memory_context_dir)
        mem.write_context(session_id, "reflection", reflection)

    def _record_experience(self, prompt: str, persona: Persona,
                           result: KernelResult, cwd: str) -> None:
        """任务完成后自动沉淀经验到 experience_store。失败静默，不拖垮主流程。

        延迟导入 runtime 的 ExperienceStore（把 runtime 目录加入 sys.path），
        避免 agent_core 对 runtime 形成包级强依赖。cwd 暂未参与记录，保留形参
        以便未来按工作目录归类经验。
        """
        try:
            import sys
            rt = (Path(self.config.project_root) / "workspace" / "agent"
                  / "orchestrator" / "runtime")
            if str(rt) not in sys.path:
                sys.path.insert(0, str(rt))
            from experience_store import ExperienceStore

            exp_store = ExperienceStore(self.config.experience_store_dir)
            exp_store.record_from_task(
                task_id=result.session_id or f"local-{int(time.time())}",
                role=persona.name,
                prompt=prompt,
                output=result.output,
                success=not result.is_error,
                verdict=None,
                artifacts=result.artifacts,
                created_at=str(int(time.time())),
            )
        except Exception:  # noqa: BLE001 - 经验沉淀失败不影响任务结果
            pass

    async def _execute_oneshot(self, prompt: str, persona: Persona, cwd: str,
                               context_memory: str,
                               cancel_event: Any) -> KernelResult:
        """异步执行核心：消费 sdk.query() 的消息流，聚合为 KernelResult。"""
        import claude_agent_sdk as sdk

        options = self._build_options(persona, cwd)
        # 前缀含任务工作目录/上下文记忆，调用方在其后追加「[任务]」段与真正的 prompt
        full_prompt = build_user_message_prefix(cwd, context_memory) + f"\n\n[任务]\n{prompt}"
        # 规划模式：注入「先产出结构化计划」引导（默认关闭，失败静默）
        if self.config.planning_enabled:
            try:
                from agent_core.planning import build_planning_prompt
                full_prompt = build_planning_prompt(full_prompt)
            except Exception:  # noqa: BLE001 - 规划包装失败降级为原 prompt
                pass

        assistant_texts: list[str] = []
        final_result: str | None = None
        turns = 0
        is_error = False
        usage: dict | None = None
        cost_usd: float | None = None
        duration_ms = 0
        session_id: str | None = None

        async for msg in sdk.query(prompt=full_prompt, options=options):
            name = type(msg).__name__
            if name == "AssistantMessage":
                for block in getattr(msg, "content", []) or []:
                    if type(block).__name__ == "TextBlock":
                        assistant_texts.append(getattr(block, "text", "") or "")
            elif name == "ResultMessage":
                r = getattr(msg, "result", None)
                if r:
                    final_result = r
                turns = getattr(msg, "num_turns", None) or turns
                is_error = bool(getattr(msg, "is_error", False))
                u = getattr(msg, "usage", None)
                if isinstance(u, dict):
                    usage = u
                c = getattr(msg, "total_cost_usd", None)
                if c is not None:
                    cost_usd = c
                duration_ms = int(getattr(msg, "duration_ms", 0) or 0)
                session_id = getattr(msg, "session_id", None)

            # 协作式取消：在消息边界检查，当前 turn 结束即停
            if cancel_event is not None and cancel_event.is_set():
                return KernelResult(
                    output=final_result or "\n".join(assistant_texts),
                    turns=turns, is_error=is_error, usage=usage,
                    cost_usd=cost_usd, duration_ms=duration_ms,
                    session_id=session_id, cancelled=True,
                )

        output = final_result or "\n".join(assistant_texts) or "(无输出)"
        return KernelResult(
            output=output, turns=turns, is_error=is_error, usage=usage,
            cost_usd=cost_usd, duration_ms=duration_ms, session_id=session_id,
        )

    def open_session(self, persona: Persona, cwd: str) -> AgentSession:
        """创建多轮交互会话（未连接；调用方需 await session.connect()）。"""
        return AgentSession(self, persona, cwd)

    async def stream(self, prompt: str, persona: Persona, cwd: str,
                     context_memory: str = "") -> AsyncIterator[AgentEvent]:
        """流式执行，边消费边 yield AgentEvent。供 CLI 打印和 HTTP SSE 使用。"""
        import claude_agent_sdk as sdk

        options = self._build_options(persona, cwd)
        full_prompt = build_user_message_prefix(cwd, context_memory) + f"\n\n[任务]\n{prompt}"
        try:
            async for msg in sdk.query(prompt=full_prompt, options=options):
                for ev in _msg_to_events(msg):
                    yield ev
        except Exception as e:  # noqa: BLE001 - 流式异常转 error 事件
            yield AgentEvent("error", str(e))

    @staticmethod
    def _run_async(coro: Any) -> Any:
        """同步执行协程。若当前线程已在事件循环内，借独立线程起新 loop。

        统一消除 agent_runner.py 与 orch_cli.py 中重复的 loop 处理逻辑。
        """
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(coro)
        import concurrent.futures
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
            return ex.submit(asyncio.run, coro).result()
