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
from typing import Any, Awaitable, Callable

from agent_core.config import AgentConfig, load_agent_config
from agent_core.context import load_core_memories_block
from agent_core.kernel import AgentKernel
from agent_core.persona import Persona, resolve_persona
from agent_core.planning import (
    PlanningManager,
    build_planning_prompt,
    parse_plan_from_output,
)
from agent_core.tools import load_mcp_servers, scan_skills, valid_memory_name

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


# ---- 会话持久化（多槽目录制） ----
#
# 新格式：<项目根>/.agent_sessions/<session_id>.json，每会话一份元数据：
#   {session_id, persona, created_at, updated_at, first_message,
#    total_turns, total_cost}
# 兼容读取旧单文件 <root>/.agent_session（仅含 session_id）：目录无记录时
# 把旧文件内容合成为一条记录，供 /sessions、/switch、--resume 消费。


def _default_session_file() -> Path:
    """旧版单会话持久化文件（兼容保留）：<项目根>/.agent_session。"""
    return Path(_discover_project_root()) / ".agent_session"


def _sessions_dir(project_root: str | Path | None = None) -> Path:
    """多槽会话记录目录：<项目根>/.agent_sessions。"""
    root = project_root or _discover_project_root()
    return Path(root) / ".agent_sessions"


def save_session_record(record: dict, sessions_dir: str | Path | None = None) -> Path:
    """把单条会话元数据写入 .agent_sessions/<session_id>.json 并返回路径。

    session_id 缺失时忽略（尚未产生 SDK session_id 时没有可续接的意义）。
    """
    sid = record.get("session_id")
    if not sid:
        raise ValueError("session_id 缺失，无法持久化会话记录")
    d = Path(sessions_dir) if sessions_dir else _sessions_dir()
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{sid}.json"
    p.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    return p


def load_session_records(sessions_dir: str | Path | None = None,
                         project_root: str | Path | None = None) -> list[dict]:
    """列出全部会话记录，按 updated_at 降序（最近在前）。

    目录无记录时尝试兼容读取旧 .agent_session，合成为一条最小记录。
    损坏文件跳过；目录不存在且无旧文件返回空列表。
    """
    d = Path(sessions_dir) if sessions_dir else _sessions_dir(project_root)
    records: list[dict] = []
    if d.is_dir():
        for p in d.glob("*.json"):
            try:
                rec = json.loads(p.read_text(encoding="utf-8"))
                if isinstance(rec, dict) and rec.get("session_id"):
                    records.append(rec)
            except Exception:  # noqa: BLE001 - 损坏记录跳过不拖垮列表
                continue
    if not records:
        legacy_id = load_session(_default_session_file() if project_root is None
                                 else Path(project_root) / ".agent_session")
        if legacy_id:
            records.append({
                "session_id": legacy_id,
                "persona": "(未知)",
                "created_at": "",
                "updated_at": "",
                "first_message": "",
                "total_turns": 0,
                "total_cost": 0.0,
                "legacy": True,
            })
    records.sort(key=lambda r: r.get("updated_at") or "", reverse=True)
    return records


def load_last_session_id(project_root: str | Path | None = None) -> str | None:
    """取最近一次会话的 session_id（--resume 用）：多槽目录优先，旧文件兜底。"""
    records = load_session_records(project_root=project_root)
    if records:
        return records[0].get("session_id")
    return load_session()


def save_session(session_id: str | None, path: str | Path | None = None) -> None:
    """【兼容保留】把 session_id 写入旧版单文件；空值忽略。"""
    if not session_id:
        return
    p = Path(path) if path else _default_session_file()
    p.write_text(json.dumps({"session_id": session_id}), encoding="utf-8")


def load_session(path: str | Path | None = None) -> str | None:
    """读取旧版单文件持久化的 session_id；不存在/损坏返回 None。"""
    try:
        p = Path(path) if path else _default_session_file()
        return json.loads(p.read_text(encoding="utf-8")).get("session_id")
    except Exception:  # noqa: BLE001 - 无历史会话是正常情形
        return None


# ---- 人设解析（resolve_persona 统一实现见 agent_core.persona） ----


# ---- slash 命令清单（补全与 /help 共用） ----

# (命令, 说明)；/quit 也接受但不在列表里显示（等价 /exit）
_COMMANDS: list[tuple[str, str]] = [
    ("/help", "查看本帮助"),
    ("/new", "开新会话（清空上下文，重连客户端）"),
    ("/sessions", "列出历史会话（.agent_sessions/）"),
    ("/switch <序号>", "切换到指定历史会话（关闭当前、resume 选中）"),
    ("/session", "显示当前会话元数据"),
    ("/cost", "显示本次累计 turns 与 cost"),
    ("/usage", "把本会话逐轮用量追加写 .agent_usage/<session_id>.jsonl"),
    ("/tools", "列出人设工具、技能、MCP 服务与记忆工具"),
    ("/plan", "显示当前规划进度（--planning 模式下有意义）"),
    ("/memory", "查看核心记忆；/memory <名> 查看；/memory set <名> 写入"),
    ("/retry", "重发上一条失败的消息"),
    ("/interrupt", "中断当前正在执行的一轮"),
    ("/exit", "退出"),
]

_HELP_TEXT = """命令：
  /help        查看本帮助
  /new         开新会话（清空上下文，重连客户端）
  /sessions    列出历史会话（多槽持久化于 .agent_sessions/）
  /switch <序号>  切换到指定历史会话（关闭当前、resume 选中）
  /session     显示当前会话元数据（id/人设/累计用量/首条消息）
  /cost        显示本次累计 turns 与 cost
  /usage       把本会话逐轮用量 {时间,turns,cost,tokens} 追加写
               .agent_usage/<session_id>.jsonl
  /tools       列出人设工具、技能、MCP 服务与记忆工具
  /plan        显示当前规划进度（--planning 模式下有意义）
  /memory          查看全部核心记忆
  /memory <名>     按名查看一条核心记忆
  /memory set <名> 交互式写入一条核心记忆（空行结束）
  /retry       重发上一条失败的消息
  /interrupt   中断当前正在执行的一轮
  /exit        退出
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
        # 会话多槽：当前会话元数据（personas/首条消息/创建时间），随会话切换重置
        self.session_meta: dict = self._new_session_meta()
        # /retry：缓存最近一次发送的用户消息，fatal 后可重发
        self.last_prompt: str | None = None
        # /usage：本会话逐轮用量记录 + 已写盘条数（避免重复追加）
        self.turn_records: list[dict] = []
        self.usage_flushed: int = 0

    def _new_session_meta(self) -> dict:
        return {
            "persona": self.persona.name if self.persona else "",
            "first_message": "",
            "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }

    @property
    def session_key(self) -> str:
        """规划进度文件的键：优先真实 session_id，否则用 default。"""
        sid = self.session.session_id if self.session else None
        return sid or "default"

    def note_first_message(self, text: str) -> None:
        """记录本会话首条用户消息摘要（会话元数据用，幂等）。"""
        if self.session_meta.get("first_message") is None or \
                not self.session_meta.get("first_message"):
            self.session_meta["first_message"] = " ".join(text.split())[:80]

    def record_turn(self, info: dict) -> None:
        """把本轮用量追加进 turn_records（/usage 落盘数据源）。"""
        usage = info.get("usage") if isinstance(info.get("usage"), dict) else {}
        self.turn_records.append({
            "time": time.strftime("%Y-%m-%d %H:%M:%S"),
            "turns": info.get("num_turns") or 0,
            "cost": info.get("cost_usd"),
            "input_tokens": (usage or {}).get("input_tokens") or 0,
            "output_tokens": (usage or {}).get("output_tokens") or 0,
        })

    def save_current_session_record(self) -> Path | None:
        """把当前会话元数据写入 .agent_sessions/；无 session_id 时静默跳过。"""
        sid = self.session.session_id if self.session else None
        if not sid:
            return None
        rec = {
            "session_id": sid,
            "persona": self.session_meta.get("persona") or self.persona.name,
            "created_at": self.session_meta.get("created_at", ""),
            "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "first_message": self.session_meta.get("first_message", ""),
            "total_turns": getattr(self.session, "total_turns", 0) or 0,
            "total_cost": round(getattr(self.session, "total_cost", 0.0) or 0.0, 6),
        }
        try:
            return save_session_record(rec, _sessions_dir(self.config.project_root))
        except Exception:  # noqa: BLE001 - 持久化失败不拖垮会话
            return None


async def _open_session(state: CliState, resume_id: str | None = None,
                        ui: Any = None) -> None:
    """创建并连接一个新的 AgentSession；resume_id 非空时续接历史会话。

    resume 失败（SDK 异常，如历史会话文件已损坏/不存在）时降级为开新会话，
    并通过 ui（可选）明确告知「历史不可恢复」。
    """
    session = state.kernel.open_session(state.persona, state.cwd)
    if resume_id:
        # AgentSession.connect() 会以 _session_id 作为 resume 目标续接
        session._session_id = resume_id  # noqa: SLF001 - CLI 复用内核续接语义
    try:
        await session.connect()
    except Exception as e:  # noqa: BLE001 - resume 失败降级开新会话
        if resume_id:
            fallback = state.kernel.open_session(state.persona, state.cwd)
            await fallback.connect()
            state.session = fallback
            state.session_meta = state._new_session_meta()
            msg = (f"· 续接会话 {resume_id} 失败（{e}）；已开新会话，"
                   "历史上下文不可恢复")
            if ui is not None:
                ui.info(msg)
            else:
                print(dim(msg))
            return
        raise
    state.session = session
    state.session_meta = state._new_session_meta()


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


def _extract_tokens(usage: Any) -> tuple[int, int]:
    """从 result 事件的 usage dict 提取 (input_tokens, output_tokens)。"""
    if not isinstance(usage, dict):
        return 0, 0
    try:
        inp = int(usage.get("input_tokens") or 0)
    except (TypeError, ValueError):
        inp = 0
    try:
        out = int(usage.get("output_tokens") or 0)
    except (TypeError, ValueError):
        out = 0
    return inp, out


def _tokens_text(info: dict) -> str:
    """渲染本轮 tokens 片段（无 usage 数据时返回空串）。"""
    inp, out = _extract_tokens(info.get("usage"))
    if inp or out:
        return f"  tokens={inp}/{out}"
    return ""


def _budget_warning_text(data: Any) -> str:
    """渲染预算软预警行。"""
    if isinstance(data, dict):
        thr = data.get("threshold")
        spent = data.get("spent_usd")
        budget = data.get("budget_usd")
        try:
            return (f"⚠ 预算软预警：累计成本 ${float(spent):.4f} 已达预算 "
                    f"${float(budget):.2f} 的 {int(float(thr) * 100)}%")
        except (TypeError, ValueError):
            pass
    return f"⚠ 预算软预警：{data}"


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

    def budget_warning(self, data: Any) -> None:
        print(dim(_budget_warning_text(data)))

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
        print(dim(f"[turns={turns} 累计cost=${cost:.4f}{_tokens_text(info)}]"))

    def oneshot_stats(self, info: dict, session: Any) -> None:
        turns = info.get("num_turns") or 0
        cost = session.total_cost if session else 0.0
        print(dim(f"\n[turns={turns} cost=${cost:.4f}{_tokens_text(info)}]"))


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

    def budget_warning(self, data: Any) -> None:
        self._stop_live()
        self.console.print(Text(_budget_warning_text(data), style="yellow"))

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
        inp, out = _extract_tokens(info.get("usage"))
        t = Text()
        if lead_newline:
            t.append("\n")
        t.append("turns=", style="dim")
        t.append(f"{turns}", style="bold")
        if inp or out:
            t.append("  tokens(in/out)=", style="dim")
            t.append(f"{inp}/{out}", style="bold")
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

    墙钟截止（P0）：此前 send 无 timeout，卡住的后端会让整轮无限等待；现取
    config.task_timeout 作为单轮上限（非正数视为不限），超时由内核转 error
    事件收场，headless 退出码据此判失败（见 oneshot_async）。
    """
    text_buf: list[str] = []
    result_info: dict = {}
    saw_error = False   # 是否出现 error 事件（流内报错）
    saw_result = False  # 是否收到终结 result 事件（缺失=流异常提前结束）
    turn_timeout = getattr(state.config, "task_timeout", None)
    if not (isinstance(turn_timeout, (int, float)) and turn_timeout > 0):
        turn_timeout = None
    ui.turn_start()
    try:
        async for ev in state.session.send(prompt, timeout=turn_timeout):
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
                saw_result = True
                result_info = ev.data if isinstance(ev.data, dict) else {}
            elif ev.type == "budget_warning":
                ui.budget_warning(ev.data)
            elif ev.type == "error":
                saw_error = True
                ui.error(ev.data)
    except (KeyboardInterrupt, asyncio.CancelledError):
        ui.interrupted()
        try:
            await state.session.interrupt()
        except Exception:  # noqa: BLE001 - 中断失败不阻断 REPL
            pass
        result_info = dict(result_info)
        result_info["_saw_error"] = True   # 中断=未正常完成
        result_info["_saw_result"] = saw_result
        return result_info

    ui.turn_end()
    # 规划模式：尝试从输出解析结构化计划并落地进度文件
    if state.planning_mgr is not None:
        plan = parse_plan_from_output("".join(text_buf))
        if plan is not None:
            state.planning_mgr.save_plan(state.session_key, plan)
    # 真相口径（P0）：把流内 error / 缺失终结 result 的信号透传给调用方，
    # 供 headless 退出码判定，避免「只出 error 事件、无 result」静默退出 0。
    result_info = dict(result_info)
    result_info["_saw_error"] = saw_error
    result_info["_saw_result"] = saw_result
    return result_info


# ---- slash 命令处理（共用，按 UI 分叉显示） ----

async def _send_user_message(state: CliState, text: str, ui: Any) -> None:
    """发送一条用户消息的完整链路（REPL 主循环与 /retry 共用）。

    规划提示包装保留在 CLI；RAG 经验拼装已在内核侧统一（kernel.rag_enrich），
    CLI 不再自行拼接。发送前缓存 last_prompt 供 /retry 重发。
    """
    state.last_prompt = text
    state.note_first_message(text)
    prompt = build_planning_prompt(text) if state.planning else text
    try:
        info = await stream_turn(state, prompt, ui)
    except Exception as e:  # noqa: BLE001 - 单轮异常不拖垮 REPL
        ui.fatal(e)
        ui.info("· 可输入 /retry 重发上一条消息")
        return
    state.record_turn(info)
    state.save_current_session_record()
    ui.turn_stats(info, state.session)


def _render_session_records(records: list[dict]) -> str:
    """渲染 /sessions 列表（朴素文本，共用）。"""
    if not records:
        return "（暂无历史会话）"
    lines = ["历史会话（最近在前，/switch <序号> 切换）："]
    for i, rec in enumerate(records, 1):
        sid = str(rec.get("session_id") or "")[:16]
        persona = rec.get("persona") or "(未知)"
        updated = rec.get("updated_at") or "—"
        first = rec.get("first_message") or "—"
        cost = rec.get("total_cost")
        cost_s = f"${float(cost):.4f}" if isinstance(cost, (int, float)) else "—"
        legacy = " [旧格式]" if rec.get("legacy") else ""
        lines.append(f"  {i}. {sid}  {persona}  {updated}  cost={cost_s}{legacy}")
        lines.append(f"     首条消息：{first}")
    return "\n".join(lines)


def _usage_file_path(state: CliState) -> Path | None:
    """本会话用量 JSONL 落盘路径：.agent_usage/<session_id>.jsonl。"""
    sid = state.session.session_id if state.session else None
    if not sid:
        return None
    root = Path(state.config.project_root or _discover_project_root())
    return root / ".agent_usage" / f"{sid}.jsonl"




async def _interactive_write_memory(cfg: AgentConfig, name: str,
                                    input_fn=None) -> str | None:
    """交互式写入核心记忆 config.memory_core_dir/<name>.md。

    逐行读取直到空行；返回写入路径，取消/无内容返回 None。
    input_fn 可注入（测试用）；默认阻塞读 stdin，放线程避免卡事件循环。
    """
    if input_fn is None:
        input_fn = input
    print(dim(f"输入记忆 '{name}' 的内容（逐行输入，空行结束）："))
    lines: list[str] = []
    while True:
        line = await asyncio.to_thread(input_fn)
        if line is None or line.strip() == "":
            break
        lines.append(line)
    content = "\n".join(lines).strip()
    if not content:
        return None
    d = Path(cfg.memory_core_dir)
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{name}.md"
    p.write_text(content, encoding="utf-8")
    return str(p)


async def handle_command(state: CliState, text: str, ui: Any) -> None:
    """处理 slash 命令：按命令名查分发表，分发到各自 handler。

    /exit /quit 置 stop；/new 重连会话。未命中命令提示未知命令不发送。
    """
    cmd = text.split()[0]
    handler = _COMMAND_HANDLERS.get(cmd)
    if handler is None:
        ui.info(f"· 未知命令 {cmd}（未发送；/help 查看命令）")
        return
    await handler(state, text, ui)


def _memory_name_error_msg() -> str:
    """记忆名校验失败提示（/memory set 与 /memory 读共用，保持提示一致）。"""
    return ("· 记忆名只能含字母/数字/下划线/连字符，不含路径分隔符，"
            "且不得为 Windows 保留设备名（con/nul 等）")


async def _close_current_session(state: CliState) -> None:
    """关闭当前会话（/new 与 /switch 换会话前共用；关闭失败不阻断）。"""
    if state.session is not None:
        try:
            await state.session.close()
        except Exception:  # noqa: BLE001 - 关闭失败不阻断新会话
            pass


async def _cmd_exit(state: CliState, text: str, ui: Any) -> None:
    state.stop = True


async def _cmd_help(state: CliState, text: str, ui: Any) -> None:
    ui.help()


async def _cmd_new(state: CliState, text: str, ui: Any) -> None:
    await _close_current_session(state)
    await _open_session(state, ui=ui)
    ui.info("· 已开新会话（核心记忆保留，上下文清空）")


async def _cmd_sessions(state: CliState, text: str, ui: Any) -> None:
    records = load_session_records(
        sessions_dir=_sessions_dir(state.config.project_root))
    ui.info(_render_session_records(records))


async def _cmd_switch(state: CliState, text: str, ui: Any) -> None:
    cfg = state.config
    parts = text.split()
    records = load_session_records(sessions_dir=_sessions_dir(cfg.project_root))
    if len(parts) < 2 or not parts[1].isdigit():
        ui.info("· 用法：/switch <序号>（先 /sessions 查看列表）")
        return
    idx = int(parts[1])
    if not (1 <= idx <= len(records)):
        ui.info(f"· 序号超出范围（1-{len(records)}）")
        return
    target = records[idx - 1]
    await _close_current_session(state)
    await _open_session(state, resume_id=target.get("session_id"), ui=ui)
    # 元数据继承自目标记录；resume 失败降级时 _open_session 已重置
    if state.session is not None and \
            state.session.session_id == target.get("session_id"):
        state.session_meta = {
            "persona": target.get("persona") or state.persona.name,
            "first_message": target.get("first_message") or "",
            "created_at": target.get("created_at")
            or time.strftime("%Y-%m-%d %H:%M:%S"),
        }
    ui.info(f"· 已切换到会话 {target.get('session_id')}"
            f"（人设：{target.get('persona') or '(未知)'}）")


async def _cmd_session(state: CliState, text: str, ui: Any) -> None:
    s = state.session
    sid = s.session_id if s else None
    meta = state.session_meta
    lines = ["当前会话：",
             f"  session_id：{sid or '（尚未产生）'}",
             f"  人设：{meta.get('persona') or state.persona.name}",
             f"  工作目录：{state.cwd}",
             f"  创建时间：{meta.get('created_at') or '—'}",
             f"  首条消息：{meta.get('first_message') or '—'}",
             f"  累计 turns：{getattr(s, 'total_turns', 0) if s else 0}",
             f"  累计 cost：${getattr(s, 'total_cost', 0.0) or 0.0:.4f}"]
    if sid:
        lines.append(f"  记录文件：.agent_sessions/{sid}.json")
    ui.info("\n".join(lines))


async def _cmd_usage(state: CliState, text: str, ui: Any) -> None:
    p = _usage_file_path(state)
    if p is None:
        ui.info("· 尚未产生 session_id（先发一条消息）")
        return
    pending = state.turn_records[state.usage_flushed:]
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a", encoding="utf-8") as f:
            for rec in pending:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        state.usage_flushed = len(state.turn_records)
        ui.info(f"· 已追加写入 {len(pending)} 条用量记录 → {p}")
    except Exception as e:  # noqa: BLE001 - 写盘失败只提示不崩溃
        ui.info(f"· 写入失败：{e}")


async def _cmd_cost(state: CliState, text: str, ui: Any) -> None:
    s = state.session
    ui.cost(s.total_turns if s else 0, s.total_cost if s else 0.0)


async def _cmd_tools(state: CliState, text: str, ui: Any) -> None:
    ui.tools(state)


async def _cmd_plan(state: CliState, text: str, ui: Any) -> None:
    if state.planning_mgr is None:
        ui.info("· 未启用规划模式（启动加 --planning）")
    else:
        plan = state.planning_mgr.load_plan(state.session_key)
        if plan is None:
            ui.info("· （无进行中的计划；先发一条任务让 Agent 产出计划）")
        else:
            ui.plan(plan)


async def _cmd_memory(state: CliState, text: str, ui: Any) -> None:
    cfg = state.config
    parts = text.split()
    if len(parts) == 1:
        ui.memory(load_core_memories_block(cfg.memory_core_dir))
        return
    if parts[1] == "set":
        if len(parts) < 3:
            ui.info("· 用法：/memory set <名称>（随后逐行输入内容，空行结束）")
            return
        name = parts[2]
        if not valid_memory_name(name):
            ui.info(_memory_name_error_msg())
            return
        path = await _interactive_write_memory(cfg, name)
        if path:
            ui.info(f"· 已写入核心记忆 '{name}' → {path}")
        else:
            ui.info("· 未输入任何内容，取消写入")
        return
    name = parts[1]
    if not valid_memory_name(name):
        ui.info(_memory_name_error_msg())
        return
    p = Path(cfg.memory_core_dir) / f"{name}.md"
    if p.is_file():
        try:
            ui.memory(p.read_text(encoding="utf-8"))
        except Exception as e:  # noqa: BLE001
            ui.info(f"· 读取失败：{e}")
    else:
        available = sorted(
            q.stem for q in Path(cfg.memory_core_dir).glob("*.md")
        ) if Path(cfg.memory_core_dir).is_dir() else []
        ui.info(f"· 无此记忆 '{name}'（{p}）；"
                f"可用：{', '.join(available) or '无'}")


async def _cmd_retry(state: CliState, text: str, ui: Any) -> None:
    if not state.last_prompt:
        ui.info("· 没有可重发的消息（/retry 用于重发上一条失败消息）")
        return
    ui.info(f"· 重发上一条消息：{state.last_prompt[:50]}")
    await _send_user_message(state, state.last_prompt, ui)


async def _cmd_interrupt(state: CliState, text: str, ui: Any) -> None:
    try:
        if state.session is not None:
            await state.session.interrupt()
        ui.info("· 已请求中断当前轮（在 turn 边界停下）")
    except Exception as e:  # noqa: BLE001
        ui.info(f"· 中断失败：{e}")


# slash 命令分发表；命令清单（help/补全展示用）见上方 _COMMANDS
_COMMAND_HANDLERS: dict[str, Callable[[CliState, str, Any], Awaitable[None]]] = {
    "/exit": _cmd_exit,
    "/quit": _cmd_exit,
    "/help": _cmd_help,
    "/new": _cmd_new,
    "/sessions": _cmd_sessions,
    "/switch": _cmd_switch,
    "/session": _cmd_session,
    "/usage": _cmd_usage,
    "/cost": _cmd_cost,
    "/tools": _cmd_tools,
    "/plan": _cmd_plan,
    "/memory": _cmd_memory,
    "/retry": _cmd_retry,
    "/interrupt": _cmd_interrupt,
}


# ---- REPL 主循环 ----

async def repl_async(state: CliState, resume: bool, ui: Any) -> None:
    """交互 REPL：单一事件循环内维持长连接会话，逐轮读取输入并流式响应。

    富交互时用 prompt_toolkit 读取（多行/历史/补全），否则回退到内置 input()。
    """
    resume_id = load_last_session_id(state.config.project_root) if resume else None
    await _open_session(state, resume_id=resume_id, ui=ui)
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

        await _send_user_message(state, text, ui)

    # 退出前持久化会话记录并关闭会话
    state.save_current_session_record()
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
    resume_id = load_last_session_id(state.config.project_root) if resume else None
    await _open_session(state, resume_id=resume_id, ui=ui)
    full_prompt = build_planning_prompt(prompt) if state.planning else prompt
    exit_code = 0
    try:
        info = await stream_turn(state, full_prompt, ui)
        state.note_first_message(prompt)
        state.record_turn(info)
        state.save_current_session_record()
        # 旧版单文件同步写一份，保持 --resume 对旧工具链的兼容
        save_session(state.session.session_id if state.session else None,
                     state.session_file)
        if info.get("is_error"):
            exit_code = 1
        # 真相口径（P0）：流内出现 error 事件、或整轮未收到终结 result
        # 事件（SDK 子进程崩溃/被杀/协议中断），均视为失败，不得退出 0。
        if info.get("_saw_error") or not info.get("_saw_result"):
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
                                               "default"),
                        help="权限姿态（默认 default；bypassPermissions 须显式指定）")
    parser.add_argument("--planning", action="store_true",
                        default=_env_bool("AGENT_PLANNING_ENABLED"),
                        help="启用规划模式（先产出结构化计划再执行；"
                             "默认读 AGENT_PLANNING_ENABLED）")
    parser.add_argument("--resume", action="store_true",
                        help="续接上一次会话")
    parser.add_argument("--rag", action="store_true",
                        default=_env_bool("AGENT_RAG_ENABLED"),
                        help="启用 RAG 检索增强")
    parser.add_argument("--doctor", action="store_true",
                        help="运行环境自检（SDK/鉴权/MCP/人设/.env）后退出")
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
    persona = resolve_persona(
        args.persona, config,
        warn=lambda msg: print(dim(msg), file=sys.stderr))
    kernel = AgentKernel(config)
    cwd = config.project_root or project_root
    state = CliState(kernel, persona, config, cwd, planning=args.planning)

    # doctor 自检：装配 config 后、进入 REPL/单发前执行，打印报告即退出
    if args.doctor:
        from agent_core.doctor import run_checks, format_report
        checks = run_checks(config, default_persona=args.persona)
        print(format_report(checks))
        return 1 if any(c.status == "fail" for c in checks) else 0

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
