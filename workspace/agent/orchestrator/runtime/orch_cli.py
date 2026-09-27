"""单机交互模式：让编排框架脱离 Claude Code/ZCode 主会话，成为像它们一样的独立工具。

定位：orch CLI 本身就是 V1 的「大脑」——加载 orchestrator 工作流作为系统提示，
经 Agent 工具派发 .claude/agents/ 下的 executor / security-guard 子 agent，
核心记忆自动加载（与大脑 SKILL.md 约定一致）。不需要 brain_api / worker，
一台机器开箱即用；也可配 ORCH_MCP_CONFIG 给整个会话挂 MCP 工具。

交互范式借鉴自成熟 CLI agent（Claude Code/Aider/Gemini CLI 等）：
TUI + headless 双模式、可恢复持久会话（--resume）、/cost 用量显示、
slash 命令注册表、状态行、分级权限姿态。

用法：
  python orch_cli.py                    # 交互 REPL
  python orch_cli.py "任务"              # 单发（headless，脚本/CI 可用）
  python orch_cli.py --resume           # 继续上一次会话
环境变量：ORCH_PERMISSION_MODE / ORCH_MODEL / ORCH_MAX_TURNS / ORCH_MCP_CONFIG
"""
import argparse
import json
import os
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
# runtime → orchestrator → workspace/agent → 项目根
_PROJECT_ROOT = _HERE.parents[3]
sys.path.insert(0, str(_HERE))
# 统一委托 agent_core 内核（不再借道 worker/agent_runner）；确保项目根可导入
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from agent_core.context import load_core_memories_block as _load_core_memories_block  # noqa: E402
from agent_core.persona import parse_subagents as _parse_subagents  # noqa: E402
from agent_core.persona import strip_frontmatter as _strip_frontmatter  # noqa: E402
from agent_core.tools import load_mcp_servers, scan_skills  # noqa: E402

_SESSION_FILE = _HERE / ".orch_session"


# 以下三个函数保持向后兼容的公共接口（测试直接调用 orch_cli.xxx），
# 逻辑委托给 agent_core 内核，输出格式与原实现一致。
def strip_frontmatter(text: str) -> str:
    """去掉 SKILL.md 的 YAML frontmatter，返回正文（委托 agent_core.persona）。"""
    return _strip_frontmatter(text)


def load_core_memories_block(core_dir: str | Path) -> str:
    """核心记忆自动加载块（委托 agent_core.context，输出格式与原实现一致）。"""
    return _load_core_memories_block(core_dir)


def build_brain_env_block(project_root: Path) -> str:
    """运行环境块：项目根 + 可用技能清单（大脑路由用，派发 prompt 打 [技能名] 标记）。

    刻意不委托 agent_core.context.build_env_block：后者标题为「运行环境（内核注入…）」，
    而 orch CLI 约定标题为「运行环境（orch CLI 注入）」（测试断言依赖此精确文本）。
    技能扫描复用内核 scan_skills。
    """
    lines = [
        "\n---",
        "## 运行环境（orch CLI 注入）",
        f"- 项目根（你的工作区）：{project_root.resolve()}",
        "- 子 agent 已注册：executor（执行）、security-guard（只读检验），用 Agent 工具派发",
        "- 可用技能（在派发 prompt 里写 [技能名] 标记即路由 executor 按其流程执行）：",
    ]
    skills = scan_skills(str(project_root))
    if skills:
        for s in skills:
            trigger_hint = f"（触发词：{'、'.join(s['triggers'])}）" if s["triggers"] else ""
            lines.append(f"  - {s['name']}{trigger_hint}")
    else:
        lines.append("  - 无")
    return "\n".join(lines)


def parse_subagents(agents_dir: str | Path) -> dict:
    """解析 .claude/agents/*.md 为 SDK options.agents 形状（委托 agent_core.persona）。

    name 作字典键；tools 逗号串转列表；model 透传（如 sonnet/haiku）。
    """
    return _parse_subagents(agents_dir)


def build_system_prompt(project_root: Path) -> str:
    """大脑系统提示 = orchestrator 工作流 + 核心记忆 + 运行环境。"""
    skill_md = project_root / "workspace" / "agent" / "skills" / "orchestrator" / "SKILL.md"
    parts = ["你是 orchestrator，多 agent 编排框架的大脑。"]
    if skill_md.is_file():
        parts.append(strip_frontmatter(skill_md.read_text(encoding="utf-8")))
    core = load_core_memories_block(project_root / "workspace" / "agent" / "skills"
                                    / "orchestrator" / "memory" / "core")
    if core:
        parts.append(core)
    parts.append(build_brain_env_block(project_root))
    return "\n\n".join(parts)


def _subagents_enabled_default() -> bool:
    """子代理派发全局默认（env ORCH_/AGENT_SUBAGENTS_ENABLED，缺省 True=行为不变）。"""
    for name in ("ORCH_SUBAGENTS_ENABLED", "AGENT_SUBAGENTS_ENABLED"):
        raw = os.environ.get(name)
        if raw is not None and raw.strip() != "":
            return raw.strip().lower() in ("1", "true", "yes", "on")
    return True


def _auto_apply_default() -> bool:
    """顾问 auto-apply 全局默认（env ORCH_/AGENT_ADVISOR_AUTO_APPLY，缺省 False=仅建议）。"""
    for name in ("ORCH_ADVISOR_AUTO_APPLY", "AGENT_ADVISOR_AUTO_APPLY"):
        raw = os.environ.get(name)
        if raw is not None and raw.strip() != "":
            return raw.strip().lower() in ("1", "true", "yes", "on")
    return False


def build_options(project_root: Path, model: str | None, max_turns: int,
                  permission_mode: str, resume_session_id: str | None = None,
                  enable_subagents: bool | None = None):
    """构造 SDK options：大脑提示 + 子 agent（可开关）+ MCP（可选）+ 会话续连。

    enable_subagents：本次运行覆盖子代理派发开关；None=跟随全局默认
    （ORCH_/AGENT_SUBAGENTS_ENABLED）。关闭时不挂 agents → 大脑不会派生 SDK 子代理。
    """
    import claude_agent_sdk as sdk

    option_kwargs: dict = {
        "cwd": str(project_root),
        "system_prompt": build_system_prompt(project_root),
        "max_turns": max_turns,
        "permission_mode": permission_mode,
    }
    effective_subagents = (enable_subagents if enable_subagents is not None
                           else _subagents_enabled_default())
    if effective_subagents:
        agents = parse_subagents(project_root / ".claude" / "agents")
        if agents:
            option_kwargs["agents"] = agents
    if model:
        option_kwargs["model"] = model
    mcp_servers = load_mcp_servers(os.environ.get("ORCH_MCP_CONFIG", ""))
    if mcp_servers:
        option_kwargs["mcp_servers"] = mcp_servers
    if resume_session_id:
        option_kwargs["resume"] = resume_session_id
    return sdk.ClaudeAgentOptions(**option_kwargs)


# ---- 会话持久化（borrowed: Claude Code 可恢复会话） ----

def save_session(session_id: str | None, path: str | Path | None = None) -> None:
    if not session_id:
        return
    Path(path or _SESSION_FILE).write_text(json.dumps({"session_id": session_id}),
                                           encoding="utf-8")


def load_session(path: str | Path | None = None) -> str | None:
    try:
        return json.loads(Path(path or _SESSION_FILE).read_text(encoding="utf-8")) \
            .get("session_id")
    except Exception:
        return None


# ---- 输出辅助（borrowed: NO_COLOR/tty 友好的弱化样式） ----

def _dim_enabled() -> bool:
    return sys.stderr.isatty() and not os.environ.get("NO_COLOR")


def dim(text: str) -> str:
    return f"\033[2m{text}\033[0m" if _dim_enabled() else text


_ADVISOR = None


def _get_advisor():
    """惰性构造 CLI 用的独立上下文顾问（AdvisorService，绝不污染任务上下文）。"""
    global _ADVISOR
    if _ADVISOR is None:
        from agent_core.advisor import AdvisorService
        from agent_core.config import load_agent_config
        cfg = load_agent_config(project_root=str(_PROJECT_ROOT))
        _ADVISOR = AdvisorService(
            cfg, persona_dir=str(_PROJECT_ROOT / ".claude" / "agents"))
    return _ADVISOR


def _print_advice(result) -> None:
    """把 AdvisorResult 渲染为 CLI 文本（弱化样式）。"""
    should = bool(getattr(result, "should_dispatch", False))
    verdict = "建议派发子代理" if should else "不建议派发子代理（单 agent 即可完成）"
    print(f"\n推荐：{verdict}")
    roles = getattr(result, "roles", None) or []
    if roles:
        print(dim(f"  角色：{', '.join(str(r) for r in roles)}"))
    plan = getattr(result, "plan", None) or []
    if plan:
        print(dim("  拆解计划："))
        for step in plan:
            if isinstance(step, dict):
                sid = step.get("id", "?")
                action = step.get("action", "")
                deps = step.get("depends_on", []) or []
                dep = f"（依赖 {deps}）" if deps else ""
                print(dim(f"    {sid}. {action}{dep}"))
            else:
                print(dim(f"    - {step}"))
    explanation = getattr(result, "explanation", "") or ""
    if explanation:
        print(f"  解释：{explanation}")
    risks = getattr(result, "risks", None) or []
    if risks:
        print(dim("  风险："))
        for r in risks:
            print(dim(f"    - {r}"))
    print(dim("  （顾问运行于独立上下文，未触碰本任务会话/记忆/经验）"))


_TRANSLATOR = None


def _get_translator():
    """惰性构造 CLI 用的独立上下文翻译服务（TranslatorService，绝不污染任务上下文）。"""
    global _TRANSLATOR
    if _TRANSLATOR is None:
        from agent_core.config import load_agent_config
        from agent_core.translator import TranslatorService
        cfg = load_agent_config(project_root=str(_PROJECT_ROOT))
        _TRANSLATOR = TranslatorService(
            cfg, persona_dir=str(_PROJECT_ROOT / ".claude" / "agents"))
    return _TRANSLATOR


def _print_translation(result) -> None:
    """把 TranslationResult 渲染为 CLI 文本（弱化样式）。"""
    if getattr(result, "error", ""):
        print(f"[翻译失败] {result.error}")
        return
    skipped = getattr(result, "skipped", "")
    if skipped == "already_target_lang":
        print(dim(f"· 文本已是目标语言（{getattr(result, 'target_lang', 'zh')}），无需翻译"))
        return
    if skipped == "empty":
        print(dim("· 待翻译文本为空"))
        return
    print(f"\n译文（{getattr(result, 'target_lang', 'zh')}）：")
    print(getattr(result, "translated", "") or "")
    print(dim("  （翻译运行于独立上下文，未触碰本任务会话/记忆/经验）"))


def run_turn(prompt: str, options) -> dict:
    """跑一轮（同步阻塞），流式打印；返回 {session_id, num_turns, cost_usd, is_error}。"""
    import asyncio

    import claude_agent_sdk as sdk

    async def _run() -> dict:
        info: dict = {"session_id": None, "num_turns": 0,
                      "cost_usd": None, "is_error": False}
        async for msg in sdk.query(prompt=prompt, options=options):
            if type(msg).__name__ == "AssistantMessage":
                for block in getattr(msg, "content", []) or []:
                    kind = type(block).__name__
                    if kind == "TextBlock":
                        print(block.text, end="", flush=True)
                    elif kind == "ToolUseBlock":
                        print(dim(f"\n  · ⚙ {block.name}"), flush=True)
            elif type(msg).__name__ == "ResultMessage":
                info["session_id"] = getattr(msg, "session_id", None)
                info["num_turns"] = getattr(msg, "num_turns", 0) or 0
                info["cost_usd"] = getattr(msg, "total_cost_usd", None)
                info["is_error"] = bool(getattr(msg, "is_error", False))
        return info

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(_run())
    import concurrent.futures
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
        return ex.submit(asyncio.run, _run).result()


# ---- 会话状态与命令注册表（borrowed: Aider slash 体系 / Claude Code /cost） ----

class Session:
    def __init__(self, model: str | None, max_turns: int, permission_mode: str,
                 enable_subagents: bool | None = None):
        self.model = model
        self.max_turns = max_turns
        self.permission_mode = permission_mode
        self.enable_subagents = enable_subagents  # None=跟随全局默认
        self.session_id: str | None = None
        self.total_turns = 0
        self.total_cost = 0.0
        self.stop = False


def handle_command(text: str, session: Session) -> bool:
    """slash 命令注册表。返回 True 表示已处理（含未知命令：给提示且不发送给模型）。
    /exit /quit 置 session.stop。"""
    cmd = text.split()[0]
    if cmd in ("/exit", "/quit"):
        session.stop = True
        return True
    if cmd == "/new":
        session.session_id = None
        print(dim("· 已开新会话（核心记忆保留，上下文清空）"))
        return True
    if cmd == "/memory":
        core = load_core_memories_block(_PROJECT_ROOT / "workspace" / "agent"
                                        / "skills" / "orchestrator" / "memory" / "core")
        print(core or "（核心记忆为空）")
        return True
    if cmd == "/cost":
        print(dim(f"本轮会话累计：turns={session.total_turns} "
                  f"cost=${session.total_cost:.4f}"))
        return True
    if cmd == "/recommend":
        task = text[len("/recommend"):].strip()
        if not task:
            print(dim("· 用法：/recommend <任务描述>（独立上下文，仅建议不执行）"))
            return True
        try:
            result = _get_advisor().recommend(task, mode="all")
        except Exception as e:  # noqa: BLE001 - 顾问异常不拖垮 REPL
            print(f"[顾问异常] {e}")
            return True
        _print_advice(result)
        return True
    if cmd == "/translate":
        payload = text[len("/translate"):].strip()
        if not payload:
            print(dim("· 用法：/translate <要翻译的文本>（独立上下文，思考英文/结果中文）"))
            return True
        try:
            tr = _get_translator().translate(payload)
        except Exception as e:  # noqa: BLE001 - 翻译异常不拖垮 REPL
            print(f"[翻译异常] {e}")
            return True
        _print_translation(tr)
        return True
    if cmd == "/subagents":
        parts = text.split()
        arg = (parts[1] if len(parts) > 1 else "").lower()
        if arg in ("on", "1", "true", "yes"):
            session.enable_subagents = True
        elif arg in ("off", "0", "false", "no"):
            session.enable_subagents = False
        elif arg in ("default", "auto", ""):
            session.enable_subagents = None
        else:
            print(dim("· 用法：/subagents on|off|default"))
            return True
        state = {True: "开", False: "关", None: "跟随全局默认"}[session.enable_subagents]
        print(dim(f"· 子代理派发：{state}（下一轮生效）"))
        return True
    if cmd == "/help":
        print("命令：/new 新会话 | /memory 查看核心记忆 | /cost 累计用量 | /exit 退出")
        print("      /recommend <任务> 让顾问推荐并解释（独立上下文） | /subagents on|off|default 子代理派发开关")
        print("      /translate <文本> 把结果翻译成中文（独立上下文，思考英文/结果中文）")
        print("其余输入都是给大脑的任务；长对话由 Claude Code 内核自动压缩。")
        return True
    print(dim(f"· 未知命令 {cmd}（未发送；/help 查看命令）"))
    return True


def print_banner(session: Session) -> None:
    skills = ", ".join(s["name"] for s in scan_skills(str(_PROJECT_ROOT))) or "无"
    mcp_count = len(load_mcp_servers(os.environ.get("ORCH_MCP_CONFIG", "")) or {})
    core_count = len(list((_PROJECT_ROOT / "workspace" / "agent" / "skills"
                           / "orchestrator" / "memory" / "core").glob("*.md"))) \
        if (_PROJECT_ROOT / "workspace" / "agent" / "skills" / "orchestrator"
            / "memory" / "core").is_dir() else 0
    print("orch CLI · 单机编排模式（多 agent 工作流已加载）")
    print(dim(f"  模型: {session.model or 'SDK默认'}  权限: {session.permission_mode}  "
              f"MCP服务: {mcp_count}  技能: {skills}  核心记忆: {core_count} 条"))
    print(dim("  安全关卡①（指令）②（产物）生效中 · /help 查看命令"))


def repl(session: Session, resume: bool = False) -> None:
    if resume:
        session.session_id = load_session()
        print(dim(f"· 继续会话 {session.session_id}" if session.session_id
                  else "· 没有可恢复的历史会话，从头开始"))
    else:
        print_banner(session)
    while not session.stop:
        try:
            text = input("\n你> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not text:
            continue
        if text.startswith("/"):
            if handle_command(text, session):
                continue
        try:
            options = build_options(_PROJECT_ROOT, session.model, session.max_turns,
                                    session.permission_mode,
                                    resume_session_id=session.session_id,
                                    enable_subagents=session.enable_subagents)
            info = run_turn(text, options)
        except KeyboardInterrupt:
            print(dim("\n[已中断本轮]"))
            continue
        except Exception as e:
            print(f"\n[执行异常] {e}")
            continue
        session.session_id = info.get("session_id") or session.session_id
        session.total_turns += info.get("num_turns") or 0
        if info.get("cost_usd") is not None:
            session.total_cost += info["cost_usd"]
        save_session(session.session_id)
        footer = f"[turns={info.get('num_turns')} 累计cost=${session.total_cost:.4f}]"
        print(dim(footer))


def main() -> None:
    parser = argparse.ArgumentParser(description="orch CLI：单机编排模式交互壳")
    parser.add_argument("prompt", nargs="?", help="单发任务；省略则进入交互 REPL")
    parser.add_argument("--resume", action="store_true", help="继续上一次会话")
    parser.add_argument("--model", default=os.environ.get("ORCH_MODEL"),
                        help="大脑模型（默认 ORCH_MODEL 或 SDK 默认）")
    parser.add_argument("--max-turns", type=int,
                        default=int(os.environ.get("ORCH_MAX_TURNS", "100")))
    parser.add_argument("--permission-mode",
                        default=os.environ.get("ORCH_PERMISSION_MODE", "default"))
    # 子代理派发开关（互斥；缺省 None=跟随 ORCH_/AGENT_SUBAGENTS_ENABLED）
    sub = parser.add_mutually_exclusive_group()
    sub.add_argument("--subagents", dest="subagents", action="store_true",
                     default=None, help="本次运行启用子代理派发")
    sub.add_argument("--no-subagents", dest="subagents", action="store_false",
                     help="本次运行禁用子代理派发")
    # AI 推荐/解释顾问（独立上下文）
    parser.add_argument("--recommend", action="store_true",
                        help="只让顾问推荐并解释（独立上下文，默认不执行任务）")
    parser.add_argument("--advisor-mode",
                        default=os.environ.get("ORCH_ADVISOR_MODE", "all"),
                        choices=["pre_flight", "plan", "explain", "all"],
                        help="顾问模式（pre_flight/plan/explain/all）")
    parser.add_argument("--auto-apply", dest="auto_apply", action="store_true",
                        default=None,
                        help="顾问建议派发后自动执行（缺省跟随 ORCH_/AGENT_ADVISOR_AUTO_APPLY）")
    args = parser.parse_args()

    session = Session(args.model, args.max_turns, args.permission_mode,
                      enable_subagents=args.subagents)
    if args.resume:
        session.session_id = load_session()

    # --recommend：先跑独立上下文顾问，打印建议；仅当 auto-apply 且建议派发时才继续执行
    if args.prompt and args.recommend:
        try:
            result = _get_advisor().recommend(args.prompt, mode=args.advisor_mode)
        except Exception as e:  # noqa: BLE001 - 顾问异常不阻断主流程
            print(f"[顾问异常] {e}")
            return
        _print_advice(result)
        auto_apply = (args.auto_apply if args.auto_apply is not None
                      else _auto_apply_default())
        if not (auto_apply and getattr(result, "should_dispatch", False)):
            return
        print(dim("· auto-apply：按建议执行任务"))

    if args.prompt:
        options = build_options(_PROJECT_ROOT, session.model, session.max_turns,
                                session.permission_mode,
                                resume_session_id=session.session_id,
                                enable_subagents=args.subagents)
        info = run_turn(args.prompt, options)
        session.total_turns = info.get("num_turns") or 0
        if info.get("cost_usd") is not None:
            session.total_cost = info["cost_usd"]
        save_session(info.get("session_id"))
        print(dim(f"\n[turns={session.total_turns} cost=${session.total_cost:.4f}]"))
    else:
        repl(session, resume=args.resume)


if __name__ == "__main__":
    main()
