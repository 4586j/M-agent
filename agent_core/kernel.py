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
import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, AsyncIterator

from agent_core.config import AgentConfig
from agent_core.context import build_system_prompt, build_user_message_prefix
from agent_core.errors import humanize_sdk_error, humanize_timeout
from agent_core.output_guard import detect_degenerate
from agent_core.persona import Persona
from agent_core.result_validator import validate_output, build_correction_hint
from agent_core.prompt_guard import detect_injection
from agent_core.tools import build_memory_mcp_server, load_mcp_servers, scan_skills
from agent_core.trace import ExecutionTracer

# 进程内记忆工具挂载为 SDK MCP server 时用的服务名（工具全名前缀 mcp__agent_memory__）
_MEMORY_SERVER_NAME = "agent_memory"

# RAG 拼接到 prompt 前的经验块限幅（字符）：上下文是稀缺注意力预算，防噪声注入。
# 从 cli.py 下沉至此，kernel 是唯一 prompt 拼装点（oneshot / stream / session 共用）。
_RAG_BLOCK_MAX_CHARS = 3000

# Plan-Execute 引擎：单步骤最大重试次数（首次执行 + 最多重试 2 次，耗尽终止）
_PLAN_STEP_MAX_RETRIES = 2
# 计划执行失败时摘要进 output 的单步输出截断（字符）
_PLAN_STEP_OUTPUT_MAX_CHARS = 800

# 输出质量守卫（output_guard.py）：退化重试的纠偏提示（追加到用户消息末尾，
# 保持稳定前缀不变以保住 prompt caching）
_GUARD_RETRY_HINT = ("[系统提示] 上一次输出异常（{reason}）。"
                     "请直接、正常地回答任务问题，输出有意义的内容，"
                     "不要重复或输出无意义字符。")

# AgentSession 超时后叫停底层执行/排空残留消息的单条等待限时（秒），
# 防底层子进程卡死时排空无限阻塞
_DRAIN_WAIT_SECONDS = 5.0

# 守卫重试的预算闸门：累计花费达 task_budget_usd 的该比例后不再重试
_BUDGET_GUARD_STOP_RATIO = 0.8

# 自纠错闭环：修正提示前缀（注入 prompt 引导 Agent 修正）
_CORRECTION_HINT_PREFIX = "[验证反馈] 上次输出存在以下问题，请修正后重新输出："

log = logging.getLogger(__name__)


def _retrieve_experiences(prompt: str, config: AgentConfig) -> list[tuple[str, str]]:
    """RAG 检索钩子：返回 [(任务摘要, 产出摘要), ...]。

    延迟导入 runtime 的 ExperienceStore（把 runtime 目录加入 sys.path），
    检索失败返回空列表，不拖垮主流程。独立成函数便于测试 monkeypatch。
    """
    try:
        import sys
        rt = (Path(config.project_root) / "workspace" / "agent"
              / "orchestrator" / "runtime")
        if str(rt) not in sys.path:
            sys.path.insert(0, str(rt))
        from experience_store import ExperienceStore
        store = ExperienceStore(config.experience_store_dir)
        similar = store.search(prompt, successful_only=True, limit=3)
        return [(getattr(exp, "prompt", "") or "",
                 getattr(exp, "output_summary", "") or "") for exp in similar]
    except Exception:  # noqa: BLE001 - RAG 失败不影响主流程
        return []


def _rag_enrich(prompt: str, config: AgentConfig,
                context_budget: int | None = None) -> str:
    """RAG 增强：检索相似历史经验并拼接到 prompt 前（从 cli.py 下沉至内核）。

    仅在 config.rag_enabled 为真时生效；否则原样返回（关闭时行为与以前完全
    一致）。无相似经验或任何异常都静默回退到原 prompt。

    context_budget（可选）：动态 RAG 预算（字符数）。有值时按剩余预算截断
    经验块，而非固定 _RAG_BLOCK_MAX_CHARS；无值时保持原行为（向后兼容）。
    """
    if not getattr(config, "rag_enabled", False) or not prompt:
        return prompt
    try:
        similar = _retrieve_experiences(prompt, config)
        if not similar:
            return prompt
        lines = ["[历史经验参考]"]
        for i, (exp_prompt, summary) in enumerate(similar, 1):
            lines.append(f"{i}. 任务: {exp_prompt[:100]}")
            if summary:
                lines.append(f"   产出: {summary[:200]}")
        block = "\n".join(lines)
        # 动态预算：优先用 context_budget，否则回退固定上限
        max_chars = context_budget if context_budget else _RAG_BLOCK_MAX_CHARS
        max_chars = max(max_chars, 500)  # 至少 500 字符，避免截断过度
        try:
            from dispatch_common import clamp_context
        except Exception:  # noqa: BLE001
            clamp_context = None
        if clamp_context is not None:
            block = clamp_context(block, max_chars)
        elif len(block) > max_chars:
            block = block[:max_chars] + "\n[经验块已截断]"
        return f"{block}\n\n{prompt}"
    except Exception:  # noqa: BLE001 - RAG 失败不影响主流程
        return prompt


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
    # 执行元数据：反思回流（reflection）、计划状态（plan/plan_status）等。
    # worker 侧 adapter 会并入 TaskResult.meta，经 callback 入库 TaskRecord.meta，
    # 供 retry_manager 重试时把失败教训注入重试任务的 context。
    meta: dict = field(default_factory=dict)


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
    elif name == "StreamEvent":
        # SDK include_partial_messages 开启时的增量事件（原始 Anthropic 流事件）。
        # 只翻译文本增量；thinking/工具入参增量不产出事件。
        event = getattr(msg, "event", None)
        if isinstance(event, dict) and event.get("type") == "content_block_delta":
            delta = event.get("delta") or {}
            if delta.get("type") == "text_delta":
                text = delta.get("text", "") or ""
                if text:
                    events.append(AgentEvent("text_delta", text))
    return events


class _StreamFilter:
    """include_partial_messages 模式下的文本去重过滤器。

    SDK 开启增量流后，同一段文本会以 StreamEvent（增量）+ 完整 AssistantMessage
    （最终）双份到达；不滤重则 CLI/HTTP 会把文本打印两遍。策略：一旦本轮出现过
    StreamEvent 文本增量，同一条 AssistantMessage 的 TextBlock 全部跳过
    （ToolUseBlock 保留——工具入参增量不翻译为事件，必须靠完整消息）。
    """

    def __init__(self, partial_text: bool):
        self._partial_text = partial_text
        self._saw_partial = False

    def reset(self) -> None:
        """复位去重标志（每轮 result 到达或流结束/异常路径统一调用）。

        中断/异常轮次可能只出现增量、未到达完整 AssistantMessage；不复位的话
        残留标志会把下一轮完整消息的文本误吞。
        """
        self._saw_partial = False

    def translate(self, msg: Any) -> list[AgentEvent]:
        name = type(msg).__name__
        if name == "StreamEvent":
            evs = _msg_to_events(msg)
            if any(e.type == "text_delta" for e in evs):
                self._saw_partial = True
            return evs
        if name == "ResultMessage":
            # 每轮 result 到达即复位去重标志：无论本轮是否被中断/异常打断，
            # 下一轮完整消息的文本都不应再被误吞
            self._saw_partial = False
            return _msg_to_events(msg)
        if self._partial_text and name == "AssistantMessage" and self._saw_partial:
            self._saw_partial = False
            events: list[AgentEvent] = []
            for block in getattr(msg, "content", []) or []:
                if type(block).__name__ == "ToolUseBlock":
                    events.append(AgentEvent("tool_use", {
                        "name": getattr(block, "name", None),
                        "input": getattr(block, "input", None),
                    }))
            return events
        return _msg_to_events(msg)


class _RoundAccumulator:
    """Plan-Execute 多轮指标累计器：turns/usage/cost/duration 跨轮累加。

    规划轮与每个步骤轮是独立的 SDK query 会话，各轮指标必须汇总后
    才能如实反映整个任务的真实成本（KernelResult.turns/cost_usd）。
    """

    def __init__(self):
        self.turns = 0
        self.usage: dict = {}
        self.cost_usd = 0.0
        self.has_cost = False
        self.duration_ms = 0

    def absorb(self, r: "KernelResult") -> None:
        """吸收一轮执行结果里的指标。"""
        self.turns += r.turns or 0
        if isinstance(r.usage, dict):
            for k, v in r.usage.items():
                if isinstance(v, (int, float)):
                    self.usage[k] = self.usage.get(k, 0) + v
        if r.cost_usd is not None:
            self.cost_usd += r.cost_usd
            self.has_cost = True
        self.duration_ms += r.duration_ms or 0

    def result(self, output: str, session_id: str | None = None,
               is_error: bool = False, cancelled: bool = False) -> "KernelResult":
        """按累计指标构造最终 KernelResult。"""
        return KernelResult(
            output=output, turns=self.turns, is_error=is_error,
            usage=self.usage or None,
            cost_usd=round(self.cost_usd, 6) if self.has_cost else None,
            duration_ms=self.duration_ms, session_id=session_id,
            cancelled=cancelled,
        )


class _Deadline:
    """整任务墙钟截止点（单调时钟）：从 run_oneshot 入口贯穿执行/纠错/验证。

    用绝对截止时刻而非各阶段独立 timeout，确保「卡住的纠错」不会把总时长
    拖过 task_timeout（P0：此前纠错/重试在主超时之外无限延长）。
    remaining() 供后续阶段计算各自的等待上限；expired() 用于提前收场。
    """

    def __init__(self, timeout_s: float):
        try:
            span = max(0.0, float(timeout_s))
        except (TypeError, ValueError):
            span = 0.0
        self._end = time.monotonic() + span

    def remaining(self) -> float:
        return self._end - time.monotonic()

    def expired(self) -> bool:
        return self.remaining() <= 0


def _plan_summary(plan) -> str:
    """全部步骤完成后的产出汇总（每步摘要限幅，供经验沉淀/反思消费）。"""
    lines = [f"Plan-Execute 完成：计划「{plan.goal}」共 {len(plan.steps)} 个步骤全部执行完成。"]
    for s in plan.steps:
        if s.status != "done":
            continue
        out = (s.output or "").strip()
        lines.append(f"\n## 步骤 {s.id}: {s.action}\n"
                     + out[:_PLAN_STEP_OUTPUT_MAX_CHARS]
                     + ("…[已截断]" if len(out) > _PLAN_STEP_OUTPUT_MAX_CHARS else ""))
    return "\n".join(lines)


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
        # 预算软预警已触发的阈值集合（80% / 95% 各最多提醒一次）
        self._warned: set[float] = set()
        # 轮次摘要机制：记录每轮文本，每 N 轮压缩为摘要
        self._turn_texts: list[str] = []
        self._turn_count: int = 0
        self._context_summary: str = ""  # 已压缩的历史摘要

    async def connect(self) -> None:
        """建立底层 SDK 客户端连接。可携带已有 session_id 续接历史会话。"""
        import claude_agent_sdk as sdk

        options = self._kernel._build_options(
            self._persona, self._cwd, resume_session_id=self._session_id
        )
        self._client = sdk.ClaudeSDKClient(options=options)
        await self._client.connect()

    def _check_budget_warnings(self) -> list[dict]:
        """按累计 cost 检查 task_budget_usd 的 80% / 95% 软预警。

        每个阈值最多提醒一次（_warned 去重），不改 SDK 硬熔断行为。
        无预算配置或累计为 0 时返回空。
        """
        budget = self._kernel.config.task_budget_usd
        if not budget or self._total_cost <= 0:
            return []
        out: list[dict] = []
        for threshold in (0.8, 0.95):
            if threshold not in self._warned and self._total_cost >= budget * threshold:
                self._warned.add(threshold)
                out.append({
                    "threshold": threshold,
                    "spent_usd": round(self._total_cost, 6),
                    "budget_usd": budget,
                })
        return out

    def _record_session_experience(self, raw_prompt: str,
                                   result_data: dict | None,
                                   texts: list[str]) -> None:
        """会话经验沉淀（config.auto_record_experience 控制，失败静默）。

        复用 run_oneshot 的 _record_experience 实现：把本轮 result 事件数据
        收敛为 KernelResult 后记录。产物不扫描 cwd（会话工作目录里常混有
        多轮中间文件，避免噪声）。本轮检出退化输出（result 事件 data 的
        degenerate 标记）时按失败口径沉淀，不沉淀“成功”假经验。
        """
        d = result_data or {}
        try:
            degen = d.get("degenerate") if isinstance(d.get("degenerate"), dict) else None
            kr = KernelResult(
                output=d.get("result") or "\n".join(texts) or "(无输出)",
                turns=d.get("num_turns") or 0,
                is_error=(bool(d.get("is_error", False))
                          or bool(degen and degen.get("detected"))),
                usage=d.get("usage"),
                cost_usd=d.get("cost_usd"),
                duration_ms=int(d.get("duration_ms") or 0),
                session_id=d.get("session_id") or self._session_id,
                meta={"degenerate": degen} if degen else {},
            )
            self._kernel._record_experience(raw_prompt, self._persona, kr, self._cwd)
        except Exception:  # noqa: BLE001 - 经验沉淀失败不影响会话
            pass

    def _check_round_degenerate(self, prompt: str, d: dict,
                                texts: list[str]) -> None:
        """会话轮退化检测（轻量版，不做自动重试）。

        result 事件产出后对最终文本跑 detect_degenerate（tool 轮无文本跳过）；
        检出时在该轮 result 事件 data 附 degenerate 标记（上层 SSE/CLI 可见），
        且本轮经验沉淀改走失败口径（_record_session_experience 读该标记）。
        多轮上下文由用户接管，异常输出不从 SDK 会话历史中删除（SDK 无此能力）。
        """
        if not getattr(self._kernel.config, "output_guard_enabled", True):
            return
        text = str(d.get("result") or "") or "\n".join(t for t in texts if t)
        if not text or text.strip() == "(无输出)":
            return
        verdict = detect_degenerate(text, prompt)
        if verdict.is_degenerate:
            d["degenerate"] = {"detected": True, "reason": verdict.reason,
                               "score": verdict.score, "hits": list(verdict.hits)}

    def _maybe_summarize_turns(self) -> None:
        """轮次摘要压缩：每 session_summarize_interval 轮触发一次摘要。

        将早期轮次文本压缩为摘要，仅保留最近 2 轮完整内容。
        摘要存入 self._context_summary，供后续轮次的 context_memory 注入。
        失败静默，不拖垮会话。
        """
        interval = getattr(self._kernel.config, "session_summarize_interval", 5)
        if not getattr(self._kernel.config, "session_summarize_enabled", True):
            return
        if self._turn_count < interval or len(self._turn_texts) < 3:
            return
        try:
            # 取前 N-2 轮做摘要，保留最近 2 轮
            to_summarize = self._turn_texts[:-2]
            keep = self._turn_texts[-2:]
            summary_text = "\n".join(t[:200] for t in to_summarize)
            if self._context_summary:
                self._context_summary = (
                    f"{self._context_summary}\n\n"
                    f"[早期轮次摘要]\n{summary_text}")
            else:
                self._context_summary = f"[早期轮次摘要]\n{summary_text}"
            # 截断总摘要长度
            max_chars = getattr(self._kernel.config,
                                "session_max_context_chars", 80000)
            if len(self._context_summary) > max_chars // 2:
                self._context_summary = self._context_summary[-(max_chars // 2):]
            self._turn_texts = keep
            log.info("会话轮次摘要压缩：%d 轮 -> 摘要 + %d 轮保留",
                     len(to_summarize), len(keep))
        except Exception:  # noqa: BLE001
            log.warning("轮次摘要压缩失败", exc_info=True)

    async def send(self, prompt: str, timeout: float | None = None) -> AsyncIterator[AgentEvent]:
        """发送一轮消息并流式 yield AgentEvent。

        SDK 0.2.x 用法：ClaudeSDKClient.query() 是「协程」（写入 transport 后返回
        None，只负责发送），必须先 await；随后用 receive_response() 这个「异步生成器」
        收流，它产出消息并在 ResultMessage 处自动结束本轮。
        （早期误把 query() 当异步生成器直接 ``async for``，真跑会报
        "'async for' requires an object with __aiter__ method, got coroutine"，
        而 mock 测试因把 query 造成了生成器而漏过——见 test_kernel 的假 SDK。）
        ResultMessage 到达时更新累计 session_id / turns / cost。

        timeout（可选墙钟，秒）：等待 SDK 每条消息的最长时间；超时转 error 事件
        结束本轮（而非裸抛异常），默认 None 不限超时（保持原行为）；
        timeout<=0 是调用方 bug，抛 ValueError（0 不再被解释成「不限超时」）。
        config.rag_enabled 开启时在内核侧做经验 RAG 拼装（经验沉淀用原始 prompt）。
        """
        if self._client is None:
            raise RuntimeError("会话未连接，请先 await connect()")
        if timeout is not None and timeout <= 0:
            raise ValueError("timeout 必须为正数（秒）；None 表示不限超时")
        # Prompt Injection 检测（会话入口）
        injection_result = self._kernel._check_prompt_injection(prompt)
        if injection_result is not None:
            yield AgentEvent("error", injection_result.output)
            yield AgentEvent("result", {
                "result": injection_result.output, "num_turns": 0,
                "is_error": True, "usage": None, "cost_usd": None,
                "duration_ms": 0, "session_id": self._session_id,
                "injection": injection_result.meta.get("injection"),
            })
            return
        # session_id 是流内消息路由标识，SDK 约定用 "default"；
        # 会话续接靠 connect() 时把 self._session_id 作为 resume 选项，与此无关。
        enriched = self._kernel.rag_enrich(prompt)
        await self._client.query(prompt=enriched)
        rx = self._client.receive_response()
        it = rx.__aiter__()
        deadline = (time.monotonic() + timeout) if timeout is not None else None
        flt = _StreamFilter(partial_text=bool(self._kernel.config.streaming))
        texts: list[str] = []
        result_data: dict | None = None
        while True:
            try:
                if deadline is None:
                    msg = await it.__anext__()
                else:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise asyncio.TimeoutError()
                    msg = await asyncio.wait_for(it.__anext__(), remaining)
            except StopAsyncIteration:
                break
            except (TimeoutError, asyncio.TimeoutError):
                # 墙钟超时转 error 事件，不裸抛（与流式语义一致）；随后叫停
                # 底层执行并排空本轮残留消息，防残留消息污染下一轮
                flt.reset()  # 异常路径复位去重标志，防残留吞掉下一轮文本
                yield AgentEvent("error", f"本轮等待响应超时（>{timeout}s）。"
                                         "建议：调大超时参数或检查后端负载。")
                await self._drain_after_timeout(it)
                return
            for ev in flt.translate(msg):
                if ev.type == "result" and isinstance(ev.data, dict):
                    d = ev.data
                    result_data = d
                    if d.get("session_id"):
                        self._session_id = d["session_id"]
                    self._total_turns += d.get("num_turns") or 0
                    if d.get("cost_usd") is not None:
                        self._total_cost += d["cost_usd"]
                    # 输出质量守卫（轻量版）：检出退化时在该轮 result 事件 data
                    # 附 degenerate 标记，本轮经验改走失败口径沉淀
                    self._check_round_degenerate(prompt, d, texts)
                elif ev.type == "text_delta":
                    texts.append(ev.data or "")
                yield ev
                if ev.type == "result":
                    # 预算软预警（80%/95%，各提醒一次）：紧跟 result 事件之后
                    for w in self._check_budget_warnings():
                        yield AgentEvent("budget_warning", w)
        # 轮次跟踪：记录本轮文本，触发摘要压缩
        self._turn_count += 1
        turn_text = "\n".join(texts) if texts else ""
        if turn_text:
            self._turn_texts.append(turn_text)
        self._maybe_summarize_turns()
        # 经验自动沉淀（内核统一实现；用原始 prompt 而非 RAG 拼装后的）
        if result_data is not None and self._kernel.config.auto_record_experience:
            self._record_session_experience(prompt, result_data, texts)

    async def _drain_after_timeout(self, it: Any) -> None:
        """超时后叫停底层执行并排空本轮残留消息，防污染下一轮。

        先 interrupt() 协作中断当前 turn；随后继续消费 __anext__ 至本轮
        ResultMessage / StopAsyncIteration，把残留消息排空（不 yield、
        不更新累计指标——本轮已按 error 收场）。每条等待限时
        _DRAIN_WAIT_SECONDS，防底层子进程卡死时排空无限阻塞；
        interrupt/排空失败仅记日志，不影响本轮 error 已返回的事实。
        """
        try:
            await asyncio.wait_for(self._client.interrupt(), _DRAIN_WAIT_SECONDS)
        except Exception as e:  # noqa: BLE001 - 叫停失败不阻断排空尝试
            log.warning("session 超时后 interrupt 失败: %r", e)
        try:
            while True:
                try:
                    await asyncio.wait_for(it.__anext__(), _DRAIN_WAIT_SECONDS)
                except StopAsyncIteration:
                    return
                except (TimeoutError, asyncio.TimeoutError):
                    log.warning("session 超时后排空等待超时（>%ss），放弃剩余消息",
                                _DRAIN_WAIT_SECONDS)
                    return
        except Exception as e:  # noqa: BLE001 - 排空失败不影响本轮返回
            log.warning("session 超时后排空残留消息失败: %r", e)

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
        # 守卫执行进度快照（_execute_oneshot_guarded 每轮更新）：
        # run_oneshot 墙钟超时时协程被取消、acc 状态不可达，超时分支
        # 借此把已累计的 cost/turns/usage 与最近一次 verdict 摘要透传出
        self._last_progress: dict = {}
        # 结果翻译服务（惰性构造）：仅 result_translate_enabled 开启时首次用到才建。
        self._translator = None

    def _check_prompt_injection(self, prompt: str) -> "KernelResult | None":
        """Prompt Injection 检测。返回 None 表示通过，否则返回拒绝/告警结果。

        config.prompt_guard_enabled=False 时跳过；action="warn" 时仅记 meta
        不拦截；action="reject" 时返回 is_error=True 的 KernelResult。
        """
        if not getattr(self.config, "prompt_guard_enabled", True):
            return None
        try:
            verdict = detect_injection(prompt)
        except Exception:  # noqa: BLE001 - 检测异常不阻断
            log.warning("Prompt Injection 检测异常，放行", exc_info=True)
            return None
        if not verdict.is_injection:
            return None
        action = getattr(self.config, "prompt_guard_action", "reject").lower()
        log.warning("Prompt Injection 检出：signals=%s score=%.2f action=%s",
                    verdict.signals, verdict.score, action)
        if action == "warn":
            return None  # 仅告警不拦截，调用方可从 meta 读取
        # reject：返回错误结果
        kr = KernelResult(
            output="输入被拒绝：检测到潜在的提示注入攻击。"
                   "请检查输入内容是否包含角色劫持、分隔符伪造或指令覆盖等内容。",
            is_error=True, error_type="prompt_injection",
        )
        kr.meta["injection"] = verdict.to_dict()
        return kr

    def _write_unattended_settings(self, cwd: str) -> str:
        """无人值守模式（bypassPermissions）的任务目录权限设置。

        Windows CLI 实测：bypassPermissions 需要 IS_SANDBOX 且 Windows 下 CLI 直接异常；
        改为在任务目录写 .claude/settings.json（allow 清单 + defaultMode=acceptEdits），
        经 options.settings 显式加载——setting_sources 仍为空，用户级/项目级设置继续隔离。
        """
        settings_dir = Path(cwd) / ".claude"
        settings_dir.mkdir(parents=True, exist_ok=True)
        settings = {"permissions": {
            "defaultMode": "acceptEdits",
            "allow": ["Write", "Edit", "Read", "Glob", "Grep", "Bash", "WebFetch", "WebSearch"],
            "deny": [],
        }}
        path = settings_dir / "settings.json"
        path.write_text(json.dumps(settings, indent=2), encoding="utf-8")
        return str(path)

    def _build_options(self, persona: Persona, cwd: str,
                       resume_session_id: str | None = None,
                       query: str = "") -> Any:
        """构造 SDK ClaudeAgentOptions。

        组装：system_prompt（context.build_system_prompt）、model、cwd、
        permission_mode、max_turns、max_thinking_tokens、max_budget_usd、
        mcp_servers（tools.load_mcp_servers）、agents（persona.sub_agents）、
        tools（memory_tools）、resume。
        model 优先 persona.model，其次 config.model；
        max_thinking_tokens / max_budget_usd 为 0/None 时不传（表示不限制）。
        query：memory_core_retrieval 开启时作为核心记忆 Top-K 筛选依据
        （传入当前任务 prompt）；关闭时忽略，保持全量注入与缓存行为不变。
        """
        import claude_agent_sdk as sdk

        skills = scan_skills(self.config.project_root)
        system_prompt = build_system_prompt(
            persona, self.config.project_root, self.config.memory_core_dir, skills,
            memory_backend=self.config.memory_backend,
            query=query if self.config.memory_core_retrieval else "",
        )
        model = persona.model or self.config.model

        # 无人值守权限：bypassPermissions（Windows 不可用）→ 任务目录 settings + acceptEdits
        permission_mode = self.config.permission_mode
        unattended_settings: str | None = None
        if permission_mode == "bypassPermissions":
            unattended_settings = self._write_unattended_settings(cwd)
            permission_mode = "acceptEdits"

        kwargs: dict[str, Any] = dict(
            cwd=cwd,
            system_prompt=system_prompt,
            permission_mode=permission_mode,
            max_turns=self.config.max_turns,
            # 隔离用户级/项目级 Claude Code 设置文件（~/.claude/settings.json 等）。
            # 某些本地代理工具（如 image-vision-mcp）会在用户级 settings.json 的 env 块里
            # 把 ANTHROPIC_BASE_URL 覆盖成 http://127.0.0.1:<port>，且该 env 优先级「高于」
            # 进程环境变量——会让本内核无视 .env 里的真实端点，转而连一个流式挂起的本地代理
            # （症状：CLI 打印 init 后卡死、直接 HTTP 探针却 200 正常）。传空 setting_sources
            # → CLI 收到 --setting-sources=（加载零个设置文件）→ 只认 .env 注入的端点/鉴权。
            setting_sources=[],
            # 只用本内核显式挂载的 MCP（.mcp.json + 进程内记忆 server，经 --mcp-config 传入），
            # 屏蔽用户级自动发现的 npx MCP（playwright/image-vision 等），避免启动被拖慢，
            # 也杜绝再次引入那个改写端点的本地代理。
            strict_mcp_config=True,
        )
        if model:
            kwargs["model"] = model
        # 成本控制：思考 token 是最大成本项；max_budget_usd 是单任务硬预算熔断
        if self.config.max_thinking_tokens:
            kwargs["max_thinking_tokens"] = self.config.max_thinking_tokens
        if self.config.task_budget_usd:
            kwargs["max_budget_usd"] = self.config.task_budget_usd
        # AGENT_STREAMING（config.streaming）映射为 SDK 增量流选项 include_partial_messages：
        # 开启后 SDK 额外产出 StreamEvent 部分增量（经 _msg_to_events 翻译为
        # text_delta，完整 AssistantMessage 的 TextBlock 由 _StreamFilter 去重），
        # CLI/SSE 能拿到更细粒度的流式输出；关闭时保持 SDK 默认（只有完整消息）。
        if self.config.streaming:
            kwargs["include_partial_messages"] = True

        # MCP 服务：外部配置（.mcp.json 等）与进程内记忆工具（SDK MCP server）合并挂载。
        # 进程内工具必须经 create_sdk_mcp_server 包成 server 放进 mcp_servers——不能把
        # SdkMcpTool 直接塞进 ClaudeAgentOptions.tools（那是 built-in 工具「名字」列表，
        # 传对象会在 SDK 序列化 CLI 参数时 str.join 报错，且会顶掉全部内置工具）。
        mcp_servers = dict(
            self.config.mcp_servers
            or load_mcp_servers(self.config.mcp_config_path)
            or {}
        )
        # 记忆工具构建可能因 runtime 不可达/依赖缺失而报错，不能拖垮整个选项构建
        try:
            mem_server = build_memory_mcp_server(self.config)
            if mem_server:
                mcp_servers[_MEMORY_SERVER_NAME] = mem_server
        except Exception:  # noqa: BLE001 - 记忆工具失败不阻断主执行
            pass
        if mcp_servers:
            kwargs["mcp_servers"] = mcp_servers
        if persona.sub_agents and getattr(self.config, "subagents_enabled", True):
            kwargs["agents"] = persona.sub_agents
        if resume_session_id:
            kwargs["resume"] = resume_session_id
        if unattended_settings:
            kwargs["settings"] = unattended_settings

        options = sdk.ClaudeAgentOptions(**kwargs)
        # persona 显式限定工具集时收窄 allowed_tools（None=全工具）
        if persona.tools:
            options.allowed_tools = [str(t).strip() for t in persona.tools]
        return options

    def run_oneshot(self, prompt: str, persona: Persona, cwd: str,
                    context_memory: str = "",
                    cancel_event: Any = None, resume: bool = False,
                    record_experience: bool = True) -> KernelResult:
        """同步单发执行。

        内部调用 _run_async(...)，并施加墙钟超时（task_timeout）。
        - asyncio.wait_for 超时控制；
        - cancel_event 协作式取消（在消息边界检查）；
        - 收集 ResultMessage 的 usage/cost/duration；
        - 成功完成后扫描 cwd 下的文件作为 artifacts。

        config.planning_enabled=True 时改由 Plan-Execute 引擎接管（_execute_plan_engine）：
        先规划轮产出结构化计划，再逐步骤驱动执行；规划解析失败自动降级为
        普通单轮执行。resume=True 为显式断点续跑声明（默认也会自动检测
        .orch_progress 下同 prompt 的未完成计划）。

        config.auto_routing=True 时用 TaskRouter 分类任务复杂度，动态决定
        是否走规划/纠错路径。config.correction_enabled=True 时在退化检测
        通过后追加结果验证 + 修正循环。全程用 ExecutionTracer 收集 trace。
        """
        self._last_progress = {}  # 重置进度快照，防上一任务残留误透传
        tracer = ExecutionTracer("run_oneshot")
        # 整任务截止点（P0）：在入口（admission）即起算，贯穿核心执行、自纠错
        # 与验证，使各阶段共享同一份墙钟预算，纠错/重试不再游离于主超时之外。
        deadline = _Deadline(self.config.task_timeout)

        # Prompt Injection 检测（内核入口统一拦截）
        injection_result = self._check_prompt_injection(prompt)
        if injection_result is not None:
            return injection_result

        # 任务自适应路由
        use_planning = self.config.planning_enabled
        use_correction = self.config.correction_enabled
        routing_info: dict = {}
        if self.config.auto_routing:
            from agent_core.task_router import (classify_task, should_use_planning,
                                                should_use_correction)
            profile = classify_task(prompt, persona.name)
            use_planning = should_use_planning(profile, self.config.planning_enabled)
            use_correction = (should_use_correction(profile)
                              and self.config.correction_enabled)
            routing_info = {
                "complexity": profile.complexity, "strategy": profile.strategy,
                "confidence": profile.confidence, "reason": profile.reason,
            }

        if use_planning:
            core = self._execute_plan_engine(prompt, persona, cwd, context_memory,
                                             cancel_event, resume)
        else:
            # 输出质量守卫：普通单轮路径带退化检测与三级兜底（默认开，可关）。
            core = self._execute_oneshot_guarded(prompt, persona, cwd,
                                                 context_memory, cancel_event)

        async def _run_with_timeout() -> KernelResult:
            # 用整任务截止点的剩余时间作为核心执行上限（入口时≈task_timeout）。
            return await asyncio.wait_for(core, timeout=max(0.0, deadline.remaining()))

        try:
            result = self._run_async(_run_with_timeout())
        except asyncio.TimeoutError:
            msg, hint = humanize_timeout(self.config.task_timeout)
            prog = self._last_progress or {}
            result = KernelResult(
                output=f"{msg}。建议：{hint}", is_error=True,
                turns=prog.get("turns") or 0,
                usage=prog.get("usage"),
                cost_usd=prog.get("cost_usd"),
                duration_ms=self.config.task_timeout * 1000, error_type="timeout",
            )
            if prog.get("degenerate_last_reason"):
                result.meta["degenerate"] = {
                    "detected": True, "recovered": False,
                    "reasons": [prog["degenerate_last_reason"]],
                    "note": "timeout",
                }
            tracer.finish()
            result.meta["trace"] = tracer.to_dict()
            if routing_info:
                result.meta["routing"] = routing_info
            return result
        except Exception as e:  # noqa: BLE001 - SDK 异常统一收敛为错误结果
            error_type, message, hint = humanize_sdk_error(e)
            tracer.finish()
            kr = KernelResult(
                output=f"{message}\n原始错误：{e}\n建议：{hint}",
                is_error=True, error_type="sdk_error")
            kr.meta["trace"] = tracer.to_dict()
            if routing_info:
                kr.meta["routing"] = routing_info
            return kr

        # 自纠错闭环：退化检测通过后追加结果验证 + 修正循环（共享整任务截止点）
        if use_correction and not result.cancelled and not result.is_error:
            result = self._run_correction_loop(
                result, prompt, persona, cwd, context_memory, cancel_event, tracer,
                deadline)

        # artifacts 收集：仅在未取消/未报错且 cwd 存在时扫描产物文件
        if not result.cancelled and not result.is_error:
            cwd_path = Path(cwd)
            if cwd_path.is_dir():
                result.artifacts = [
                    str(p.relative_to(cwd_path))
                    for p in cwd_path.rglob("*") if p.is_file()
                ]
        # 经验自动沉淀（record_experience=False 为评测消融组的硬关：即使
        # config.auto_record_experience 为真也不写，确保「无经验」组不落库）。
        if (self.config.auto_record_experience and record_experience
                and not result.cancelled):
            self._record_experience(prompt, persona, result, cwd)
        # 自我反思
        reflection = self._run_reflection(result, persona, cwd)
        if reflection:
            result.meta["reflection"] = reflection[:2000]
            if not result.is_error:
                try:
                    self._write_reflection_to_memory(result.session_id, reflection)
                except Exception:  # noqa: BLE001
                    pass
        # trace 写入 meta
        tracer.finish()
        result.meta["trace"] = tracer.to_dict()
        if routing_info:
            result.meta["routing"] = routing_info
        # 结果翻译（可选，默认关）：思考过程/流式 token 保持原语言（通常英文），
        # 仅在结果边界把最终产出译为目标语言（默认中文）。刻意放在经验沉淀/反思之后，
        # 确保入库经验与反思基于模型真实产出语言；翻译失败绝不影响主结果。
        self._maybe_translate_result(result)
        return result

    def _get_translator(self):
        """惰性构造结果翻译服务（独立上下文，绝不污染任务上下文）。"""
        if self._translator is None:
            from agent_core.translator import TranslatorService
            self._translator = TranslatorService(self.config)
        return self._translator

    def _should_translate(self, result: KernelResult) -> bool:
        """是否对结果做自动翻译：全局开关开 + 成功未取消 + 有产出文本。

        顾问/翻译自身的隔离配置把 result_translate_enabled 关成 False，故其
        run_oneshot 不会进入本分支，杜绝递归翻译。
        """
        return (bool(getattr(self.config, "result_translate_enabled", False))
                and not result.is_error and not result.cancelled
                and bool(result.output and result.output.strip()))

    def _maybe_translate_result(self, result: KernelResult) -> None:
        """就地把 result.output 译为目标语言；原文保留进 meta，任何失败都不影响主结果。"""
        if not self._should_translate(result):
            return
        target = getattr(self.config, "translate_target_lang", "zh") or "zh"
        try:
            tr = self._get_translator().translate(result.output, target_lang=target)
        except Exception:  # noqa: BLE001 - 翻译失败绝不影响主结果
            log.warning("结果翻译失败，保留原文", exc_info=True)
            result.meta["translated"] = {"target_lang": target, "changed": False,
                                         "error": "translate_exception"}
            return
        if tr.changed and tr.translated:
            # 保留英文原文（可回溯）；对外结果切换为译文
            result.meta["original_output"] = result.output
            result.meta["translated"] = {"target_lang": tr.target_lang, "changed": True}
            result.output = tr.translated
        elif tr.skipped:
            result.meta["translated"] = {"target_lang": tr.target_lang,
                                         "changed": False, "skipped": tr.skipped}
        elif tr.error:
            result.meta["translated"] = {"target_lang": target, "changed": False,
                                         "error": tr.error}

    def _run_reflection(self, result: KernelResult, persona: Persona,
                        cwd: str) -> str | None:
        """自我反思：成功后质量自评 / 失败后原因复盘。返回反思文本或 None。

        仅在 config.reflection_enabled 且任务未取消时触发：
        - 成功（默认）：用 build_reflection_prompt 评估产出质量；
        - 失败（result.is_error 且有产出可用）：用 build_failure_reflection_prompt
          分析失败原因与改进建议，产物经 meta["reflection"] 回流，供重试时注入。
        无产出内容的失败（timeout/sdk_error）无从反思，返回 None。
        用同一 persona 做一次单轮轻量查询，任何异常都静默返回 None。
        """
        if not self.config.reflection_enabled or result.cancelled:
            return None
        if not result.output:
            return None
        try:
            from agent_core.planning import (build_reflection_prompt,
                                             build_failure_reflection_prompt)
            # 限幅避免反思输入过长烧 token
            if result.is_error:
                reflection_prompt = build_failure_reflection_prompt(result.output[:4000])
            else:
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

    async def _execute_plan_engine(self, prompt: str, persona: Persona, cwd: str,
                                   context_memory: str, cancel_event: Any,
                                   resume: bool) -> KernelResult:
        """Plan-Execute 两阶段引擎（config.planning_enabled 时由 run_oneshot 接管）。

        流程：
        1. 断点检测：.orch_progress 下同 prompt 的未完成计划（resume=True 为
           显式声明，默认同样自动检测）；计划年龄超 plan_resume_max_age_hours
           不续跑（开新计划），避免定时任务/评测同 prompt 错配陈旧计划；命中则
           跳过规划轮直接续跑，failed 步骤重置为 pending 重新执行（续跑自愈）；
        2. 规划轮：注入规划指令跑一轮，解析结构化计划并 save_plan；
           解析失败降级为普通单轮执行并告警，不报错；
        3. 步骤轮：按 next_pending_step() 逐步骤驱动，每步单独一轮，注入计划
           上下文（当前步骤/已完成摘要/依赖产物）与调用方 context_memory 前缀
           （与规划轮一致，保证首跑与断点续跑输入相同）；成功标 done 并实时
           save_plan；失败重试最多 _PLAN_STEP_MAX_RETRIES 次（重试注入上次
           失败教训），耗尽则终止并返回带 failed 计划状态的结果；
        4. 步骤循环结束：仍存在 failed 步骤则如实报 failed（is_error=True，
           经验沉淀走失败口径）；否则返回产出汇总结果，外层照常跑经验沉淀/反思。
        规划轮与步骤轮的 cost/turns/usage/duration 全部累加进最终 KernelResult。
        """
        from agent_core.planning import (PlanningManager, build_planning_prompt,
                                         build_step_prompt, parse_plan_from_output)
        progress_dir = Path(self.config.project_root) / ".orch_progress"
        mgr = PlanningManager(progress_dir)
        acc = _RoundAccumulator()
        session_id: str | None = None
        plan = None

        # 1. 断点检测（同 prompt 未完成计划；resume 参数为显式声明，检测逻辑一致）。
        #    超龄计划不续跑，避免定时任务/评测同 prompt 错配陈旧计划。
        resumed = mgr.find_resumable(
            prompt, max_age_hours=self.config.plan_resume_max_age_hours)
        if resumed is not None:
            session_id, plan = resumed
            # 续跑前重新校验依赖图（P1：陈旧/损坏的持久化计划不得盲目续跑）；
            # 校验不通过则放弃续跑、回落到重新规划（plan=None → 走规划轮）。
            if getattr(self.config, "plan_validation_enabled", True):
                from agent_core.plan_validator import validate_plan
                rv = validate_plan(plan)
                if not rv.is_valid:
                    log.warning("续跑计划校验失败（%s），放弃续跑改为重新规划",
                                "; ".join(rv.issues))
                    plan = None
                    session_id = None
            if plan is not None:
                # 续跑自愈：failed 步骤重置为 pending 重新执行——否则失败步骤及其
                # 依赖会被静默跳过；重试再耗尽则按下方失败口径如实上报
                reset_ids = [s.id for s in plan.steps if s.status == "failed"]
                if reset_ids:
                    for s in plan.steps:
                        if s.status == "failed":
                            s.status = "pending"
                            s.output = ""
                    mgr.save_plan(session_id, plan)
                log.info("resume plan %s (%d steps, goal=%s)", session_id,
                         len(plan.steps), plan.goal)

        # 2. 规划轮
        if plan is None:
            options = self._build_options(persona, cwd, query=prompt)
            planning_prompt = (build_user_message_prefix(cwd, context_memory)
                               + f"\n\n[任务]\n{prompt}")
            planning_prompt = build_planning_prompt(planning_prompt)
            planning_prompt = self.rag_enrich(planning_prompt)
            plan_round = await self._run_sdk_query(planning_prompt, options, cancel_event)
            acc.absorb(plan_round)
            if plan_round.cancelled:
                return acc.result(output=plan_round.output or "(无输出)",
                                  session_id=plan_round.session_id, cancelled=True)
            try:
                plan = parse_plan_from_output(plan_round.output)
            except Exception:  # noqa: BLE001 - 解析异常视同解析失败
                plan = None
            if plan is None or not plan.steps:
                # 规划失败降级：普通单轮执行（不再注入规划指令），不报错
                log.warning("规划轮未解析出有效计划，降级为普通单轮执行")
                fallback = await self._execute_oneshot(prompt, persona, cwd,
                                                       context_memory, cancel_event)
                acc.absorb(fallback)
                return acc.result(output=fallback.output,
                                  session_id=fallback.session_id or plan_round.session_id,
                                  is_error=fallback.is_error,
                                  cancelled=fallback.cancelled)
            session_id = plan_round.session_id or f"plan-{int(time.time())}"
            plan.source_prompt = prompt
            # 计划验证（plan_validation_enabled 时）
            if getattr(self.config, "plan_validation_enabled", True):
                from agent_core.plan_validator import validate_plan, try_fix_plan
                vresult = validate_plan(plan)
                if not vresult.is_valid:
                    log.warning("计划验证失败：%s，尝试自动修复",
                                "; ".join(vresult.issues))
                    fixed = try_fix_plan(plan)
                    if fixed:
                        vresult2 = validate_plan(plan)
                        if vresult2.is_valid:
                            log.info("计划自动修复成功")
                        else:
                            log.warning("计划修复后仍存在问题：%s，降级为单轮",
                                        "; ".join(vresult2.issues))
                            fallback = await self._execute_oneshot(
                                prompt, persona, cwd, context_memory, cancel_event)
                            acc.absorb(fallback)
                            return acc.result(
                                output=fallback.output,
                                session_id=fallback.session_id or plan_round.session_id,
                                is_error=fallback.is_error,
                                cancelled=fallback.cancelled)
                    else:
                        log.warning("计划无法自动修复，降级为单轮执行")
                        fallback = await self._execute_oneshot(
                            prompt, persona, cwd, context_memory, cancel_event)
                        acc.absorb(fallback)
                        return acc.result(
                            output=fallback.output,
                            session_id=fallback.session_id or plan_round.session_id,
                            is_error=fallback.is_error,
                            cancelled=fallback.cancelled)
                if vresult.warnings:
                    log.info("计划验证警告：%s", "; ".join(vresult.warnings))
            mgr.save_plan(session_id, plan)

        # 3. 步骤轮：逐步骤驱动
        replanned = False
        while True:
            if cancel_event is not None and cancel_event.is_set():
                return acc.result(output=_plan_summary(plan), session_id=session_id,
                                  cancelled=True)
            step = plan.next_pending_step()
            if step is None:
                break
            step_prompt = build_step_prompt(prompt, plan, step,
                                            context_memory=context_memory)
            success = False
            last_output = ""
            for attempt in range(_PLAN_STEP_MAX_RETRIES + 1):
                options = self._build_options(persona, cwd, query=step_prompt)
                step_round = await self._run_sdk_query(
                    self.rag_enrich(step_prompt), options, cancel_event)
                acc.absorb(step_round)
                if step_round.cancelled:
                    return acc.result(output=step_round.output or _plan_summary(plan),
                                      session_id=step_round.session_id or session_id,
                                      cancelled=True)
                last_output = step_round.output
                if not step_round.is_error:
                    step.status = "done"
                    step.output = step_round.output
                    mgr.save_plan(session_id, plan)
                    success = True
                    break
                if attempt < _PLAN_STEP_MAX_RETRIES:
                    # 重试同一个步骤：注入上次失败教训，避免重蹈覆辙
                    step_prompt = build_step_prompt(prompt, plan, step,
                                                    context_memory=context_memory,
                                                    failure_hint=last_output)
            if not success:
                step.status = "failed"
                step.output = (last_output or "")[:500]
                mgr.save_plan(session_id, plan)
                # 动态重规划（plan_replan_enabled 时尝试重新规划而非直接终止）
                if (getattr(self.config, "plan_replan_enabled", True)
                        and not replanned):
                    log.info("步骤 %d 重试耗尽，尝试重新规划", step.id)
                    # id 接续提示：降低新步骤与已完成步骤 id 冲突的概率
                    next_id = plan.max_step_id() + 1
                    replan_prompt = (
                        f"原任务：{prompt}\n\n"
                        f"已完成步骤摘要：\n"
                        + "\n".join(plan.done_summaries())
                        + f"\n\n失败步骤 {step.id}：{step.action}\n"
                        f"失败原因：{(last_output or '')[:1000]}\n\n"
                        f"请基于以上进展重新规划剩余步骤：新步骤 id 从 {next_id} "
                        "开始递增，不要与已完成步骤 id 冲突；depends_on 可引用"
                        "已完成步骤的 id。")
                    replan_options = self._build_options(persona, cwd,
                                                         query=replan_prompt)
                    replan_round = await self._run_sdk_query(
                        self.rag_enrich(build_planning_prompt(replan_prompt)),
                        replan_options, cancel_event)
                    acc.absorb(replan_round)
                    new_plan = parse_plan_from_output(replan_round.output)
                    if new_plan and new_plan.steps:
                        # 保留已完成步骤，替换剩余计划
                        done_steps = [s for s in plan.steps if s.status == "done"]
                        merged = new_plan
                        merged.steps = done_steps + new_plan.steps
                        merged.source_prompt = prompt
                        # P1：重规划合并后必须重新校验依赖图（此前直接采用，可能
                        # 引入重复 id/环/断链）；不通过先尝试自动修复，仍不通过
                        # 则拒绝采用该重规划、如实终止（绝不静默执行歧义图）。
                        replan_ok = True
                        if getattr(self.config, "plan_validation_enabled", True):
                            from agent_core.plan_validator import (
                                validate_plan, try_fix_plan)
                            rv = validate_plan(merged)
                            if not rv.is_valid and try_fix_plan(merged):
                                rv = validate_plan(merged)
                            if not rv.is_valid:
                                replan_ok = False
                                log.warning("重规划校验失败（%s），拒绝采用并终止",
                                            "; ".join(rv.issues))
                        if replan_ok:
                            plan = merged
                            mgr.save_plan(session_id, plan)
                            replanned = True
                            log.info("重新规划成功，新计划 %d 个步骤",
                                     len(plan.steps))
                            continue  # 继续步骤循环
                    else:
                        log.warning("重新规划未解析出有效计划，终止执行")
                result = acc.result(
                    output=(f"计划执行终止：步骤 {step.id} 重试 "
                            f"{_PLAN_STEP_MAX_RETRIES} 次后仍失败。\n"
                            f"失败步骤：{step.action}\n"
                            f"失败输出：{(last_output or '')[:_PLAN_STEP_OUTPUT_MAX_CHARS]}"),
                    session_id=session_id, is_error=True)
                result.meta["plan_status"] = "failed"
                result.meta["plan_failed_steps"] = [s.id for s in plan.steps
                                                     if s.status == "failed"]
                result.meta["plan"] = plan.to_dict()
                return result

        # 4. 步骤循环结束：仅当每个必需步骤都 done 才算成功（all_done）；否则
        #    如实报失败——含 failed 步骤，或因依赖未满足被阻塞而停留 pending/
        #    in_progress 的步骤（此前只查 failed，会把「被阻塞的 pending」漏报为完成）。
        #    is_error=True 同时让外层经验沉淀走失败口径，不再沉淀“成功”假经验。
        if not plan.all_done():
            not_done = [s for s in plan.steps if s.status != "done"]
            failed_ids = [s.id for s in plan.steps if s.status == "failed"]
            not_done_desc = "；".join(
                f"步骤 {s.id}（{s.action}）— {s.status}" for s in not_done)
            result = acc.result(
                output=(f"计划未全部完成：{not_done_desc}。\n"
                        f"计划目标：{plan.goal}"),
                session_id=session_id, is_error=True)
            result.meta["plan_status"] = "failed"
            result.meta["plan_failed_steps"] = failed_ids
            result.meta["plan"] = plan.to_dict()
            return result

        # 全部步骤完成：产出汇总（外层照常跑经验沉淀/反思）
        result = acc.result(output=_plan_summary(plan), session_id=session_id)
        result.meta["plan_status"] = "complete"
        result.meta["plan"] = plan.to_dict()
        return result

    async def _execute_oneshot(self, prompt: str, persona: Persona, cwd: str,
                               context_memory: str, cancel_event: Any,
                               retry_hint: str = "") -> KernelResult:
        """异步单轮执行：拼装用户消息前缀与 RAG 后消费 sdk.query() 消息流。

        retry_hint（退化重试纠偏提示）：非空时追加到用户消息末尾——
        前缀（工作目录/上下文记忆/RAG 经验块）保持稳定不变，保住
        provider 侧的 prompt caching（前置注入会破坏缓存）。
        """
        options = self._build_options(persona, cwd, query=prompt)
        # 前缀含任务工作目录/上下文记忆，调用方在其后追加「[任务]」段与真正的 prompt；
        # RAG 经验块最后拼接（最外层），与原 CLI 拼装顺序一致
        full_prompt = build_user_message_prefix(cwd, context_memory) + f"\n\n[任务]\n{prompt}"
        full_prompt = self.rag_enrich(full_prompt)
        if retry_hint:
            full_prompt = f"{full_prompt}\n\n{retry_hint}"
        return await self._run_sdk_query(full_prompt, options, cancel_event)

    def _guard_persona_with_model(self, persona: Persona, model: str) -> Persona:
        """复制 persona 并覆盖 model（退化第 2 次重试换 fallback_model 用）。

        _build_options 里 model 取 persona.model or config.model，覆盖 persona
        即等价于按次覆盖构造 options 的 model。
        """
        return Persona(name=persona.name, description=persona.description,
                       model=model, tools=persona.tools,
                       system_prompt=persona.system_prompt,
                       sub_agents=persona.sub_agents)

    async def _execute_oneshot_guarded(self, prompt: str, persona: Persona,
                                       cwd: str, context_memory: str,
                                       cancel_event: Any) -> KernelResult:
        """单轮执行 + 退化输出三级兜底（config.output_guard_enabled，默认开）。

        流程：执行 → detect_degenerate 检测（tool 轮无文本/取消/错误轮跳过）
        → 命中则按 config.output_guard_max_retries 重试：
        - 第 1 次重试：用户消息末尾追加纠偏提示，复用 _execute_oneshot；
        - 第 2 次重试：配置了 fallback_model 且与当前模型不同则换模型再试
          （构造 options 时按次覆盖 model），否则仍注入提示重试；
        - 每次重试前检查任务级预算：累计花费已达 task_budget_usd 的 80%
          则不再重试，按退化口径收场（meta 记 budget_guard_stop）——
          重试轮各自得完整 per-query 预算，不设闸门最坏 3× 预算。
        重试成功（检测通过）：正常返回，meta["degenerate_recovered"]=True 并记
        尝试次数；耗尽仍退化：熔断——is_error=True / error_type=
        "degenerate_output"，output 保留最后一次原始输出，meta["degenerate"]
        记详情（attempts/reasons/models_tried）；经验沉淀按 is_error 天然走失败口径。
        各尝试的 turns/usage/cost/duration 经 _RoundAccumulator 累加进最终结果。

        异常隔离：检测器/参数解析异常绝不吞掉已成功的正常结果——首次检测
        异常时 log.warning 并直接放行第一轮结果；重试轮检测异常按本轮通过
        （recovered）口径返回。
        """
        result = await self._execute_oneshot(prompt, persona, cwd, context_memory,
                                             cancel_event)
        if (not getattr(self.config, "output_guard_enabled", True)
                or result.cancelled or result.is_error
                or result.output in ("", "(无输出)")):
            # tool_use 密集而无文本的轮次（占位输出）与取消/错误轮不做检测
            return result
        try:
            verdict = detect_degenerate(result.output, prompt)
        except Exception as e:  # noqa: BLE001 - 检测异常放行，不拦截正常结果
            log.warning("退化输出检测异常，放行第一轮结果: %r", e)
            return result
        if not verdict.is_degenerate:
            if verdict.reason:  # warn 项（如短输出）透出便于排障，不触发兜底
                result.meta["degenerate_warn"] = verdict.reason
            return result

        acc = _RoundAccumulator()
        acc.absorb(result)
        reasons = [verdict.reason]
        self._update_guard_progress(acc, reasons)
        cur_model = persona.model or self.config.model or ""
        models_tried = [cur_model]
        try:
            max_retries = max(0, int(getattr(self.config,
                                             "output_guard_max_retries", 1)))
        except (TypeError, ValueError) as e:  # 参数非法不拦截正常结果
            log.warning("output_guard_max_retries 解析异常，放行第一轮结果: %r", e)
            return result
        fallback = (getattr(self.config, "fallback_model", "") or "").strip()
        budget = getattr(self.config, "task_budget_usd", None)
        last = result

        for attempt in range(1, max_retries + 1):
            if cancel_event is not None and cancel_event.is_set():
                return acc.result(output=last.output,
                                  session_id=last.session_id, cancelled=True)
            # 任务级预算闸门：task_budget_usd 是 per-query 语义，重试轮各自
            # 得完整预算（不设闸门最坏 3×）；累计花费已达 80% 则不再重试，
            # 按退化口径收场（输出已检出退化，绝不谎报成功）
            if budget and acc.cost_usd >= budget * _BUDGET_GUARD_STOP_RATIO:
                log.warning("退化重试前预算闸门生效：累计 $%.6f 达预算 $%.6f 的 "
                            "%.0f%%，停止重试", acc.cost_usd, budget,
                            _BUDGET_GUARD_STOP_RATIO * 100)
                fin = acc.result(output=last.output, session_id=last.session_id,
                                 is_error=True)
                fin.error_type = "degenerate_output"
                fin.meta["degenerate"] = {
                    "detected": True, "recovered": False,
                    "reasons": reasons, "attempts": attempt - 1,
                    "models_tried": models_tried,
                    "budget_guard_stop": True,
                    "spent_usd": round(acc.cost_usd, 6),
                    "budget_usd": budget,
                }
                return fin
            if attempt == 2 and fallback and fallback != cur_model:
                persona_try = self._guard_persona_with_model(persona, fallback)
                models_tried.append(fallback)
                cur_model = fallback
            else:
                persona_try = persona
            # hint 用最近一次检出原因（第 2 次重试纠正的是上一轮的病）
            hint = _GUARD_RETRY_HINT.format(reason=reasons[-1])
            retry = await self._execute_oneshot(prompt, persona_try, cwd,
                                                context_memory, cancel_event,
                                                retry_hint=hint)
            acc.absorb(retry)
            last = retry
            if retry.cancelled:
                return acc.result(output=retry.output,
                                  session_id=retry.session_id, cancelled=True)
            if retry.is_error:
                # 重试轮 SDK 错误：如实返回错误结果，不再检测文本
                fin = acc.result(output=retry.output, session_id=retry.session_id,
                                 is_error=True)
                fin.meta["degenerate"] = {
                    "detected": True, "recovered": False,
                    "reasons": reasons, "attempts": attempt,
                    "models_tried": models_tried,
                }
                return fin
            try:
                v2 = detect_degenerate(retry.output, prompt)
            except Exception as e:  # noqa: BLE001 - 重试轮检测异常按通过处理
                log.warning("重试轮退化检测异常，按 recovered 口径返回: %r", e)
                fin = acc.result(output=retry.output,
                                 session_id=retry.session_id)
                fin.meta["degenerate_recovered"] = True
                fin.meta["degenerate"] = {
                    "detected": True, "recovered": True,
                    "reasons": reasons, "attempts": attempt,
                    "models_tried": models_tried,
                    "note": "detection_error_pass",
                }
                return fin
            if not v2.is_degenerate:
                fin = acc.result(output=retry.output, session_id=retry.session_id)
                fin.meta["degenerate_recovered"] = True
                fin.meta["degenerate"] = {
                    "detected": True, "recovered": True,
                    "reasons": reasons, "attempts": attempt,
                    "models_tried": models_tried,
                }
                return fin
            reasons.append(v2.reason)
            self._update_guard_progress(acc, reasons)

        # 重试耗尽仍退化：熔断（质量守卫的最终兜底，绝不谎报成功）
        fin = acc.result(output=last.output, session_id=last.session_id,
                         is_error=True)
        fin.error_type = "degenerate_output"
        fin.meta["degenerate"] = {
            "detected": True, "recovered": False,
            "reasons": reasons, "attempts": max_retries,
            "models_tried": models_tried,
        }
        return fin

    def _update_guard_progress(self, acc: "_RoundAccumulator",
                               reasons: list[str]) -> None:
        """更新守卫执行进度快照（run_oneshot 超时路径透传用）。"""
        self._last_progress = {
            "turns": acc.turns,
            "usage": dict(acc.usage) if acc.usage else None,
            "cost_usd": round(acc.cost_usd, 6) if acc.has_cost else None,
            "degenerate_last_reason": reasons[-1] if reasons else "",
        }

    def _run_correction_loop(self, result: KernelResult, prompt: str,
                             persona: Persona, cwd: str, context_memory: str,
                             cancel_event: Any,
                             tracer: ExecutionTracer,
                             deadline: "_Deadline | None" = None) -> KernelResult:
        """自纠错闭环：对成功的执行结果做结构化验证，失败则修正重试。

        流程：validate_output -> 验证通过则直接返回 -> 验证失败则构造
        修正提示注入 prompt 重新执行 -> 最多重试 correction_max_retries 次。
        修正结果记入 result.meta["correction"]。

        真相口径（P0）：
        - 跨修正轮累计 turns/usage/cost/duration（_RoundAccumulator），成功的
          修正轮不再「替换」掉之前累计的用量（避免谎报低成本）；
        - 取消时把结果标为 cancelled，绝不返回之前的「成功」结果；
        - 重试耗尽后若最终验证为高严重度结构性错误（如 Python 语法错），
          视为「必需验证失败」-> is_error=True，不得谎报成功；中/低严重度
          （JSON 格式、启发式覆盖度）为弱信号，记入 meta.verification 但不翻转
          is_error，避免启发式误判把正常任务判失败。
        - 传入 deadline（整任务墙钟截止点）时，每个修正轮受剩余预算约束；
          预算耗尽则停止纠错并在 meta 标 deadline_exceeded，绝不无限延长总时长。
        """
        try:
            max_retries = max(0, int(getattr(self.config,
                                             "correction_max_retries", 2)))
        except (TypeError, ValueError):
            return result

        acc = _RoundAccumulator()
        acc.absorb(result)
        deadline_exceeded = False
        attempts_done = 0

        def _finalize(base: KernelResult, correction: dict,
                      verification: dict | None = None,
                      is_error: bool | None = None,
                      error_type: str = "",
                      cancelled: bool = False) -> KernelResult:
            fin = acc.result(output=base.output, session_id=base.session_id,
                             is_error=(base.is_error if is_error is None else is_error),
                             cancelled=cancelled)
            fin.meta = {**base.meta, "correction": correction}
            if verification is not None:
                fin.meta["verification"] = verification
            if error_type:
                fin.error_type = error_type
            return fin

        for attempt in range(1, max_retries + 1):
            if cancel_event is not None and cancel_event.is_set():
                return _finalize(result, {"cancelled": True, "attempts": attempt - 1},
                                 cancelled=True)
            with tracer.span("correction", "correction") as span:
                verdict = validate_output(result.output, prompt, cwd)
                span.end(issues=verdict.issues, attempt=attempt,
                         is_valid=verdict.is_valid)
                if verdict.is_valid:
                    return _finalize(
                        result,
                        {"validated": True, "attempts": attempt,
                         "checks_run": verdict.checks_run},
                        verification={"status": "passed",
                                      "severity": verdict.severity,
                                      "checks_run": verdict.checks_run})
                # 验证失败需修正重试。先卡整任务截止点：剩余预算耗尽则不再
                # 开启新修正轮（卡住的纠错不得把总时长拖过 task_timeout）。
                remaining = deadline.remaining() if deadline is not None else None
                if remaining is not None and remaining <= 0:
                    deadline_exceeded = True
                    break
                hint = build_correction_hint(verdict)
                corrected_prompt = f"{prompt}\n\n{hint}"
                coro = self._execute_oneshot(corrected_prompt, persona, cwd,
                                             context_memory, cancel_event)
                if remaining is not None:
                    # 修正轮也受剩余预算约束，不获得无限期的新超时。
                    coro = asyncio.wait_for(coro, timeout=remaining)
                try:
                    retry_result = self._run_async(coro)
                except asyncio.TimeoutError:
                    deadline_exceeded = True
                    break
                except Exception:  # noqa: BLE001
                    return _finalize(result, {"validated": False, "attempts": attempt,
                                              "error": "correction_execution_failed"})
                acc.absorb(retry_result)
                attempts_done = attempt
                if retry_result.cancelled:
                    return _finalize(retry_result,
                                     {"cancelled": True, "attempts": attempt},
                                     cancelled=True)
                if retry_result.is_error:
                    # 修正轮 SDK 错误：如实返回错误结果（累计用量保留）
                    return _finalize(retry_result,
                                     {"validated": False, "attempts": attempt,
                                      "correction_failed": True},
                                     is_error=True,
                                     error_type=retry_result.error_type)
                result = retry_result
        # 重试耗尽 / 截止点到期：对最后一次结果做最终验证
        final_verdict = validate_output(result.output, prompt, cwd)
        verification = {"status": "passed" if final_verdict.is_valid else "failed",
                        "severity": final_verdict.severity,
                        "issues": final_verdict.issues[:5],
                        "checks_run": final_verdict.checks_run}
        correction = {"validated": final_verdict.is_valid, "attempts": attempts_done,
                      "remaining_issues": final_verdict.issues[:5]}
        if deadline_exceeded:
            # 截止点到期：纠错未能在整任务预算内完成，如实标注（不谎报已验证）。
            verification["deadline_exceeded"] = True
            correction["deadline_exceeded"] = True
        hard_fail = (not final_verdict.is_valid) and final_verdict.severity == "high"
        return _finalize(
            result, correction,
            verification=verification,
            is_error=(result.is_error or hard_fail),
            error_type="validation_failed" if hard_fail else "")

    async def _run_sdk_query(self, full_prompt: str, options: Any,
                             cancel_event: Any) -> KernelResult:
        """消费 sdk.query() 的消息流（prompt 已拼装好），聚合为 KernelResult。

        从 _execute_oneshot 拆出的消息循环：单轮执行与 Plan-Execute 引擎
        （规划轮/步骤轮）共用，保证指标收集与取消语义一致。
        """
        import claude_agent_sdk as sdk

        assistant_texts: list[str] = []
        final_result: str | None = None
        turns = 0
        is_error = False
        usage: dict | None = None
        cost_usd: float | None = None
        duration_ms = 0
        session_id: str | None = None
        saw_result = False  # 是否收到终结 ResultMessage（缺失=流异常提前结束）

        async for msg in sdk.query(prompt=full_prompt, options=options):
            name = type(msg).__name__
            if name == "AssistantMessage":
                for block in getattr(msg, "content", []) or []:
                    if type(block).__name__ == "TextBlock":
                        assistant_texts.append(getattr(block, "text", "") or "")
            elif name == "ResultMessage":
                saw_result = True
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
        # 未见终结 ResultMessage：SDK 流异常提前结束（子进程崩溃/被杀/协议中断）。
        # 绝不谎报成功——标记 is_error + error_type="no_result"，保留已收文本供排障。
        no_result = not saw_result and not is_error
        kr = KernelResult(
            output=output, turns=turns, is_error=is_error or no_result, usage=usage,
            cost_usd=cost_usd, duration_ms=duration_ms, session_id=session_id,
        )
        if no_result:
            kr.error_type = "no_result"
            kr.meta["no_result"] = True
        return kr

    def open_session(self, persona: Persona, cwd: str) -> AgentSession:
        """创建多轮交互会话（未连接；调用方需 await session.connect()）。"""
        return AgentSession(self, persona, cwd)

    def rag_enrich(self, prompt: str, context_budget: int | None = None) -> str:
        """RAG 增强（内核统一 prompt 拼装点）：按 config.rag_enabled 检索历史

        经验并拼接到 prompt 前。cli.py 的同名逻辑已下沉至此，oneshot / stream /
        session 三种运行路径同等受益；关闭时原样返回，行为不变。

        context_budget（可选）：动态 RAG 预算（字符数）。
        """
        return _rag_enrich(prompt, self.config, context_budget=context_budget)

    async def stream(self, prompt: str, persona: Persona, cwd: str,
                     context_memory: str = "") -> AsyncIterator[AgentEvent]:
        """流式执行，边消费边 yield AgentEvent。供 CLI 打印和 HTTP SSE 使用。

        墙钟截止（P0）：与 oneshot 同口径，取 config.task_timeout 为整轮上限
        （非正数视为不限）；超时转 error + 终结 result（is_error）事件收场，
        不再无限等待卡住的后端。流结束仍未见终结 ResultMessage 时同样补发
        error + result（error_type=no_result），绝不把半截流当成功。
        """
        import claude_agent_sdk as sdk

        # Prompt Injection 检测（流式入口）
        injection_result = self._check_prompt_injection(prompt)
        if injection_result is not None:
            yield AgentEvent("error", injection_result.output)
            yield AgentEvent("result", {
                "result": injection_result.output, "num_turns": 0,
                "is_error": True, "usage": None, "cost_usd": None,
                "duration_ms": 0, "session_id": None,
                "injection": injection_result.meta.get("injection"),
            })
            return

        options = self._build_options(persona, cwd, query=prompt)
        full_prompt = build_user_message_prefix(cwd, context_memory) + f"\n\n[任务]\n{prompt}"
        full_prompt = self.rag_enrich(full_prompt)
        flt = _StreamFilter(partial_text=bool(self.config.streaming))
        task_timeout = getattr(self.config, "task_timeout", None)
        deadline = None
        if isinstance(task_timeout, (int, float)) and task_timeout > 0:
            deadline = time.monotonic() + task_timeout
        saw_result = False
        try:
            ait = sdk.query(prompt=full_prompt, options=options).__aiter__()
            while True:
                try:
                    if deadline is None:
                        msg = await ait.__anext__()
                    else:
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            raise asyncio.TimeoutError()
                        msg = await asyncio.wait_for(ait.__anext__(), remaining)
                except StopAsyncIteration:
                    break
                except (TimeoutError, asyncio.TimeoutError):
                    # 墙钟超时转 error + 终结 result（is_error），不裸抛、不静默截断
                    flt.reset()
                    yield AgentEvent("error", f"流式执行超时（>{task_timeout}s）。"
                                             "建议：调大 task_timeout 或检查后端负载。")
                    yield AgentEvent("result", {
                        "result": "", "num_turns": 0, "is_error": True,
                        "usage": None, "cost_usd": None, "duration_ms": 0,
                        "session_id": None, "error_type": "timeout"})
                    return
                for ev in flt.translate(msg):
                    if ev.type == "result":
                        saw_result = True
                    yield ev
        except Exception as e:  # noqa: BLE001 - 流式异常转 error 事件（人性化）
            flt.reset()  # 流结束/异常路径统一复位去重标志，防残留误吞后续轮次
            error_type, message, hint = humanize_sdk_error(e)
            yield AgentEvent("error", f"{message}\n原始错误：{e}\n建议：{hint}")
            return
        # 流正常结束但未见终结 ResultMessage：SDK 流异常提前结束（子进程崩溃/
        # 被杀/协议中断）。绝不把半截流当成功——补发 error + result（no_result）。
        if not saw_result:
            yield AgentEvent("error", "流式执行未收到终结结果（后端可能崩溃或被中断）。")
            yield AgentEvent("result", {
                "result": "", "num_turns": 0, "is_error": True,
                "usage": None, "cost_usd": None, "duration_ms": 0,
                "session_id": None, "error_type": "no_result"})

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
