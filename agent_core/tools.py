"""工具管理：技能扫描、MCP 配置加载、进程内记忆工具。

scan_skills / load_mcp_servers 从 worker/agent_runner.py 原样迁移（逻辑不变），
使 worker、orch CLI、内核共用同一实现。build_memory_tools 为新增：用 SDK
@tool 装饰器把「检索经验 / 读写记忆」暴露为进程内工具，工具名与 runtime 的
mcp_server.py 对齐，便于两种接入方式（HTTP MCP / 进程内 SDK）行为一致。
"""
import json
import logging
import re
import sys
from pathlib import Path

log = logging.getLogger(__name__)


# ---- 共享记忆名校验（service / cli 共用，避免两处各写一套漂移） ----

# Windows 保留设备名：作为文件名会被设备吞（如 PUT core/NUL 实际写到
# NUL 设备而非磁盘文件），一律拒绝（大小写不敏感）
_WINDOWS_RESERVED_NAMES = {
    "con", "prn", "aux", "nul",
    *(f"com{i}" for i in range(1, 10)),
    *(f"lpt{i}" for i in range(1, 10)),
}
_MEMORY_NAME_RE = re.compile(r"[\w\-\u4e00-\u9fff]+")


def valid_memory_name(name: str) -> bool:
    """记忆名校验：仅字母/数字/下划线/连字符/中文，防路径穿越；
    并拒绝 Windows 保留设备名（con/prn/aux/nul/com1-9/lpt1-9）。"""
    if not name or _MEMORY_NAME_RE.fullmatch(name) is None:
        return False
    return name.lower() not in _WINDOWS_RESERVED_NAMES

# SDK @tool 装饰器：可用则构造真正的 SdkMcpTool；导入失败降级为 dict 定义
# （fallback，见 build_memory_tools 尾部说明）。
try:  # pragma: no cover - 取决于运行环境是否装了 claude-agent-sdk
    from claude_agent_sdk import tool as sdk_tool
except Exception:  # pragma: no cover
    sdk_tool = None

# embedding 模型进程内缓存：模型加载成本高（秒级/数百 MB），而 _make_local_backend
# 在每个任务的 _build_options 都会执行，按模型名缓存实例（含失败结果 None，
# 避免每个任务反复重试拖慢启动）。
_EMBEDDING_MODEL_CACHE: dict[str, object | None] = {}


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


def load_mcp_servers(path: str, *, return_warnings: bool = False):
    """加载 MCP 服务器配置（ORCH_MCP_CONFIG 指向的 JSON）。

    格式与 .mcp.json 一致：{"mcpServers": {"<名称>": {"command": ..., "args": [...],
    "env": {...}}} 或 {"url": ..., "headers": {...}}}。返回
    {名称: 配置dict}（即 SDK mcp_servers 形状）；文件缺失/为空/格式错返回 None，
    不让 MCP 配置问题拖垮任务执行。

    return_warnings=True（doctor 自检用）：返回 ``(servers, warnings)`` 二元组，
    warnings 逐条说明被过滤/损坏的条目；默认 False 保持原有返回值形状不变。
    """
    warnings: list[str] = []
    if not path:
        return (None, warnings) if return_warnings else None
    p = Path(path)
    if not p.is_file():
        warnings.append(f"MCP 配置文件不存在：{path}")
        return (None, warnings) if return_warnings else None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        servers = data.get("mcpServers") or {}
        if not isinstance(servers, dict) or not servers:
            warnings.append("MCP 配置缺少有效的 mcpServers 映射（为空或类型错误）")
            return (None, warnings) if return_warnings else None
        cleaned = {}
        for name, cfg in servers.items():
            if not isinstance(cfg, dict):
                warnings.append(f"MCP 条目 '{name}' 不是对象（dict），已跳过")
                continue
            if not (cfg.get("command") or cfg.get("url")):
                warnings.append(f"MCP 条目 '{name}' 缺少 command/url 字段，已跳过")
                continue
            cleaned[name] = cfg
        result = cleaned or None
        if not cleaned:
            warnings.append("MCP 配置中没有可用条目（全部被过滤）")
        return (result, warnings) if return_warnings else result
    except Exception as e:
        warnings.append(f"MCP 配置 JSON 解析失败：{e}")
        return (None, warnings) if return_warnings else None


# ---- 记忆后端：local 直连本地 store，remote 经 brain HTTP API ----

def _runtime_dir(config) -> Path:
    """定位大脑 runtime 目录（memory_store / experience_store 所在），供本地后端 import。"""
    return (Path(config.project_root) / "workspace" / "agent"
            / "orchestrator" / "runtime")


def ensure_runtime_on_path(config) -> Path:
    """把大脑 runtime 目录加入 sys.path（幂等），返回其路径。

    供 agent_core 内延迟导入 runtime 模块（memory_store / experience_store /
    worker_api 等）前调用，收敛各处「拼路径 + sys.path.insert」的样板，避免
    内核重复写同一段依赖路径逻辑。
    """
    rt = _runtime_dir(config)
    if str(rt) not in sys.path:
        sys.path.insert(0, str(rt))
    return rt


def _load_embedding_model(config):
    """按 config 解析 ExperienceStore 的 embedding 模型（阶段四记忆修复）。

    - config.embedding_model 为空：沿用 runtime embedding_model.
      create_embedding_model() 的默认逻辑（ORCH_EMBEDDING_MODEL，缺省
      all-MiniLM-L6-v2）；
    - "none"/"off"：显式禁用语义检索（纯关键词）；
    - 其他值：作为模型名直接构造。
    任何加载失败（依赖缺失/模型下载失败）都降级返回 None，关键词检索照常可用。
    须在 runtime 目录已加入 sys.path 后调用（import 依赖 runtime 模块）。
    """
    name = (getattr(config, "embedding_model", "") or "").strip()
    key = name.lower()
    if key in _EMBEDDING_MODEL_CACHE:
        return _EMBEDDING_MODEL_CACHE[key]
    model: object | None = None
    if key not in ("none", "off"):
        try:
            from embedding_model import (SentenceTransformerModel,
                                         create_embedding_model)
            model = (SentenceTransformerModel(name) if name
                     else create_embedding_model())
        except Exception:
            log.warning("embedding 模型加载失败，经验检索降级为纯关键词",
                        exc_info=True)
            model = None
    _EMBEDDING_MODEL_CACHE[key] = model
    return model


def _make_local_backend(config):
    """构造 local 后端：延迟把 runtime 目录加入 sys.path 并 import 本地 store。

    返回一组异步函数（search/read_core/write_core/write_context），内部同步调用
    FileMemoryStore / ExperienceStore。runtime 不可用时抛出异常，由工具层转 is_error。
    """
    rt = ensure_runtime_on_path(config)
    from memory_store import FileMemoryStore  # noqa: E402  延迟导入避免循环依赖
    from experience_store import ExperienceStore  # noqa: E402

    mem = FileMemoryStore(config.memory_core_dir, config.memory_context_dir)
    # 阶段四记忆修复：注入 embedding 模型启用语义检索（与 brain 侧 brain_api
    # 构造 ExperienceStore 的方式对齐）；加载失败降级为纯关键词，行为不变
    exp = ExperienceStore(config.experience_store_dir,
                          embedding_model=_load_embedding_model(config))

    async def search_experiences(query, role=None, successful_only=True, limit=5):
        recs = exp.search(query, role=role, successful_only=successful_only,
                          limit=limit)
        # embedding 向量对调用方无用且巨大，剥离后再返回（与 mcp_server 一致）
        return [{k: v for k, v in r.__dict__.items() if k != "embedding"}
                for r in recs]

    async def read_core_memory(name):
        if not valid_memory_name(name):
            raise ValueError(f"非法记忆名：{name!r}")
        content = mem.read_core(name)
        return {"name": name, "content": content}

    async def write_core_memory(name, content):
        if not valid_memory_name(name):
            raise ValueError(f"非法记忆名：{name!r}")
        mem.write_core(name, content)
        return {"status": "written", "name": name}

    async def write_task_context(task_id, key, content):
        if not valid_memory_name(task_id):
            raise ValueError(f"非法任务 ID：{task_id!r}")
        if not valid_memory_name(key):
            raise ValueError(f"非法上下文键：{key!r}")
        mem.write_context(task_id, key, content)
        return {"status": "written", "task_id": task_id, "key": key}

    return {
        "search_experiences": search_experiences,
        "read_core_memory": read_core_memory,
        "write_core_memory": write_core_memory,
        "write_task_context": write_task_context,
    }


def _make_remote_backend(config):
    """构造 remote 后端：经 brain HTTP API 调用（httpx 异步），端点对齐 brain_api。

    返回与 local 后端同名的异步函数字典，屏蔽底层传输差异。
    """
    base = config.brain_url.rstrip("/")
    api_key = config.api_key

    def _headers():
        return {"X-API-Key": api_key} if api_key else {}

    async def _request(method, path, json_body=None, params=None):
        import httpx
        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.request(method, base + path, json=json_body,
                                        params=params, headers=_headers())
            resp.raise_for_status()
            return resp.json()

    async def search_experiences(query, role=None, successful_only=True, limit=5):
        result = await _request("POST", "/experiences/search", json_body={
            "query": query, "role": role,
            "successful_only": successful_only, "limit": limit,
        })
        if isinstance(result, list):
            return [{k: v for k, v in r.items() if k != "embedding"}
                    for r in result]
        return result

    async def read_core_memory(name):
        return await _request("GET", f"/memory/core/{name}")

    async def write_core_memory(name, content):
        return await _request("PUT", f"/memory/core/{name}",
                              json_body={"content": content})

    async def write_task_context(task_id, key, content):
        return await _request("PUT", f"/memory/context/{task_id}/{key}",
                              json_body={"content": content})

    return {
        "search_experiences": search_experiences,
        "read_core_memory": read_core_memory,
        "write_core_memory": write_core_memory,
        "write_task_context": write_task_context,
    }


# 工具元数据：名称、描述、input_schema（映射形）、参数顺序。集中声明，
# 供 SDK @tool 与 dict fallback 两种路径共用，避免重复。
_TOOL_SPECS = [
    {
        "name": "search_experiences",
        "description": "按关键词+语义混合检索历史任务经验（prompt/输出摘要/安全裁决）。"
                       "做新任务前先检索相似经验，避免重复踩坑。role 可按角色过滤。",
        "schema": {"query": str, "role": str | None,
                   "successful_only": bool, "limit": int},
        "call": lambda be, a: be["search_experiences"](
            a.get("query", ""), role=a.get("role"),
            successful_only=a.get("successful_only", True),
            limit=a.get("limit", 5)),
    },
    {
        "name": "read_core_memory",
        "description": "读一条核心记忆。name 为记忆名（如 proj-pref）。",
        "schema": {"name": str},
        "call": lambda be, a: be["read_core_memory"](a.get("name", "")),
    },
    {
        "name": "write_core_memory",
        "description": "写核心记忆（持久、跨任务共识写入的高价值内容）。"
                       "临时过程信息请用 write_task_context，不要写进核心记忆。"
                       "写入成功后向用户复述记忆名与内容，确认已保存。",
        "schema": {"name": str, "content": str},
        "call": lambda be, a: be["write_core_memory"](
            a.get("name", ""), a.get("content", "")),
    },
    {
        "name": "write_task_context",
        "description": "写某任务的上下文记忆笔记（中间产物路径、已试方案、临时决策等过程信息）。",
        "schema": {"task_id": str, "key": str, "content": str},
        "call": lambda be, a: be["write_task_context"](
            a.get("task_id", ""), a.get("key", ""), a.get("content", "")),
    },
]


def _text_result(payload) -> dict:
    """把后端返回值包成 MCP 文本内容格式（SDK @tool 约定）。"""
    return {"content": [{"type": "text",
                         "text": json.dumps(payload, ensure_ascii=False,
                                            default=str)}]}


def build_memory_tools(config: "AgentConfig") -> list:
    """构造进程内记忆工具列表（SDK @tool 格式）。

    根据 config.memory_backend 决定后端：
    - "local": 直接调用本地 FileMemoryStore / ExperienceStore
    - "remote": 经 brain HTTP API（httpx）调用

    工具名对齐 runtime/mcp_server.py：search_experiences、read_core_memory、
    write_core_memory、write_task_context。

    返回工具列表：若 claude_agent_sdk.tool 可用，返回 SdkMcpTool 列表，可经
    create_sdk_mcp_server(...) 包成进程内 MCP server 挂到 ClaudeAgentOptions
    的 mcp_servers；SDK 不可用时降级为工具定义 dict 列表（fallback，含
    name/description/input_schema），由上层按需另行接线。
    """
    if config.memory_backend == "remote":
        backend = _make_remote_backend(config)
    else:
        backend = _make_local_backend(config)

    tools: list = []
    for spec in _TOOL_SPECS:
        if sdk_tool is not None:
            # 用闭包绑定当前 spec，避免 late-binding 拿到循环末尾的值
            def _handler(args, _spec=spec):
                return _run_tool(backend, _spec, args)
            tools.append(sdk_tool(spec["name"], spec["description"],
                                  spec["schema"])(_handler))
        else:
            # fallback：SDK 不可用时给出工具定义字典，上层可自行接线
            tools.append({
                "name": spec["name"],
                "description": spec["description"],
                "input_schema": spec["schema"],
            })
    return tools


async def _run_tool(backend: dict, spec: dict, args: dict) -> dict:
    """执行单个记忆工具：调用后端并包装结果；异常转 is_error 文本，不抛出。"""
    try:
        payload = await spec["call"](backend, args or {})
        return _text_result(payload)
    except Exception as e:  # 后端错误（如 runtime 不可达）以工具错误返回
        return {"content": [{"type": "text", "text": f"{spec['name']} 失败：{e}"}],
                "is_error": True}


def build_memory_mcp_server(config: "AgentConfig"):
    """便捷封装：把 build_memory_tools 的结果包成进程内 MCP server 配置。

    SDK 不可用或无工具时返回 None。返回的 dict 可直接并入
    ClaudeAgentOptions 的 mcp_servers。
    """
    tools = build_memory_tools(config)
    if sdk_tool is None or not tools:
        return None
    try:
        from claude_agent_sdk import create_sdk_mcp_server
    except Exception:
        return None
    return create_sdk_mcp_server(name="agent_memory", version="1.0.0",
                                 tools=tools)
