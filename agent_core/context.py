"""统一提示拼装。

合并原分散逻辑：
- worker/agent_runner.py 的 build_env_block（项目根 + 技能清单）
- runtime/orch_cli.py 的 build_brain_env_block / load_core_memories_block /
  build_system_prompt

关键设计（利于 SDK prompt caching）：
- system prompt 只含**跨任务稳定**的前缀：persona 正文 + 核心记忆 + 运行环境。
  这些内容不随单个任务变化，拼在一起可让前缀缓存跨任务复用。
- **任务特有**信息（工作目录、context_memory）不进 system prompt，改由
  build_user_message_prefix 放到用户消息开头，避免破坏缓存前缀。
"""
from pathlib import Path

from .persona import Persona


def load_core_memories_block(core_dir: str | Path) -> str:
    """核心记忆自动加载块（大脑 SKILL.md 约定：直接在会话里，无需额外注入）。

    读取 core_dir 下所有 *.md，按文件名排序拼为带小标题的记忆块。
    目录不存在或无有效内容时返回空串（调用方据此决定是否拼接）。
    """
    d = Path(core_dir)
    if not d.is_dir():
        return ""
    memories = []
    for p in sorted(d.glob("*.md")):
        try:
            content = p.read_text(encoding="utf-8").strip()
        except Exception:
            continue  # 单条记忆损坏不拖垮整体加载
        if content:
            memories.append(f"### {p.stem}\n{content}")
    if not memories:
        return ""
    return "\n---\n## 核心记忆（自动加载，跨任务持久）\n" + "\n\n".join(memories)


def build_env_block(project_root: str | Path, skills: list[dict] | None = None,
                    sub_agents: dict | None = None) -> str:
    """运行环境注入块：告诉 agent 项目根、技能清单（绝对路径）与子 agent 情况。

    只含跨任务稳定的内容，拼在 system prompt 末尾可让前缀缓存跨任务复用。
    有 sub_agents 时输出「子 agent 已注册」（大脑姿态，用 Agent 工具派发）；
    否则输出纯技能列表（executor 姿态，直接按绝对路径调用技能）。
    任务工作目录是每任务变化的信息，不在此块，见 build_user_message_prefix。
    """
    lines = [
        "",
        "---",
        "## 运行环境（内核注入，路径以本块为准）",
        f"- 项目根（技能资产所在，只读）：{Path(project_root).resolve()}",
        "- 当前任务的工作目录见任务消息开头的「任务工作目录」，所有产物写到那里",
    ]
    if sub_agents:
        names = "、".join(sorted(sub_agents.keys()))
        lines.append(f"- 子 agent 已注册：{names}，用 Agent 工具派发")
        lines.append("- 可用技能（在派发 prompt 里写 [技能名] 标记即路由子 agent 执行）：")
    else:
        lines.append("- 可用技能（调用时一律用下列绝对路径）：")
    if skills:
        for s in skills:
            trigger_hint = (f"（触发词：{'、'.join(s['triggers'])}）"
                            if s.get("triggers") else "")
            if sub_agents:
                # 大脑姿态只需名字与触发词，绝对路径由子 agent 侧的环境块给出
                lines.append(f"  - {s['name']}{trigger_hint}")
            else:
                lines.append(f"  - {s['name']}：{s['path']} —— "
                             f"{s.get('description', '')}{trigger_hint}")
    else:
        lines.append("  - 无（未扫描到带 SKILL.md 的技能目录）")
    return "\n".join(lines)


def build_memory_guidance_block(memory_backend: str = "local") -> str:
    """记忆使用指引块：引导 agent 在推理循环中主动使用记忆工具。

    仅在记忆工具已注入时附加到 system prompt 末尾。内容跨任务稳定，
    放进 system prompt 前缀不会破坏 SDK prompt caching。
    memory_backend 目前仅用于措辞（local/remote 工具名一致）。
    """
    return (
        "\n---\n"
        "## 记忆与经验（自主使用）\n"
        "你拥有以下记忆工具，请在执行任务时主动使用：\n"
        "- **search_experiences**：做任务前先检索相似历史经验，了解前人的做法和踩坑记录\n"
        "- **write_task_context**：产出重要中间结论、发现关键信息时，随手记录到任务上下文\n"
        "- **read_core_memory**：需要项目约定/偏好时查阅核心记忆\n"
        "- **write_core_memory**：任务完成后若有高价值发现（项目约定、最佳实践），考虑写入核心记忆\n"
        "\n"
        "使用原则：\n"
        "1. 任务开始时：search_experiences 检索相关经验\n"
        "2. 执行过程中：重要中间结论用 write_task_context 记录\n"
        "3. 任务结束时：总结关键发现，评估是否值得写入核心记忆"
    )


def build_system_prompt(persona: Persona, project_root: str | Path,
                        core_memory_dir: str | Path | None = None,
                        skills: list[dict] | None = None,
                        include_memory_guidance: bool = True,
                        memory_backend: str = "local") -> str:
    """拼装稳定系统提示 = persona 正文 + 核心记忆块 + 运行环境块 + 记忆指引块。

    前四段都跨任务稳定，组合成前缀利于 SDK prompt caching。
    core_memory_dir 为空则跳过核心记忆；skills 为空时环境块仍输出项目根。
    子 agent 姿态由 persona.sub_agents 是否非空决定（见 build_env_block）。

    include_memory_guidance=True 且 core_memory_dir 非空（意味着记忆系统已配置、
    记忆工具已注入）时，在末尾追加「记忆与经验」使用指引，引导 agent 主动调用
    记忆工具。core_memory_dir 为空时不追加，避免在无记忆能力时误导。
    """
    parts = [persona.system_prompt.strip()] if persona.system_prompt.strip() else []
    if core_memory_dir:
        core = load_core_memories_block(core_memory_dir)
        if core:
            parts.append(core)
    parts.append(build_env_block(project_root, skills=skills,
                                 sub_agents=persona.sub_agents or None))
    if include_memory_guidance and core_memory_dir:
        parts.append(build_memory_guidance_block(memory_backend))
    return "\n\n".join(parts)


def build_user_message_prefix(cwd: str, context_memory: str = "") -> str:
    """拼装用户消息前缀：任务特有的工作目录与上下文记忆。

    这些信息每任务变化，必须放用户消息开头而非 system prompt，避免破坏
    跨任务复用的缓存前缀。调用方在其后追加真正的任务 prompt。
    """
    prefix = f"[任务工作目录（所有产物写到这里）]\n{Path(cwd).resolve()}"
    if context_memory:
        prefix += f"\n\n[上下文记忆]\n{context_memory}"
    return prefix
