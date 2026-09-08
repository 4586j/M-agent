#!/usr/bin/env python3
"""单机 Agent 交互 CLI（包内模块）。

定位：单机模式下用户直接使用的 Agent 命令行，交互体验对标 Claude Code / Aider。
底层委托 ``agent_core.kernel.AgentKernel`` + ``AgentSession`` 实现**真正的多轮会话**
（长连接客户端维持上下文），不再走 orch_cli 的 query+resume 单发拼接模式。

本模块由 ``agent_cli.py`` 迁移而来（保持功能等价），并作为 pip 可安装命令入口：
``pyproject.toml`` 的 ``[project.scripts]`` 暴露 ``agent`` / ``agent-cli`` 两个命令，
均指向 ``agent_core.cli:main``。根目录 ``agent_cli.py`` 退化为薄 wrapper。

三种运行形态：
- 交互 REPL（默认）：连续多轮对话，slash 命令控制会话；tty 且未设 NO_COLOR 时
  启用 **rich + prompt_toolkit 富终端界面**（流式 Markdown、工具面板、spinner、
  命令补全、多行输入、历史），否则回退到朴素流式 print；
- headless 单发：``agent "任务"``，跑一轮即退出（脚本/CI 可用），**恒为纯文本**；
- ``--help``：在未安装 SDK / 未连接时也能正常打印（SDK 延迟导入）。

设计约束：
- **不改 kernel.py**：仅消费既有 AgentEvent 流（text_delta / tool_use /
  tool_result / result / error），事件负载统一从 ``ev.data`` 读取；
- **不硬编码绝对路径**：项目根优先取环境变量，否则基于 cwd 向上探测锚点
  （agent_core/ 或 workspace/ 或 .mcp.json），最后回退到包所在目录的上一级；
- **富库可选**：rich / prompt_toolkit 用 try/except 保护导入，缺失或导入失败时
  优雅降级为朴素模式，绝不让 CLI 直接崩溃；
- 整个会话在单一 asyncio 事件循环内驱动（connect/send/close 共享一个 loop）。
"""
import argparse
import asyncio
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

from agent_core.config import AgentConfig, load_agent_config
from agent_core.context import load_core_memories_block
from agent_core.kernel import AgentKernel
from agent_core.persona import (
    Persona,
    load_persona_by_name,
    parse_subagents,
    strip_frontmatter,
)
from agent_core.planning import (
    PlanningManager,
    build_planning_prompt,
    parse_plan_from_output,
)
from agent_core.tools import load_mcp_servers, scan_skills

# ---- 富终端依赖（可选，导入失败即降级为朴素模式） ----
# 只有 rich 与 prompt_toolkit 都可用时才启用富交互；任一缺失/报错都置 _RICH_OK=False。
_RICH_OK = False
try:  # pragma: no cover - 取决于运行环境是否装了 rich / prompt_toolkit
    from rich import box
    from rich.console import Console, Group
    from rich.live import Live
    from rich.markdown import Markdown
    from rich.panel import Panel
    from rich.spinner import Spinner
    from rich.table import Table
    from rich.text import Text

    from prompt_toolkit import PromptSession
    from prompt_toolkit.completion import Completer, Completion
    from prompt_toolkit.formatted_text import HTML
    from prompt_toolkit.history import InMemoryHistory
    from prompt_toolkit.key_binding import KeyBindings
    from prompt_toolkit.shortcuts import CompleteStyle

    _RICH_OK = True
except Exception:  # noqa: BLE001 - 富库缺失/损坏时降级为朴素模式，不阻断 CLI
    _RICH_OK = False


# ---- 项目根与 MCP 配置发现（不硬编码绝对路径） ----

# 向上探测项目根的锚点：命中任一即认定为项目根
_ROOT_ANCHORS = ("agent_core", "workspace", ".mcp.json")


def _package_root() -> str:
    """包所在目录的上一级（agent_core/cli.py -> 项目根）。

    编辑安装时即真实仓库根；作为 env 与 cwd 探测都失败时的最后兜底。
    """
    return str(Path(__file__).resolve().parents[1])


def _discover_project_root() -> str:
    """定位项目根：环境变量优先，其次 cwd 向上探测锚点，最后回退包根。

    顺序保证「在仓库根执行 agent（编辑安装）」与「python agent_cli.py」发现到
    相同的技能/persona/mcp：
    1) AGENT_PROJECT_ROOT / ORCH_PROJECT_ROOT 若已设，直接采用；
    2) 否则从当前工作目录逐级向上，命中含 agent_core/ 或 workspace/ 或
       .mcp.json 的目录即返回（不硬编码任何绝对路径）；
    3) 都不满足（如非编辑安装到 site-packages 且 cwd 不在仓库内）回退包根。
    """
    for name in ("AGENT_PROJECT_ROOT", "ORCH_PROJECT_ROOT"):
        val = os.environ.get(name)
        if val:
            return val
    try:
        cur = Path.cwd().resolve()
        for cand in (cur, *cur.parents):
            if any((cand / a).exists() for a in _ROOT_ANCHORS):
                return str(cand)
    except Exception:  # noqa: BLE001 - cwd 不可解析等极端情形直接走兜底
        pass
    return _package_root()


def _resolve_mcp_config(project_root: str) -> str:
    """解析 MCP 配置路径：环境变量优先，否则自动发现 <root>/.mcp.json。

    与 load_agent_config 的 mcp_config_path 语义对齐；env 未设且仓库根存在
    .mcp.json 时自动采用，使两种入口发现到相同的 MCP 服务。都无则返回 ""。
    """
    for name in ("AGENT_MCP_CONFIG", "ORCH_MCP_CONFIG"):
        val = os.environ.get(name)
        if val:
            return val
    cand = Path(project_root) / ".mcp.json"
    if cand.is_file():
        return str(cand)
    return ""


# ---- 输出辅助（NO_COLOR / tty 友好的弱化样式，朴素模式沿用） ----

def _dim_enabled() -> bool:
    """是否启用弱化色：stderr 为 tty 且未设 NO_COLOR。"""
    return sys.stderr.isatty() and not os.environ.get("NO_COLOR")


def dim(text: str) -> str:
    """弱化样式包装（非 tty / NO_COLOR 时原样返回）。"""
    return f"\033[2m{text}\033[0m" if _dim_enabled() else text


# ---- 会话持久化 ----

def _default_session_file() -> Path:
    """默认会话持久化文件：<项目根>/.agent_session。"""
    return Path(_discover_project_root()) / ".agent_session"


def save_session(session_id: str | None, path: str | Path | None = None) -> None:
    """把 session_id 写入持久化文件；空值忽略。"""
    if not session_id:
        return
    p = Path(path) if path else _default_session_file()
    p.write_text(json.dumps({"session_id": session_id}), encoding="utf-8")


def load_session(path: str | Path | None = None) -> str | None:
    """读取上次持久化的 session_id；不存在/损坏返回 None。"""
    try:
        p = Path(path) if path else _default_session_file()
        return json.loads(p.read_text(encoding="utf-8")).get("session_id")
    except Exception:  # noqa: BLE001 - 无历史会话是正常情形
        return None


# ---- RAG 检索增强 ----

# 拼接到 prompt 前的经验块限幅（字符）：上下文是稀缺注意力预算，防噪声注入
_RAG_BLOCK_MAX_CHARS = 3000


def _rag_enrich(prompt: str, config: AgentConfig) -> str:
    """RAG 增强：检索相似历史经验并拼接到 prompt 前。

    仅在 config.rag_enabled 为真时生效；否则原样返回 prompt。延迟导入
    runtime 的 ExperienceStore（把 runtime 目录加入 sys.path），检索失败或
    无相似经验时静默回退到原 prompt，不拖垮 REPL。
    """
    if not getattr(config, "rag_enabled", False) or not prompt:
        return prompt
    try:
        rt = (Path(config.project_root) / "workspace" / "agent"
              / "orchestrator" / "runtime")
        if str(rt) not in sys.path:
            sys.path.insert(0, str(rt))
        from experience_store import ExperienceStore
        try:
            from dispatch_common import clamp_context
        except Exception:  # noqa: BLE001 - clamp_context 不可用时降级为直接截断
            clamp_context = None

        store = ExperienceStore(config.experience_store_dir)
        similar = store.search(prompt, successful_only=True, limit=3)
        if not similar:
            return prompt
        lines = ["[历史经验参考]"]
        for i, exp in enumerate(similar, 1):
            lines.append(f"{i}. 任务: {exp.prompt[:100]}")
            if exp.output_summary:
                lines.append(f"   产出: {exp.output_summary[:200]}")
        block = "\n".join(lines)
        if clamp_context is not None:
            block = clamp_context(block, _RAG_BLOCK_MAX_CHARS)
        elif len(block) > _RAG_BLOCK_MAX_CHARS:
            block = block[:_RAG_BLOCK_MAX_CHARS] + "\n[经验块已截断]"
        return f"{block}\n\n{prompt}"
    except Exception:  # noqa: BLE001 - RAG 失败不影响主流程
        return prompt


# ---- 人设解析 ----

def _build_orchestrator_persona(config: AgentConfig) -> Persona:
    """构造 orchestrator（大脑）人设：orchestrator SKILL.md 正文 + 子 agent 注册。

    等价于 orch_cli 的大脑姿态：加载编排工作流作为系统提示，并把
    .claude/agents/ 下的 executor / security-guard 作为子 agent 供 Agent 工具派发。
    """
    skill_md = (Path(config.project_root) / "workspace" / "agent" / "skills"
                / "orchestrator" / "SKILL.md")
    body = "你是 orchestrator，多 agent 编排框架的大脑。"
    if skill_md.is_file():
        body += "\n\n" + strip_frontmatter(skill_md.read_text(encoding="utf-8"))
    return Persona(
        name="orchestrator",
        description="编排大脑：拆解目标、派发子 agent、汇总裁决",
        model=None,
        tools=None,
        system_prompt=body,
        sub_agents=parse_subagents(config.persona_dir),
    )


def resolve_persona(name: str, config: AgentConfig) -> Persona:
    """按名称解析人设。

    - 优先在 persona_dir 下找 <name>.md（executor / security-guard 等）；
    - name == orchestrator 且无定义文件时，回退构造大脑人设（等同 orch_cli）；
    - 其余未知名给最小人设兜底，避免直接崩溃。
    """
    try:
        return load_persona_by_name(name, config.persona_dir)
    except FileNotFoundError:
        if name == "orchestrator":
            return _build_orchestrator_persona(config)
        return Persona(
            name=name,
            description=f"{name}（未找到定义文件，使用最小人设）",
            system_prompt=f"你是 {name}。请按用户指令完成任务。",
        )


# ---- slash 命令清单（补全与 /help 共用） ----

# (命令, 说明)；/exit 与 /quit 等价，补全里都给出
_COMMANDS: list[tuple[str, str]] = [
    ("/help", "查看本帮助"),
    ("/new", "开新会话（清空上下文，重连客户端）"),
    ("/cost", "显示本次累计 turns 与 cost"),
    ("/tools", "列出人设工具、技能、MCP 服务与记忆工具"),
    ("/plan", "显示当前规划进度（--planning 模式下有意义）"),
    ("/memory", "查看核心记忆（跨任务持久）"),
    ("/interrupt", "中断当前正在执行的一轮"),
    ("/exit", "退出（等价 /quit）"),
    ("/quit", "退出"),
]

_HELP_TEXT = """命令：
  /help        查看本帮助
  /new         开新会话（清空上下文，重连客户端）
  /cost        显示本次累计 turns 与 cost
  /tools       列出人设工具、技能、MCP 服务与记忆工具
  /plan        显示当前规划进度（--planning 模式下有意义）
  /memory      查看核心记忆（跨任务持久）
  /interrupt   中断当前正在执行的一轮
  /exit /quit  退出
其余输入都作为消息发送给 Agent；长对话由内核自动压缩。"""


# ---- 会话运行时状态 ----

class CliState:
    """REPL 运行期状态：持有会话、人设、配置、累计用量与落地路径。"""

    def __init__(self, kernel: AgentKernel, persona: Persona, config: AgentConfig,
                 cwd: str, planning: bool, session: Any = None,
                 session_file: str | Path | None = None,
                 progress_dir: str | Path | None = None):
        self.kernel = kernel
        self.persona = persona
        self.config = config
        self.cwd = cwd
        self.planning = planning
        self.session = session
        self.stop = False
        root = Path(config.project_root or _discover_project_root())
        self.session_file = Path(session_file) if session_file else root / ".agent_session"
        progress = Path(progress_dir) if progress_dir else root / ".agent_progress"
        self.planning_mgr = PlanningManager(progress) if planning else None

    @property
    def session_key(self) -> str:
        """规划进度文件的键：优先真实 session_id，否则用 default。"""
        sid = self.session.session_id if self.session else None
        return sid or "default"


async def _open_session(state: CliState, resume_id: str | None = None) -> None:
    """创建并连接一个新的 AgentSession；resume_id 非空时续接历史会话。"""
    session = state.kernel.open_session(state.persona, state.cwd)
    if resume_id:
        # AgentSession.connect() 会以 _session_id 作为 resume 目标续接
        session._session_id = resume_id  # noqa: SLF001 - CLI 复用内核续接语义
    await session.connect()
    state.session = session


# ---- 内容截断辅助（富终端工具面板/结果摘要用） ----

def _summarize_input(inp: Any, limit: int = 160) -> str:
    """把工具入参压缩为单行紧凑摘要，超长截断。dict 展开为 k=v。"""
    if inp is None:
        return ""
    if isinstance(inp, dict):
        if not inp:
            return ""
        parts = []
        for k, v in inp.items():
            vs = v if isinstance(v, str) else json.dumps(v, ensure_ascii=False,
                                                         default=str)
            vs = " ".join(str(vs).split())  # 折叠空白/换行
            if len(vs) > 60:
                vs = vs[:57] + "…"
            parts.append(f"{k}={vs}")
        s = "  ".join(parts)
    else:
        s = " ".join(str(inp).split())
    if len(s) > limit:
        s = s[:limit - 1] + "…"
    return s


def _summarize_result(content: Any, limit: int = 120) -> str:
    """把工具返回内容压成单行摘要（截断），避免刷屏巨量结果。

    兼容 str / list[block] / dict / 其它：统一抽文本、折叠空白、按 limit 截断。
    """
    if content is None:
        return ""
    if isinstance(content, str):
        s = content
    elif isinstance(content, list):
        chunks = []
        for item in content:
            if isinstance(item, dict):
                chunks.append(str(item.get("text") or item.get("content") or item))
            else:
                chunks.append(str(item))
        s = " ".join(chunks)
    elif isinstance(content, dict):
        s = str(content.get("text") or content.get("content") or content)
    else:
        s = str(content)
    s = " ".join(s.split())  # 折叠空白/换行为单行
    if len(s) > limit:
        s = s[:limit - 1] + "…"
    return s


def _fmt_cost(v: Any) -> str:
    """把 cost 数值格式化为 $x.xxxx；非法值显示破折号。"""
    try:
        return f"${float(v):.4f}"
    except (TypeError, ValueError):
        return "—"


def _format_plan(plan: Any) -> str:
    """把 Plan 渲染为 /plan 的人类可读进度视图（朴素文本）。

    每个步骤用状态标记：[✓] 已完成、[→] 进行中、[✗] 失败、[ ] 待做（附依赖）。
    """
    lines = ["当前计划：", f"  目标：{plan.goal or '(未指定)'}", "  步骤："]
    if not plan.steps:
        lines.append("    (无步骤)")
    for s in plan.steps:
        if s.status == "done":
            mark, suffix = "✓", ""
        elif s.status == "in_progress":
            mark, suffix = "→", " (进行中)"
        elif s.status == "failed":
            mark, suffix = "✗", " (失败)"
        else:
            mark = " "
            suffix = " (待做)" if not s.depends_on else ""
            if s.depends_on:
                deps = ", ".join(str(d) for d in s.depends_on)
                suffix = f" (待做，依赖: {deps})"
        lines.append(f"    [{mark}] {s.id}. {s.action}{suffix}")
    if plan.risks:
        lines.append("  风险：" + "；".join(plan.risks))
    return "\n".join(lines)


# ---- 渲染层：朴素 UI 与富终端 UI ----
#
# 两套 UI 暴露同一组 hook，stream_turn / handle_command / repl 共用同一份事件
# 消费与命令分发逻辑，只在「怎么显示」上分叉：
# - PlainUI：与原 agent_cli.py 输出逐字一致（headless / 非 tty / NO_COLOR /
#   富库缺失时使用）；
# - RichUI：rich.Live 渐进渲染 Markdown + 工具面板 + spinner + dim 统计。

class PlainUI:
    """朴素文本 UI：逐字复刻原 agent_cli.py 的流式 print 行为。"""

    rich = False

    # -- 流式 hook --
    def turn_start(self) -> None:
        pass

    def text(self, chunk: str) -> None:
        print(chunk, end="", flush=True)

    def tool_use(self, name: Any, inp: Any) -> None:
        print(dim(f"\n  · ⚙ {name}"), flush=True)

    def tool_result(self, content: Any) -> None:
        print(dim("  · ✓ 工具返回"), flush=True)

    def error(self, msg: Any) -> None:
        # 流内 error 事件：与原文一致内联打印，不打断后续事件消费
        print(f"\n[执行异常] {msg}")

    def fatal(self, msg: Any) -> None:
        # 轮级异常（session.send 抛出）：由 REPL 捕获后打印
        print(f"\n[执行异常] {msg}")

    def interrupted(self) -> None:
        print(dim("\n[已中断本轮]"))

    def turn_end(self) -> None:
        print()  # 收尾换行

    # -- 信息 / 命令输出 --
    def info(self, msg: str) -> None:
        print(dim(msg))

    def banner(self, state: "CliState") -> None:
        cfg = state.config
        persona = state.persona
        model = persona.model or cfg.model or "SDK默认"
        tool_count = len(persona.tools) if persona.tools else "全部"
        mcp_count = len(load_mcp_servers(cfg.mcp_config_path) or {})
        core_dir = Path(cfg.memory_core_dir)
        mem_count = len(list(core_dir.glob("*.md"))) if core_dir.is_dir() else 0
        skills = ", ".join(s["name"] for s in scan_skills(cfg.project_root)) or "无"

        print("agent CLI · 单机 Agent 交互模式（多轮会话已就绪）")
        print(dim(f"  人设: {persona.name}  模型: {model}  权限: {cfg.permission_mode}"))
        print(dim(f"  工具数: {tool_count}  MCP服务: {mcp_count}  "
                  f"核心记忆: {mem_count} 条  技能: {skills}"))
        plan_hint = "规划模式: 开" if state.planning else "规划模式: 关"
        print(dim(f"  {plan_hint} · /help 查看命令"))

    def help(self) -> None:
        print(_HELP_TEXT)

    def tools(self, state: "CliState") -> None:
        persona = state.persona
        cfg = state.config
        tools_desc = "、".join(persona.tools) if persona.tools else "全部工具（未收窄）"
        print(f"人设：{persona.name} — {persona.description or '(无描述)'}")
        print(f"工具：{tools_desc}")
        skills = scan_skills(cfg.project_root)
        print("技能：" + ("、".join(s["name"] for s in skills) if skills else "无"))
        mcp = load_mcp_servers(cfg.mcp_config_path) or {}
        print("MCP 服务：" + ("、".join(mcp.keys()) if mcp else "无"))
        print("记忆工具：search_experiences、read_core_memory、"
              "write_core_memory、write_task_context")

    def plan(self, plan: Any) -> None:
        print(_format_plan(plan))

    def cost(self, turns: int, total: float) -> None:
        print(dim(f"本次会话累计：turns={turns} cost=${total:.4f}"))

    def memory(self, text: str) -> None:
        print(text or "（核心记忆为空）")

    def turn_stats(self, info: dict, session: Any) -> None:
        turns = info.get("num_turns") or 0
        cost = session.total_cost if session else 0.0
        print(dim(f"[turns={turns} 累计cost=${cost:.4f}]"))

    def oneshot_stats(self, info: dict, session: Any) -> None:
        turns = info.get("num_turns") or 0
        cost = session.total_cost if session else 0.0
        print(dim(f"\n[turns={turns} cost=${cost:.4f}]"))


if _RICH_OK:  # pragma: no cover - 依赖富库是否可用

    class SlashCompleter(Completer):
        """slash 命令补全：仅对以 / 开头的第一个 token 生效，附命令说明。"""

        def __init__(self, commands: list[tuple[str, str]]):
            self.commands = commands

        def get_completions(self, document, complete_event):
            text = document.text_before_cursor
            if not text.startswith("/"):
                return
            if " " in text.strip():  # 已进入参数区，不再补全命令名
                return
            for cmd, desc in self.commands:
                if cmd.startswith(text):
                    yield Completion(cmd, start_position=-len(text),
                                     display=cmd, display_meta=desc)


class RichUI:
    """Claude Code 风格富终端 UI（仅在 _RICH_OK 时实例化）。

    流式文本用 rich.Live 渐进渲染 Markdown（auto_refresh，≈10 fps），工具调用
    用紧凑 Panel、结果用 dim 单行摘要，等待期用 Spinner；本轮结束停掉 Live 再
    输出一次最终干净渲染，避免闪烁/重复残留。
    """

    rich = True

    def __init__(self, console: Any = None):
        self.console = console or Console()
        self._live: Any = None
        self._committed: list[Any] = []  # 已定型的块（工具面板/结果行/静态 Markdown）
        self._buf = ""                   # 当前流式文本缓冲
        self._status: str | None = None  # spinner 文案；None 表示不显示 spinner
        self._last_update = 0.0

    # -- Live 内部渲染 --
    def _spinner(self) -> Any:
        return Spinner("dots", text=Text(self._status or "思考中…",
                                         style="dim italic"))

    def _group(self) -> Any:
        parts = list(self._committed)
        if self._buf:
            parts.append(Markdown(self._buf))
        elif self._status:
            parts.append(self._spinner())
        return Group(*parts) if parts else Text("")

    def _refresh(self, force: bool = False) -> None:
        if self._live is None:
            return
        now = time.monotonic()
        # 文本增量按 ~12fps 节流重建 Markdown，避免每 chunk 全量解析导致 O(n^2)
        if force or (now - self._last_update) >= 0.08:
            self._last_update = now
            try:
                self._live.update(self._group())
            except Exception:  # noqa: BLE001 - 渲染异常不拖垮会话
                pass

    def _stop_live(self) -> None:
        if self._live is not None:
            try:
                self._live.stop()
            except Exception:  # noqa: BLE001
                pass
            self._live = None

    def _tool_panel(self, name: Any, inp: Any) -> Any:
        args = _summarize_input(inp)
        title = Text.assemble(("⚙ ", "yellow"), (str(name or "tool"), "bold cyan"))
        body = (Text(args, style="dim") if args
                else Text("（无参数）", style="dim italic"))
        return Panel(body, title=title, border_style="cyan", expand=False,
                     padding=(0, 1), title_align="left")

    # -- 流式 hook --
    def turn_start(self) -> None:
        self._committed = []
        self._buf = ""
        self._status = "思考中…"
        self._last_update = 0.0
        try:
            self._live = Live(self._group(), console=self.console,
                              auto_refresh=True, refresh_per_second=10,
                              transient=True, vertical_overflow="ellipsis")
            self._live.start()
        except Exception:  # noqa: BLE001 - Live 起不来就退化为直接打印
            self._live = None

    def text(self, chunk: str) -> None:
        self._status = None  # 正在流式输出文本，撤掉 spinner
        self._buf += chunk
        self._refresh()

    def tool_use(self, name: Any, inp: Any) -> None:
        if self._buf:  # 先把已流出的文本定型为静态 Markdown
            self._committed.append(Markdown(self._buf))
            self._buf = ""
        self._committed.append(self._tool_panel(name, inp))
        self._status = "执行工具中…"
        self._refresh(force=True)

    def tool_result(self, content: Any) -> None:
        summary = _summarize_result(content)
        self._committed.append(
            Text.assemble(("  ✓ ", "green"), (summary or "（无内容）", "dim")))
        self._status = "思考中…"  # 等模型继续
        self._refresh(force=True)

    def error(self, msg: Any) -> None:
        # 流内 error 事件：保留 Live，把红色 Panel 追加进已定型块
        self._status = None
        self._committed.append(
            Panel(Text(str(msg)), title="执行异常", border_style="red",
                  expand=False))
        self._refresh(force=True)

    def fatal(self, msg: Any) -> None:
        # 轮级异常：停掉 Live 再单独渲染红色 Panel
        self._stop_live()
        self.console.print(Panel(Text(str(msg)), title="执行异常",
                                 border_style="red", expand=False))

    def interrupted(self) -> None:
        self._stop_live()
        self.console.print(Text("[已中断本轮]", style="dim"))

    def turn_end(self) -> None:
        self._stop_live()
        final = list(self._committed)
        if self._buf:
            final.append(Markdown(self._buf))
        if final:  # 一次性输出最终干净渲染，避免与 Live 残影重复
            self.console.print(Group(*final))

    # -- 信息 / 命令输出 --
    def info(self, msg: str) -> None:
        self.console.print(Text(msg, style="dim"))

    def banner(self, state: "CliState") -> None:
        cfg = state.config
        persona = state.persona
        model = persona.model or cfg.model or "SDK默认"
        tool_count = len(persona.tools) if persona.tools else "全部"
        mcp_count = len(load_mcp_servers(cfg.mcp_config_path) or {})
        core_dir = Path(cfg.memory_core_dir)
        mem_count = len(list(core_dir.glob("*.md"))) if core_dir.is_dir() else 0
        skills = ", ".join(s["name"] for s in scan_skills(cfg.project_root)) or "无"
        plan_hint = "开" if state.planning else "关"

        body = Text()
        body.append("人设: ", style="bold")
        body.append(f"{persona.name}", style="cyan")
        body.append("   模型: ", style="bold")
        body.append(f"{model}", style="cyan")
        body.append("   权限: ", style="bold")
        body.append(f"{cfg.permission_mode}", style="cyan")
        body.append("\n")
        body.append(f"工具数: {tool_count}   MCP服务: {mcp_count}   "
                    f"核心记忆: {mem_count} 条", style="dim")
        body.append("\n")
        body.append(f"技能: {skills}", style="dim")
        body.append("\n")
        body.append(f"规划模式: {plan_hint} · /help 查看命令 · "
                    r"多行: 结尾 \ 回车 / Alt+Enter", style="dim italic")
        self.console.print(Panel(
            body, title="agent · 单机 Agent 交互模式（多轮会话已就绪）",
            border_style="bright_blue", expand=False, title_align="left"))

    def help(self) -> None:
        table = Table(box=box.SIMPLE_HEAVY, show_header=True,
                      header_style="bold magenta", title="命令一览",
                      title_style="bold", padding=(0, 1))
        table.add_column("命令", style="bold cyan", no_wrap=True)
        table.add_column("说明")
        for cmd, desc in _COMMANDS:
            table.add_row(cmd, desc)
        self.console.print(table)

        hints = Text()
        hints.append("输入提示：\n", style="bold")
        hints.append(r"· Enter 提交；结尾输入 \ 再回车续行（多行输入）" "\n", style="dim")
        hints.append("· Alt/Option+Enter 直接换行\n", style="dim")
        hints.append("· ↑/↓ 浏览输入历史；输入 / 弹出命令补全\n", style="dim")
        hints.append("· Ctrl+C：输入非空时取消当前输入，空输入时退出\n", style="dim")
        hints.append("· 生成中按 Ctrl+C 或 /interrupt：协作式中断当前轮\n", style="dim")
        hints.append("其余输入都作为消息发送给 Agent；长对话由内核自动压缩。",
                     style="dim")
        self.console.print(hints)

    def tools(self, state: "CliState") -> None:
        persona = state.persona
        cfg = state.config
        tools_desc = "、".join(persona.tools) if persona.tools else "全部工具（未收窄）"
        skills = scan_skills(cfg.project_root)
        mcp = load_mcp_servers(cfg.mcp_config_path) or {}
        table = Table(box=box.SIMPLE_HEAVY, show_header=False, padding=(0, 1),
                      expand=False)
        table.add_column(style="bold cyan", no_wrap=True)
        table.add_column()
        table.add_row("人设", f"{persona.name} — {persona.description or '(无描述)'}")
        table.add_row("工具", tools_desc)
        table.add_row("技能", "、".join(s["name"] for s in skills) if skills else "无")
        table.add_row("MCP 服务", "、".join(mcp.keys()) if mcp else "无")
        table.add_row("记忆工具", "search_experiences、read_core_memory、"
                                 "write_core_memory、write_task_context")
        self.console.print(Panel(table, title="工具与能力", border_style="blue",
                                 expand=False))

    def plan(self, plan: Any) -> None:
        table = Table(box=box.SIMPLE, show_header=True, header_style="bold",
                      padding=(0, 1), expand=False)
        table.add_column("状态", no_wrap=True)
        table.add_column("#", justify="right", style="dim")
        table.add_column("步骤")
        for s in plan.steps:
            if s.status == "done":
                mark = Text("✓ 完成", style="green")
            elif s.status == "in_progress":
                mark = Text("→ 进行中", style="yellow")
            elif s.status == "failed":
                mark = Text("✗ 失败", style="red")
            else:
                mark = Text("· 待做", style="dim")
            row = Text()
            row.append(s.action)
            if s.depends_on:
                row.append(f" (依赖 {', '.join(str(d) for d in s.depends_on)})",
                           style="dim")
            table.add_row(mark, str(s.id), row)
        self.console.print(Text.assemble(("目标：", "bold"), (plan.goal or "(未指定)", "")))
        self.console.print(table)
        if plan.risks:
            self.console.print(Text("风险：" + "；".join(plan.risks), style="dim"))

    def cost(self, turns: int, total: float) -> None:
        self.console.print(
            Text(f"本次会话累计：turns={turns} cost=${total:.4f}", style="dim"))

    def memory(self, text: str) -> None:
        if text:
            self.console.print(Markdown(text))
        else:
            self.console.print(Text("（核心记忆为空）", style="dim"))

    def _stats_text(self, info: dict, session: Any, lead_newline: bool) -> Any:
        turns = info.get("num_turns") or 0
        total = session.total_cost if session else 0.0
        dur = info.get("duration_ms")
        sid = info.get("session_id")
        t = Text()
        if lead_newline:
            t.append("\n")
        t.append("turns=", style="dim")
        t.append(f"{turns}", style="bold")
        t.append("  本轮 cost=", style="dim")
        t.append(_fmt_cost(info.get("cost_usd")), style="bold")
        t.append("  累计=", style="dim")
        t.append(f"${total:.4f}", style="bold")
        if dur:
            t.append("  用时=", style="dim")
            t.append(f"{dur}ms", style="bold")
        if sid:
            t.append("  会话=", style="dim")
            t.append(f"{sid}", style="bold")
        return t

    def turn_stats(self, info: dict, session: Any) -> None:
        self.console.print(self._stats_text(info, session, lead_newline=False))

    def oneshot_stats(self, info: dict, session: Any) -> None:
        self.console.print(self._stats_text(info, session, lead_newline=True))


def _rich_ui_enabled() -> bool:
    """是否启用富交互：富库可用 且 未设 NO_COLOR 且 stdin/stdout 均为 tty。"""
    if not _RICH_OK:
        return False
    if os.environ.get("NO_COLOR"):
        return False
    try:
        return bool(sys.stdin.isatty() and sys.stdout.isatty())
    except Exception:  # noqa: BLE001 - isatty 不可用时保守降级
        return False


def make_ui(interactive: bool = True) -> Any:
    """构造 UI：interactive 且满足富交互条件时用 RichUI，否则 PlainUI。

    headless（interactive=False）恒为 PlainUI（纯文本，便于 CI/管道）。
    """
    if interactive and _rich_ui_enabled():
        try:
            return RichUI(Console())
        except Exception:  # noqa: BLE001 - 富 UI 构造失败即降级
            return PlainUI()
    return PlainUI()


# ---- prompt_toolkit 输入会话（富交互） ----

def _build_prompt_session() -> Any:
    """构造 prompt_toolkit 输入会话：多行、历史、slash 补全、Ctrl+C 语义。

    - Enter 提交；结尾 \\ 再回车续行；Alt/Option+Enter 直接换行；
    - ↑/↓ 浏览历史（InMemoryHistory）；输入 / 弹出命令补全；
    - Ctrl+C：输入非空时清空（取消当前输入），空输入时抛 KeyboardInterrupt 退出。
    仅在 _RICH_OK 为真时调用。
    """
    kb = KeyBindings()

    @kb.add("enter")
    def _enter(event):  # noqa: ANN001 - prompt_toolkit 回调签名固定
        buff = event.current_buffer
        if buff.text.endswith("\\"):
            buff.delete_before_cursor(1)  # 去掉续行反斜杠
            buff.insert_text("\n")
        else:
            buff.validate_and_handle()    # 提交

    @kb.add("escape", "enter")
    def _alt_enter(event):  # Alt/Option+Enter 换行
        event.current_buffer.insert_text("\n")

    @kb.add("c-c")
    def _ctrl_c(event):
        buff = event.current_buffer
        if buff.text:
            buff.reset()  # 取消当前输入，停留在 REPL
        else:
            event.app.exit(exception=KeyboardInterrupt)  # 空输入时退出

    return PromptSession(
        message=HTML("<ansicyan><b>你&gt;</b></ansicyan> "),
        multiline=True,
        prompt_continuation="  · ",
        key_bindings=kb,
        completer=SlashCompleter(_COMMANDS),
        complete_style=CompleteStyle.MULTI_COLUMN,
        history=InMemoryHistory(),
    )


# ---- 流式消费（共用事件循环，按 UI 分叉显示） ----

async def stream_turn(state: CliState, prompt: str, ui: Any) -> dict:
    """发送一轮并经 ui 渲染：只消费 kernel 既有 AgentEvent 流。

    返回本轮 result 统计 dict（含 num_turns / cost_usd / duration_ms /
    session_id / is_error）。规划模式下累积文本用于解析结构化计划并持久化。
    Ctrl+C / 取消：协作式中断当前轮（session.interrupt），停在 turn 边界，
    与原 agent_cli.py 中断语义一致。
    """
    text_buf: list[str] = []
    result_info: dict = {}
    ui.turn_start()
    try:
        async for ev in state.session.send(prompt):
            if ev.type == "text_delta":
                chunk = ev.data or ""
                text_buf.append(chunk)
                ui.text(chunk)
            elif ev.type == "tool_use":
                data = ev.data if isinstance(ev.data, dict) else {"name": ev.data}
                ui.tool_use(data.get("name"), data.get("input"))
            elif ev.type == "tool_result":
                ui.tool_result(ev.data)
            elif ev.type == "result":
                result_info = ev.data if isinstance(ev.data, dict) else {}
            elif ev.type == "error":
                ui.error(ev.data)
    except (KeyboardInterrupt, asyncio.CancelledError):
        ui.interrupted()
        try:
            await state.session.interrupt()
        except Exception:  # noqa: BLE001 - 中断失败不阻断 REPL
            pass
        return result_info

    ui.turn_end()
    # 规划模式：尝试从输出解析结构化计划并落地进度文件
    if state.planning_mgr is not None:
        plan = parse_plan_from_output("".join(text_buf))
        if plan is not None:
            state.planning_mgr.save_plan(state.session_key, plan)
    return result_info


# ---- slash 命令处理（共用，按 UI 分叉显示） ----

async def handle_command(state: CliState, text: str, ui: Any) -> None:
    """处理 slash 命令。/exit /quit 置 stop；/new 重连会话。"""
    cmd = text.split()[0]
    cfg = state.config

    if cmd in ("/exit", "/quit"):
        state.stop = True
        return

    if cmd == "/help":
        ui.help()
        return

    if cmd == "/new":
        try:
            if state.session is not None:
                await state.session.close()
        except Exception:  # noqa: BLE001 - 关闭失败不阻断新会话
            pass
        await _open_session(state)
        ui.info("· 已开新会话（核心记忆保留，上下文清空）")
        return

    if cmd == "/cost":
        s = state.session
        ui.cost(s.total_turns if s else 0, s.total_cost if s else 0.0)
        return

    if cmd == "/tools":
        ui.tools(state)
        return

    if cmd == "/plan":
        if state.planning_mgr is None:
            ui.info("· 未启用规划模式（启动加 --planning）")
        else:
            plan = state.planning_mgr.load_plan(state.session_key)
            if plan is None:
                ui.info("· （无进行中的计划；先发一条任务让 Agent 产出计划）")
            else:
                ui.plan(plan)
        return

    if cmd == "/memory":
        ui.memory(load_core_memories_block(cfg.memory_core_dir))
        return

    if cmd == "/interrupt":
        try:
            if state.session is not None:
                await state.session.interrupt()
            ui.info("· 已请求中断当前轮（在 turn 边界停下）")
        except Exception as e:  # noqa: BLE001
            ui.info(f"· 中断失败：{e}")
        return

    ui.info(f"· 未知命令 {cmd}（未发送；/help 查看命令）")


# ---- REPL 主循环 ----

async def repl_async(state: CliState, resume: bool, ui: Any) -> None:
    """交互 REPL：单一事件循环内维持长连接会话，逐轮读取输入并流式响应。

    富交互时用 prompt_toolkit 读取（多行/历史/补全），否则回退到内置 input()。
    """
    resume_id = load_session(state.session_file) if resume else None
    await _open_session(state, resume_id=resume_id)
    if resume:
        ui.info(f"· 继续会话 {resume_id}" if resume_id
                else "· 没有可恢复的历史会话，从头开始")
    else:
        ui.banner(state)

    # 富交互且 prompt_toolkit 会话可用时用它读取输入；否则退回 input()
    ptk_session = None
    if getattr(ui, "rich", False):
        try:
            ptk_session = _build_prompt_session()
        except Exception:  # noqa: BLE001 - 输入会话构造失败退回 input()
            ptk_session = None

    while not state.stop:
        try:
            if ptk_session is not None:
                text = (await asyncio.to_thread(ptk_session.prompt)).strip()
            else:
                text = (await asyncio.to_thread(input, "\n你> ")).strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not text:
            continue
        if text.startswith("/"):
            await handle_command(state, text, ui)
            continue

        prompt = build_planning_prompt(text) if state.planning else text
        prompt = _rag_enrich(prompt, state.config)
        try:
            info = await stream_turn(state, prompt, ui)
        except Exception as e:  # noqa: BLE001 - 单轮异常不拖垮 REPL
            ui.fatal(e)
            continue

        save_session(state.session.session_id if state.session else None,
                     state.session_file)
        ui.turn_stats(info, state.session)

    # 退出前关闭会话
    try:
        if state.session is not None:
            await state.session.close()
    except Exception:  # noqa: BLE001
        pass


async def oneshot_async(state: CliState, prompt: str, resume: bool,
                        ui: Any) -> int:
    """headless 单发：连接会话，跑一轮，流式打印后关闭。返回进程退出码。

    ui 恒为 PlainUI（纯文本），便于 CI 与脚本管道消费。
    """
    resume_id = load_session(state.session_file) if resume else None
    await _open_session(state, resume_id=resume_id)
    full_prompt = build_planning_prompt(prompt) if state.planning else prompt
    full_prompt = _rag_enrich(full_prompt, state.config)
    exit_code = 0
    try:
        info = await stream_turn(state, full_prompt, ui)
        save_session(state.session.session_id if state.session else None,
                     state.session_file)
        if info.get("is_error"):
            exit_code = 1
        ui.oneshot_stats(info, state.session)
    finally:
        try:
            if state.session is not None:
                await state.session.close()
        except Exception:  # noqa: BLE001
            pass
    return exit_code


# ---- 入口 ----

def _env_bool(name: str, default: bool = False) -> bool:
    """解析布尔环境变量（truthy 词表判定）。"""
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def build_arg_parser() -> argparse.ArgumentParser:
    """构造命令行参数解析器。默认值融合环境变量。

    prog 固定为 "agent"，使 ``agent --help`` 与 ``python agent_cli.py --help``
    输出一致（同一命令的两个入口）。
    """
    parser = argparse.ArgumentParser(
        prog="agent",
        description="单机 Agent 交互 CLI（多轮会话，委托 agent_core 内核）",
    )
    parser.add_argument("prompt", nargs="?",
                        help="单发任务；省略则进入交互 REPL")
    parser.add_argument("--persona",
                        default=os.environ.get("AGENT_PERSONA", "executor"),
                        help="人设名（默认 executor；可选 orchestrator 等，"
                             "或 AGENT_PERSONA）")
    parser.add_argument("--model", default=os.environ.get("AGENT_MODEL"),
                        help="覆盖模型（默认 AGENT_MODEL 或 SDK 默认）")
    parser.add_argument("--max-turns", type=int,
                        default=int(os.environ.get("AGENT_MAX_TURNS", "50")),
                        help="单任务最大轮次（默认 AGENT_MAX_TURNS 或 50）")
    parser.add_argument("--permission-mode",
                        default=os.environ.get("AGENT_PERMISSION_MODE",
                                               "bypassPermissions"),
                        help="权限姿态（默认 bypassPermissions）")
    parser.add_argument("--planning", action="store_true",
                        default=_env_bool("AGENT_PLANNING"),
                        help="启用规划模式（先产出结构化计划再执行）")
    parser.add_argument("--resume", action="store_true",
                        help="续接上一次会话")
    parser.add_argument("--rag", action="store_true",
                        default=_env_bool("AGENT_RAG_ENABLED"),
                        help="启用 RAG 检索增强")
    return parser


def main(argv: list[str] | None = None) -> int:
    """CLI 入口：解析参数、装配配置与人设，用单一事件循环驱动会话。

    项目根与 MCP 配置按「env 优先 → cwd 向上探测 → 包根兜底」解析，不硬编码
    绝对路径；headless（带 prompt 参数）恒用纯文本 UI，交互 REPL 视 tty 与
    NO_COLOR 决定富/朴素 UI。
    """
    args = build_arg_parser().parse_args(argv)

    project_root = _discover_project_root()
    mcp_path = _resolve_mcp_config(project_root)
    # 装配配置：命令行参数优先级最高，覆盖环境变量结果
    config = load_agent_config(
        model=args.model,
        max_turns=args.max_turns,
        permission_mode=args.permission_mode,
        planning_enabled=args.planning,
        rag_enabled=args.rag,
        project_root=project_root,
        mcp_config_path=mcp_path,
    )
    persona = resolve_persona(args.persona, config)
    kernel = AgentKernel(config)
    cwd = config.project_root or project_root
    state = CliState(kernel, persona, config, cwd, planning=args.planning)

    try:
        if args.prompt:
            # headless 单发：纯文本，不启用任何 TUI/Live/spinner
            return asyncio.run(
                oneshot_async(state, args.prompt, args.resume,
                              make_ui(interactive=False)))
        asyncio.run(repl_async(state, args.resume, make_ui(interactive=True)))
        return 0
    except KeyboardInterrupt:
        print(dim("\n[已退出]"))
        return 0
    except Exception as e:  # noqa: BLE001 - 顶层兜底，给出可读错误
        print(f"[启动/执行失败] {e}", file=sys.stderr)
        print(dim("提示：真实会话需要可用的 claude-agent-sdk 与后端连接；"
                  "仅查看用法可加 --help。"), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
