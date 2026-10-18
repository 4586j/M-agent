"""统一 Agent 配置。

单机 CLI、本地服务、集群 worker、大脑编排共用同一份配置形状，避免各处各写一套。
字段来源合并自 worker/worker_config.py（模型与执行控制、MCP、路径）与
runtime/config.py（记忆目录）。全部从环境变量加载，前缀优先 AGENT_，
向后兼容 ORCH_；缺省值基于本文件位置定位项目根，避免相对路径随 cwd 漂移。
"""
import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

# agent_core/ 的上一级即项目根（本包放在 <project_root>/agent_core/ 下）
_PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _env(default: str, *names: str) -> str:
    """按顺序取第一个已设置且非空的环境变量；都没有则返回 default。

    统一 AGENT_ 优先、ORCH_ 兜底，让新旧环境变量并存时行为可预期。
    """
    for name in names:
        val = os.environ.get(name)
        if val is not None and val != "":
            return val
    return default


def _default_project_root() -> str:
    """项目根：本包所在目录的上一级。executor 据此定位 skills 等资产。"""
    return str(_PROJECT_ROOT)


def _default_work_dir() -> str:
    """内核默认工作区，基于项目根定位，避免相对路径随 cwd 漂移。"""
    return str(_PROJECT_ROOT / ".agent_work")


def _default_persona_dir() -> str:
    """.claude/agents/ 目录：人设（executor / security-guard 等）定义所在。"""
    return str(_PROJECT_ROOT / ".claude" / "agents")


def _default_memory_dir() -> Path:
    """框架约定的记忆根目录，与大脑主会话共用一套。"""
    return (_PROJECT_ROOT / "workspace" / "agent" / "skills"
            / "orchestrator" / "memory")


def _default_runtime_dir() -> Path:
    """大脑运行时目录（经验库等落地位置）。"""
    return _PROJECT_ROOT / "workspace" / "agent" / "orchestrator" / "runtime"


def _default_experience_store_dir() -> str:
    """经验库目录（runtime/.experiences）。"""
    return str(_default_runtime_dir() / ".experiences")


@dataclass
class AgentConfig:
    """统一 Agent 配置。所有字段可从环境变量覆盖（见 load_agent_config）。"""

    # 模型与执行控制
    model: str | None = None
    max_turns: int = 50                 # 单任务最大轮次，防失控循环烧 token
    max_thinking_tokens: int | None = 10000  # 思考 token 上限（最大成本项）；None/0=不限制
    task_budget_usd: float | None = None     # 单任务美元硬预算；None/0=不限制
    task_timeout: int = 900             # 单任务墙钟超时（秒）
    # 默认 default（SDK 权限门控生效）；bypassPermissions 须显式设置（仅限开发/受控环境）
    permission_mode: str = "default"
    # 路径
    project_root: str = ""              # 项目根目录
    work_dir: str = ""                  # 任务工作目录
    persona_dir: str = ""               # .claude/agents/ 目录
    # MCP 与工具
    mcp_config_path: str = ""           # ORCH_MCP_CONFIG 指向的 JSON
    mcp_servers: dict | None = None     # 预加载的 MCP 服务器配置（优先于 mcp_config_path）
    # 记忆
    memory_core_dir: str = ""
    memory_context_dir: str = ""
    experience_store_dir: str = ""
    memory_backend: str = "local"       # local | remote
    brain_url: str = "http://127.0.0.1:7000"
    api_key: str = ""
    # 增强功能
    planning_enabled: bool = False
    reflection_enabled: bool = False
    rag_enabled: bool = False
    streaming: bool = True
    auto_record_experience: bool = True
    # 阶段四记忆修复：经验检索 embedding 模型（空=跟随 runtime embedding_model
    # 的默认逻辑；"none"/"off" 显式禁用；其他=模型名）。加载失败自动降级为纯关键词。
    embedding_model: str = ""
    # 核心记忆按任务相关度筛选 Top-K 注入（默认 False=全量注入，行为不变）
    memory_core_retrieval: bool = False
    # Plan-Execute 断点续跑：计划年龄上限（小时，按计划 created_at 计算）。
    # 超龄不续跑、直接开新计划，避免定时任务/评测同 prompt 错配陈旧计划；<=0 不限。
    plan_resume_max_age_hours: float = 24.0
    # 输出质量守卫（output_guard.py）：检出退化输出（乱码/重复循环）后自动重试，
    # 耗尽仍退化则熔断（is_error=True / error_type="degenerate_output"）。
    output_guard_enabled: bool = True
    # 首次检出后自动重试次数上限（第 1 次注入纠偏提示，第 2 次换 fallback_model）。默认 1：
    # 重试各自得完整 per-query 预算且强规则检测保守化后误判率低，少重试减少成本放大
    output_guard_max_retries: int = 1
    # 第 2 次重试使用的备用模型；空=不换模型（仍注入提示重试）
    fallback_model: str = ""
    # ---- 运行时 Provider（模型/端点/密钥切换；persona / 任务级覆盖优先）----
    # provider_base_url / provider_auth_token：全局默认三元组（第三层兜底）。
    # 非空时 kernel._build_options 注入 SDK options.env（ANTHROPIC_BASE_URL /
    # ANTHROPIC_AUTH_TOKEN）覆盖子进程 env；空则回落进程 env（第四层）。
    provider_base_url: str = ""
    provider_auth_token: str = ""
    # provider_pool：自动故障切换候选 [{model, base_url, auth_token}, ...]。
    # 「有池才切」：sdk_error/429/超时/空结果时依序切下一候选重试，最多 len(pool) 次。
    provider_pool: list[dict] | None = None
    # ---- 自纠错闭环（result_validator.py）----
    correction_enabled: bool = True       # 自纠错闭环开关
    correction_max_retries: int = 2       # 修正重试上限
    # ---- 上下文预算管理 ----
    session_summarize_enabled: bool = True   # 会话摘要开关
    session_summarize_interval: int = 5      # 每 N 轮触发摘要
    session_max_context_chars: int = 80000   # 会话上下文总预算
    rag_budget_ratio: float = 0.15          # RAG 占总预算的最大比例
    # ---- 工具调用守卫（tool_guard.py）----
    tool_guard_enabled: bool = True          # 工具守卫开关
    tool_guard_timeout: int = 30             # 工具调用超时（秒）
    tool_guard_cache_ttl: int = 300          # 工具结果缓存 TTL（秒）
    # ---- 计划验证（plan_validator.py）----
    plan_validation_enabled: bool = True     # 计划验证开关
    plan_parallel_steps: bool = False        # [预留] 并行步骤执行（默认关）：无生产引用，仅测试引用，待并行步骤接入
    plan_replan_enabled: bool = True         # 动态重规划开关
    # ---- 安全增强 ----
    rate_limit_per_minute: int = 60          # 每分钟请求上限
    # ---- 任务自适应路由（task_router.py）----
    auto_routing: bool = False               # 自适应路由（默认关）
    # ---- Prompt Injection 检测（prompt_guard.py）----
    prompt_guard_enabled: bool = True        # 注入检测开关
    prompt_guard_action: str = "reject"      # "reject" | "warn"（检出后拒绝或仅告警）
    # ---- 工具响应验证（tool_response_guard.py）----
    tool_response_validation_enabled: bool = True  # 工具响应验证开关
    # ---- 运行历史（run_history.py）----
    run_history_dir: str = ""                      # 历史记录存储目录（默认用 work_dir）
    run_history_max_records: int = 1000            # 历史记录上限
    # ---- 持久化执行日志（run_store.py，Slice 3 rollout）----
    # 显式 rollout 开关：默认关（完全走既有 JSON 权威路径，零行为变化）；开启后
    # 本地执行以 SQLite 日志作为持久恢复底座（准入/尝试/副作用/审批 + 启动对账），
    # 历史 JSON 以幂等导入投影进来，保留供回滚/导出，不做双权威写入。
    durable_journal_enabled: bool = False
    journal_path: str = ""                         # 空=派生 <work_dir>/runtime.sqlite3
    max_active_executions: int = 1                 # 全局并发在飞尝试上限（有界准入）
    # ---- 子代理派发开关 + AI 推荐/解释顾问（advisor.py）----
    # subagents_enabled：全局闸——控制 orchestrator 人设的 sub_agents 是否映射为
    # SDK options.agents（kernel._build_options 单一源）。默认 True=行为不变。
    subagents_enabled: bool = True
    # advisor_enabled：是否启用「推荐并解释」顾问端点/命令。默认 True。
    advisor_enabled: bool = True
    # advisor_auto_apply：顾问给出 should_dispatch 后是否自动派发（默认 False=仅建议）。
    advisor_auto_apply: bool = False
    # advisor_model：顾问专用模型（空=跟随 config.model）。顾问是独立上下文的一次性调用。
    advisor_model: str = ""
    # advisor_work_dir：顾问的隔离工作目录（空=派生 <work_dir>/.advisor_work）。
    # 顾问每次调用在 <advisor_work_dir>/adv-<uuid8> 下运行，绝不复用任务 cwd。
    advisor_work_dir: str = ""
    # advisor_use_plan_engine：plan/all 模式下顾问是否复用 PlanEngine 产出校验过的拆解。
    advisor_use_plan_engine: bool = False
    # ---- 结果翻译（translator.py）----
    # result_translate_enabled：自动翻译开关——开启后 kernel.run_oneshot 在结果边界
    # 把最终产出译为目标语言（思考过程/流式 token 保持原语言，通常为英文）。
    # 默认 False=行为不变。顾问/翻译自身的隔离配置会再关一次，杜绝递归翻译。
    result_translate_enabled: bool = False
    # translate_model：翻译专用模型。优先级 translate_model > advisor_model > model。
    translate_model: str = ""
    # translate_target_lang：翻译目标语言（默认中文）。思考用英文、结果用中文。
    translate_target_lang: str = "zh"
    # translate_work_dir：翻译的隔离工作目录（空=派生 <work_dir>/.translate_work）。
    # 每次翻译在 <translate_work_dir>/tr-<uuid8> 下运行，绝不复用任务 cwd。
    translate_work_dir: str = ""
    # Personal HTTP/UI only; never consulted by AgentKernel or orchestration workers.
    understanding_enabled: bool = True
    understanding_max_cost_usd: float = 0.1
    understanding_budget_usd: float | None = None


def _to_bool(raw: str, default: bool) -> bool:
    """把环境变量字符串解析为布尔。空串按 default，其余 truthy 词表判定。"""
    if raw == "":
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _to_provider_pool(raw: str) -> list[dict] | None:
    """解析 provider_pool 环境变量（JSON 数组 [{model, base_url, auth_token}]）。

    空串/非法 JSON → 返回 None（视为未配置，“有池才切”下不启用故障切换）；
    损坏条目（非对象）跳过。密钥只来自环境变量装配，不进日志。
    """
    raw = (raw or "").strip()
    if not raw:
        return None
    try:
        parsed = json.loads(raw)
    except Exception as e:
        log.warning("AGENT_PROVIDER_POOL 解析失败，视为未配置：%s", e)
        return None
    if not isinstance(parsed, list):
        log.warning("AGENT_PROVIDER_POOL 应为 JSON 数组，视为未配置")
        return None
    pool = []
    for item in parsed:
        if not isinstance(item, dict):
            continue
        pool.append({
            "model": str(item["model"]) if item.get("model") else None,
            "base_url": str(item["base_url"]) if item.get("base_url") else None,
            "auth_token": str(item["auth_token"]) if item.get("auth_token") else None,
        })
    return pool or None


def _to_optional_int(raw: str) -> int | None:
    """解析可选整型：空串或非法值返回 None（表示不限制）。"""
    raw = raw.strip()
    if not raw:
        return None
    try:
        val = int(raw)
    except ValueError:
        return None
    return val or None  # 0 视为不限制


def _to_optional_float(raw: str) -> float | None:
    """解析可选浮点：空串或非法值返回 None；0 视为不限制。"""
    raw = raw.strip()
    if not raw:
        return None
    try:
        val = float(raw)
    except ValueError:
        return None
    return val or None


def _to_float(raw: str, default: float) -> float:
    """解析带默认值的浮点：空串或非法值返回 default。"""
    raw = raw.strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def load_dotenv(path: str | os.PathLike | None = None,
                *, override: bool = False) -> str | None:
    """把 .env 文件的 KEY=VALUE 并入 os.environ，免去每次启动手动设环境变量。

    发现顺序：显式 path > 环境变量 AGENT_ENV_FILE > 项目根 <root>/.env。
    默认「不覆盖」——已存在的真实环境变量优先于文件值（符合 dotenv 惯例），
    因此仍可在 shell 里临时用 $env:XXX 覆盖文件中的值。

    解析规则（够用即可，不追求完整 dotenv 规范）：
    - 逐行处理，跳过空行与以 # 开头的注释；
    - 允许行首 export 前缀；以第一个 = 分割 key/value；
    - key/value 去首尾空白，value 再剥离成对的单/双引号；无 = 的行忽略。

    返回实际读取的文件路径（str）；文件不存在或不可读返回 None。
    由 load_agent_config 自动调用，使 CLI/HTTP/worker/brain 各入口一致生效；
    其中 ANTHROPIC_API_KEY 进入 os.environ 后，claude-agent-sdk 派生的
    Claude Code CLI 子进程即可继承完成鉴权。
    """
    if path is None:
        explicit = os.environ.get("AGENT_ENV_FILE", "")
        path = explicit if explicit else str(_PROJECT_ROOT / ".env")
    p = Path(path)
    try:
        if not p.is_file():
            return None
        text = p.read_text(encoding="utf-8")
    except OSError:
        return None
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        key, sep, val = line.partition("=")
        if not sep:
            continue
        key = key.strip()
        if not key:
            continue
        val = val.strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in ("'", '"'):
            val = val[1:-1]
        if override or key not in os.environ:
            os.environ[key] = val
    return str(p)


def load_agent_config(**overrides) -> AgentConfig:
    """从环境变量装配 AgentConfig，overrides 显式传入的键优先级最高。

    环境变量前缀 AGENT_ 优先、ORCH_ 兜底，保证与既有 worker/runtime 配置并存。
    关键字参数 overrides 用于代码内直接指定（如 worker 传入自己的 work_dir）。
    """
    # 若存在 .env（项目根或 AGENT_ENV_FILE 指定），先并入 os.environ（不覆盖真实环境变量），
    # 使 ANTHROPIC_API_KEY / AGENT_* 等无需每次手动 export。
    load_dotenv()
    mem_dir = _default_memory_dir()
    cfg = AgentConfig(
        model=_env("", "AGENT_MODEL", "ORCH_MODEL") or None,
        max_turns=int(_env("50", "AGENT_MAX_TURNS", "ORCH_MAX_TURNS")),
        max_thinking_tokens=_to_optional_int(
            _env("10000", "AGENT_MAX_THINKING_TOKENS", "ORCH_MAX_THINKING_TOKENS")),
        task_budget_usd=_to_optional_float(
            _env("", "AGENT_TASK_BUDGET_USD", "ORCH_TASK_BUDGET_USD")),
        task_timeout=int(_env("900", "AGENT_TASK_TIMEOUT", "ORCH_TASK_TIMEOUT")),
        # 默认收敛为 default：权限门控生效；bypassPermissions 须显式环境变量指定
        permission_mode=_env("default",
                             "AGENT_PERMISSION_MODE", "ORCH_PERMISSION_MODE"),
        project_root=_env(_default_project_root(),
                          "AGENT_PROJECT_ROOT", "ORCH_PROJECT_ROOT"),
        work_dir=_env(_default_work_dir(), "AGENT_WORK_DIR", "ORCH_WORK_DIR"),
        persona_dir=_env(_default_persona_dir(),
                         "AGENT_PERSONA_DIR", "ORCH_PERSONA_DIR"),
        mcp_config_path=_env("", "AGENT_MCP_CONFIG", "ORCH_MCP_CONFIG"),
        memory_core_dir=_env(str(mem_dir / "core"),
                             "AGENT_MEMORY_CORE_DIR", "ORCH_MEMORY_CORE_DIR"),
        memory_context_dir=_env(str(mem_dir / "context"),
                                "AGENT_MEMORY_CONTEXT_DIR", "ORCH_MEMORY_CONTEXT_DIR"),
        experience_store_dir=_env(_default_experience_store_dir(),
                                  "AGENT_EXPERIENCE_STORE_DIR",
                                  "ORCH_EXPERIENCE_STORE_DIR"),
        # 专属兜底 ORCH_MEMORY_BACKEND 优先；ORCH_MODE（集群/编排模式 local|remote）
        # 仅在旧命名约定下作最后回退——两者语义不同，但远程模式下记忆后端常随之 remote。
        memory_backend=_env("local", "AGENT_MEMORY_BACKEND",
                            "ORCH_MEMORY_BACKEND", "ORCH_MODE"),
        brain_url=_env("http://127.0.0.1:7000",
                       "AGENT_BRAIN_URL", "ORCH_BRAIN_URL"),
        api_key=_env("", "AGENT_API_KEY", "ORCH_API_KEY"),
        planning_enabled=_to_bool(_env("", "AGENT_PLANNING_ENABLED"), False),
        reflection_enabled=_to_bool(_env("", "AGENT_REFLECTION_ENABLED"), False),
        rag_enabled=_to_bool(_env("", "AGENT_RAG_ENABLED"), False),
        streaming=_to_bool(_env("1", "AGENT_STREAMING"), True),
        auto_record_experience=_to_bool(
            _env("1", "AGENT_AUTO_RECORD_EXPERIENCE"), True),
        embedding_model=_env("", "AGENT_EMBEDDING_MODEL",
                             "ORCH_EMBEDDING_MODEL"),
        memory_core_retrieval=_to_bool(
            _env("", "AGENT_MEMORY_CORE_RETRIEVAL"), False),
        plan_resume_max_age_hours=_to_float(
            _env("24", "AGENT_PLAN_RESUME_MAX_AGE_HOURS",
                 "ORCH_PLAN_RESUME_MAX_AGE_HOURS"), 24.0),
        output_guard_enabled=_to_bool(
            _env("1", "AGENT_OUTPUT_GUARD_ENABLED",
                 "ORCH_OUTPUT_GUARD_ENABLED"), True),
        output_guard_max_retries=int(_env(
            "1", "AGENT_OUTPUT_GUARD_MAX_RETRIES",
            "ORCH_OUTPUT_GUARD_MAX_RETRIES")),
        fallback_model=_env("", "AGENT_FALLBACK_MODEL", "ORCH_FALLBACK_MODEL"),
        # 运行时 Provider（全局默认三元组 + 故障切换池）
        provider_base_url=_env("", "AGENT_PROVIDER_BASE_URL",
                               "ORCH_PROVIDER_BASE_URL"),
        provider_auth_token=_env("", "AGENT_PROVIDER_AUTH_TOKEN",
                                 "ORCH_PROVIDER_AUTH_TOKEN"),
        provider_pool=_to_provider_pool(_env(
            "", "AGENT_PROVIDER_POOL", "ORCH_PROVIDER_POOL")),
        # 自纠错闭环
        correction_enabled=_to_bool(_env("1", "AGENT_CORRECTION_ENABLED"), True),
        correction_max_retries=int(_env("2", "AGENT_CORRECTION_MAX_RETRIES")),
        # 上下文预算
        session_summarize_enabled=_to_bool(
            _env("1", "AGENT_SESSION_SUMMARIZE_ENABLED"), True),
        session_summarize_interval=int(
            _env("5", "AGENT_SESSION_SUMMARIZE_INTERVAL")),
        session_max_context_chars=int(
            _env("80000", "AGENT_SESSION_MAX_CONTEXT_CHARS")),
        rag_budget_ratio=_to_float(
            _env("0.15", "AGENT_RAG_BUDGET_RATIO"), 0.15),
        # 工具守卫
        tool_guard_enabled=_to_bool(
            _env("1", "AGENT_TOOL_GUARD_ENABLED"), True),
        tool_guard_timeout=int(_env("30", "AGENT_TOOL_GUARD_TIMEOUT")),
        tool_guard_cache_ttl=int(_env("300", "AGENT_TOOL_GUARD_CACHE_TTL")),
        # 计划验证
        plan_validation_enabled=_to_bool(
            _env("1", "AGENT_PLAN_VALIDATION_ENABLED"), True),
        plan_parallel_steps=_to_bool(
            _env("", "AGENT_PLAN_PARALLEL_STEPS"), False),
        plan_replan_enabled=_to_bool(
            _env("1", "AGENT_PLAN_REPLAN_ENABLED"), True),
        # 安全
        rate_limit_per_minute=int(
            _env("60", "AGENT_RATE_LIMIT_PER_MINUTE")),
        # 自适应路由
        auto_routing=_to_bool(_env("", "AGENT_AUTO_ROUTING"), False),
        # Prompt Injection 检测
        prompt_guard_enabled=_to_bool(
            _env("1", "AGENT_PROMPT_GUARD_ENABLED"), True),
        prompt_guard_action=_env("reject",
                                 "AGENT_PROMPT_GUARD_ACTION").strip().lower(),
        # 工具响应验证
        tool_response_validation_enabled=_to_bool(
            _env("1", "AGENT_TOOL_RESPONSE_VALIDATION_ENABLED"), True),
        # 运行历史
        run_history_dir=_env("", "AGENT_RUN_HISTORY_DIR"),
        run_history_max_records=int(_env("1000", "AGENT_RUN_HISTORY_MAX_RECORDS")),
        # 持久化执行日志（Slice 3 rollout：默认关，须显式开启）
        durable_journal_enabled=_to_bool(
            _env("", "AGENT_DURABLE_JOURNAL_ENABLED",
                 "ORCH_DURABLE_JOURNAL_ENABLED"), False),
        journal_path=_env("", "AGENT_JOURNAL_PATH", "ORCH_JOURNAL_PATH"),
        max_active_executions=int(_env(
            "1", "AGENT_MAX_ACTIVE_EXECUTIONS", "ORCH_MAX_ACTIVE_EXECUTIONS")),
        # 子代理派发开关 + AI 推荐/解释顾问（默认值保持既有行为）
        subagents_enabled=_to_bool(
            _env("1", "AGENT_SUBAGENTS_ENABLED", "ORCH_SUBAGENTS_ENABLED"), True),
        advisor_enabled=_to_bool(
            _env("1", "AGENT_ADVISOR_ENABLED", "ORCH_ADVISOR_ENABLED"), True),
        advisor_auto_apply=_to_bool(
            _env("", "AGENT_ADVISOR_AUTO_APPLY", "ORCH_ADVISOR_AUTO_APPLY"), False),
        advisor_model=_env("", "AGENT_ADVISOR_MODEL", "ORCH_ADVISOR_MODEL"),
        advisor_work_dir=_env("", "AGENT_ADVISOR_WORK_DIR", "ORCH_ADVISOR_WORK_DIR"),
        advisor_use_plan_engine=_to_bool(
            _env("", "AGENT_ADVISOR_USE_PLAN_ENGINE",
                 "ORCH_ADVISOR_USE_PLAN_ENGINE"), False),
        # 结果翻译（默认关，须显式开启；目标语言默认中文）
        result_translate_enabled=_to_bool(
            _env("", "AGENT_RESULT_TRANSLATE_ENABLED",
                 "ORCH_RESULT_TRANSLATE_ENABLED"), False),
        translate_model=_env("", "AGENT_TRANSLATE_MODEL", "ORCH_TRANSLATE_MODEL"),
        translate_target_lang=_env(
            "zh", "AGENT_TRANSLATE_TARGET_LANG", "ORCH_TRANSLATE_TARGET_LANG"),
        translate_work_dir=_env(
            "", "AGENT_TRANSLATE_WORK_DIR", "ORCH_TRANSLATE_WORK_DIR"),
        understanding_enabled=_to_bool(_env("1", "AGENT_UNDERSTANDING_ENABLED"), True),
        understanding_max_cost_usd=float(_env("0.1", "AGENT_UNDERSTANDING_MAX_COST_USD")),
        understanding_budget_usd=float(_env("0", "AGENT_UNDERSTANDING_BUDGET_USD")) or None,
    )
    # 显式 overrides 覆盖环境变量结果（None 值也尊重调用方意图，直接赋值）
    for key, value in overrides.items():
        if hasattr(cfg, key):
            setattr(cfg, key, value)
    return cfg
