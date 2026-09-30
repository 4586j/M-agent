"""大脑侧运行时配置。mode=local 保留 V1 单机行为，mode=remote 启用跨主机。"""
import os
from dataclasses import dataclass
from pathlib import Path

VALID_MODES = ("local", "remote")


def _default_task_store_dir() -> str:
    """基于本文件位置定位 .tasks，避免相对路径随 cwd 漂移到嵌套目录。"""
    return str(Path(__file__).resolve().parent / ".tasks")


def _default_experience_store_dir() -> str:
    """基于本文件位置定位 .experiences，避免相对路径随 cwd 漂移到嵌套目录。"""
    return str(Path(__file__).resolve().parent / ".experiences")


def _default_schedule_store_dir() -> str:
    """基于本文件位置定位 .schedules，避免相对路径随 cwd 漂移到嵌套目录。"""
    return str(Path(__file__).resolve().parent / ".schedules")


def _default_memory_dir() -> Path:
    """定位框架约定的记忆目录（skills/orchestrator/memory/），与 V1 主会话共用一套。"""
    return Path(__file__).resolve().parents[2] / "skills" / "orchestrator" / "memory"


def _default_runtime_dir() -> Path:
    return Path(__file__).resolve().parent


@dataclass
class Config:
    mode: str = "local"
    brain_url: str = "http://127.0.0.1:7000"
    heartbeat_interval: int = 15
    heartbeat_timeout: int = 45
    task_store_dir: str = _default_task_store_dir()
    experience_store_dir: str = _default_experience_store_dir()
    schedule_store_dir: str = _default_schedule_store_dir()
    procedural_store_dir: str = str(_default_runtime_dir() / ".procedural")
    memory_core_dir: str = str(_default_memory_dir() / "core")
    memory_context_dir: str = str(_default_memory_dir() / "context")
    experience_max_count: int = 500  # 经验库容量上限，超出按失败优先/命中少/最旧淘汰
    experience_ttl_days: int = 0  # 经验保留天数，0=不过期
    context_memory_max_chars: int = 8000  # 派发注入 context_memory 的限幅，防上下文膨胀
    # security-guard LLM 增强，阶段一起默认开启（需 brain 侧 claude-agent-sdk；
    # 缺失时自动降级到规则引擎）；ORCH_SECURITY_LLM=0 显式关闭
    security_llm: bool = True
    security_check_max_chars: int = 6000  # 产物检验合并输入限幅（LLM/规则输入封顶）
    model_routing: str = "off"  # off | heuristic：简单 executor 任务路由到便宜模型
    cheap_model: str = "haiku"  # 启发式命中的目标模型
    route_max_prompt_chars: int = 400  # 启发式：prompt 不超过该长度视为简单任务
    max_retries: int = 3
    retry_base_delay: float = 1.0
    sweeper_interval: int = 15
    async_poll_interval: float = 1.0
    async_poll_max: int = 600
    api_key: str = ""
    # provider 池（runtime/providers.json，gitignore）+ 默认 provider 热切起点
    providers_file: str = str(_default_runtime_dir() / "providers.json")
    default_provider: str = ""  # ORCH_DEFAULT_PROVIDER：起始默认，面板/API 可热切
    # 全局/每日预算闸门（runtime/budgets.json 账本；0=不限制，PUT /budgets 可热调）
    budgets_file: str = str(_default_runtime_dir() / "budgets.json")
    budget_global_usd: float = 0.0
    budget_daily_usd: float = 0.0
    # 持久会话（派发链路连续对话；每会话一个 JSON 文件）
    sessions_dir: str = str(_default_runtime_dir() / ".sessions")
    # 关键事件外发（webhook）：空 URL=关闭；事件名 CSV 过滤（空=默认全集）
    webhook_url: str = ""
    webhook_secret: str = ""
    webhook_events: str = ""   # 如 "task_terminal,awaiting_approval,budget_exceeded"
    webhook_timeout: float = 5.0
    webhook_max_retries: int = 3
    # worker 连续失败熔断（派发层跳过熔断中的节点，恢复期后半开试探）
    circuit_breaker_enabled: bool = True
    circuit_breaker_failure_threshold: int = 5
    circuit_breaker_recovery_timeout: float = 60.0


def load_config() -> Config:
    mode = os.environ.get("ORCH_MODE", "local")
    if mode not in VALID_MODES:
        mode = "local"
    return Config(
        mode=mode,
        brain_url=os.environ.get("ORCH_BRAIN_URL", "http://127.0.0.1:7000"),
        heartbeat_interval=int(os.environ.get("ORCH_HEARTBEAT_INTERVAL", "15")),
        heartbeat_timeout=int(os.environ.get("ORCH_HEARTBEAT_TIMEOUT", "45")),
        task_store_dir=os.environ.get("ORCH_TASK_STORE_DIR", _default_task_store_dir()),
        experience_store_dir=os.environ.get("ORCH_EXPERIENCE_STORE_DIR", _default_experience_store_dir()),
        schedule_store_dir=os.environ.get("ORCH_SCHEDULE_STORE_DIR", _default_schedule_store_dir()),
        procedural_store_dir=os.environ.get("ORCH_PROCEDURAL_STORE_DIR", str(_default_runtime_dir() / ".procedural")),
        memory_core_dir=os.environ.get("ORCH_MEMORY_CORE_DIR", str(_default_memory_dir() / "core")),
        memory_context_dir=os.environ.get("ORCH_MEMORY_CONTEXT_DIR", str(_default_memory_dir() / "context")),
        experience_max_count=int(os.environ.get("ORCH_EXPERIENCE_MAX", "500")),
        experience_ttl_days=int(os.environ.get("ORCH_EXPERIENCE_TTL_DAYS", "0")),
        context_memory_max_chars=int(os.environ.get("ORCH_CONTEXT_MEMORY_MAX", "8000")),
        # 默认开启，ORCH_SECURITY_LLM=0/false 显式关闭（规则引擎兜底，LLM 缺失自动降级）
        security_llm=os.environ.get("ORCH_SECURITY_LLM", "1") not in ("0", "false", "False", "no", "off"),
        security_check_max_chars=int(os.environ.get("ORCH_SECURITY_CHECK_MAX_CHARS", "6000")),
        model_routing=os.environ.get("ORCH_MODEL_ROUTING", "off"),
        cheap_model=os.environ.get("ORCH_CHEAP_MODEL", "haiku"),
        route_max_prompt_chars=int(os.environ.get("ORCH_ROUTE_MAX_PROMPT_CHARS", "400")),
        max_retries=int(os.environ.get("ORCH_MAX_RETRIES", "3")),
        retry_base_delay=float(os.environ.get("ORCH_RETRY_BASE_DELAY", "1.0")),
        sweeper_interval=int(os.environ.get("ORCH_SWEEPER_INTERVAL", "15")),
        async_poll_interval=float(os.environ.get("ORCH_ASYNC_POLL_INTERVAL", "1.0")),
        async_poll_max=int(os.environ.get("ORCH_ASYNC_POLL_MAX", "600")),
        api_key=os.environ.get("ORCH_API_KEY", ""),
        providers_file=os.environ.get(
            "ORCH_PROVIDERS_FILE",
            str(_default_runtime_dir() / "providers.json")),
        default_provider=os.environ.get("ORCH_DEFAULT_PROVIDER", ""),
        budgets_file=os.environ.get(
            "ORCH_BUDGETS_FILE",
            str(_default_runtime_dir() / "budgets.json")),
        budget_global_usd=float(os.environ.get("ORCH_BUDGET_GLOBAL_USD", "0") or 0),
        budget_daily_usd=float(os.environ.get("ORCH_BUDGET_DAILY_USD", "0") or 0),
        sessions_dir=os.environ.get(
            "ORCH_SESSIONS_DIR",
            str(_default_runtime_dir() / ".sessions")),
        webhook_url=os.environ.get("ORCH_WEBHOOK_URL", ""),
        webhook_secret=os.environ.get("ORCH_WEBHOOK_SECRET", ""),
        webhook_events=os.environ.get("ORCH_WEBHOOK_EVENTS", ""),
        webhook_timeout=float(os.environ.get("ORCH_WEBHOOK_TIMEOUT", "5.0")),
        webhook_max_retries=int(os.environ.get("ORCH_WEBHOOK_MAX_RETRIES", "3")),
        circuit_breaker_enabled=os.environ.get("ORCH_CIRCUIT_BREAKER_ENABLED", "1")
        not in ("0", "false", "False", "no", "off"),
        circuit_breaker_failure_threshold=int(
            os.environ.get("ORCH_CIRCUIT_BREAKER_THRESHOLD", "5")),
        circuit_breaker_recovery_timeout=float(
            os.environ.get("ORCH_CIRCUIT_BREAKER_RECOVERY", "60.0")),
    )
