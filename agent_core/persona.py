"""统一人设（persona）加载器。

合并原分散逻辑：
- worker/agent_runner.py 的 _parse_executor_def（解析单个 .md frontmatter）
- runtime/orch_cli.py 的 parse_subagents（批量解析 .claude/agents/*.md）
- runtime/orch_cli.py 的 strip_frontmatter

约定：人设是带 YAML frontmatter 的 Markdown 文件。frontmatter 用 --- 分隔，
声明 name/description/model/tools 等元数据，正文即该 agent 的系统提示。
tools 在 frontmatter 中为逗号分隔字符串，加载时统一转为列表；缺省为 None
表示放开全部工具。单个文件损坏不拖垮整体（try/except continue）。
"""
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class Persona:
    """一个 agent 的人设定义。

    name: agent 名（如 executor）。
    description: 一句话职责，供大脑路由/展示。
    model: 覆盖模型（如 sonnet/haiku）；None 表示走 SDK/上层默认。
    tools: 允许的工具名列表；None 表示放开全部工具。
    system_prompt: frontmatter 之后的正文，即该 agent 的系统提示主体。
    sub_agents: SDK options.agents 形状的子 agent 定义（可选）。
    """

    name: str
    description: str = ""
    model: str | None = None
    # 允许的工具名列表；None = 全工具（放开全部）。
    # 安全建议（阶段一）：生产/多租户场景按「最小够用」显式列出工具，
    # 只读角色参考 security-guard.md（Read/Glob/Grep/WebFetch）；
    # 该列表最终映射为 SDK options.allowed_tools（见 kernel._build_options）。
    tools: list[str] | None = None
    system_prompt: str = ""
    sub_agents: dict = field(default_factory=dict)  # SDK agents 选项


def _split_frontmatter(text: str) -> tuple[dict, str]:
    """拆分 Markdown 文本为 (frontmatter 元数据 dict, 正文 str)。

    无 frontmatter（不以 --- 开头或不足三段）时返回 ({}, 原文)。
    YAML 解析失败按空元数据处理，正文仍尽量保留，避免整体加载失败。
    """
    if not text.startswith("---"):
        return {}, text.strip()
    parts = text.split("---", 2)
    if len(parts) < 3:
        return {}, text.strip()
    meta: dict = {}
    try:
        import yaml
        loaded = yaml.safe_load(parts[1])
        if isinstance(loaded, dict):
            meta = loaded
    except Exception:
        meta = {}
    return meta, parts[2].strip()


def _normalize_tools(raw) -> list[str] | None:
    """把 frontmatter 的 tools 字段归一化为列表。

    支持逗号分隔字符串（如 "Read, Write"）或已是列表；空/缺省返回 None
    表示放开全部工具。
    """
    if raw is None:
        return None
    if isinstance(raw, (list, tuple)):
        items = [str(t).strip() for t in raw]
    else:
        items = [t.strip() for t in str(raw).split(",")]
    items = [t for t in items if t]
    return items or None


def strip_frontmatter(text: str) -> str:
    """去掉 Markdown 的 YAML frontmatter，返回正文。"""
    return _split_frontmatter(text)[1]


def load_persona(path: str | Path) -> Persona:
    """从单个 .md 文件加载人设。

    文件不存在时抛出 FileNotFoundError（由调用方决定是否兜底）。
    name 缺省取文件名主干；tools 逗号串转列表；model 透传。
    """
    p = Path(path)
    text = p.read_text(encoding="utf-8")
    meta, body = _split_frontmatter(text)
    return Persona(
        name=str(meta.get("name") or p.stem),
        description=str(meta.get("description") or ""),
        model=meta.get("model") or None,
        tools=_normalize_tools(meta.get("tools")),
        system_prompt=body,
    )


def load_persona_by_name(name: str, persona_dir: str | Path) -> Persona:
    """按名称在 persona_dir 下查找 <name>.md 并加载。

    找不到文件时抛出 FileNotFoundError，便于上层区分「未定义」与「加载失败」。
    """
    p = Path(persona_dir) / f"{name}.md"
    if not p.is_file():
        raise FileNotFoundError(f"persona 定义不存在：{p}")
    return load_persona(p)


def list_persona_names(persona_dir: str | Path) -> list[str]:
    """列出 persona_dir 下可用人设名（*.md 文件名主干，排序）。

    供 doctor 自检与 resolve_persona 未命中时的兜底提示使用：
    目录不存在或为空返回空列表，不抛异常。
    """
    d = Path(persona_dir)
    if not d.is_dir():
        return []
    return sorted(p.stem for p in d.glob("*.md"))


def parse_subagents(agents_dir: str | Path) -> dict:
    """批量解析 .claude/agents/*.md 为 SDK options.agents 形状。

    输出 {name: {"description": str, "prompt": str, "model"?: str,
    "tools"?: list}}。name 作字典键；缺 name/description 的条目跳过；
    单个文件损坏不拖垮整体（try/except continue）。
    """
    d = Path(agents_dir)
    if not d.is_dir():
        return {}
    agents: dict = {}
    for p in sorted(d.glob("*.md")):
        try:
            text = p.read_text(encoding="utf-8")
            meta, body = _split_frontmatter(text)
            if not meta.get("name") or not meta.get("description"):
                continue
            definition: dict = {
                "description": meta["description"],
                "prompt": body,
            }
            if meta.get("model"):
                definition["model"] = meta["model"]
            tools = _normalize_tools(meta.get("tools"))
            if tools:
                definition["tools"] = tools
            agents[str(meta["name"])] = definition
        except Exception:
            continue  # 单个定义损坏不拖垮启动
    return agents
