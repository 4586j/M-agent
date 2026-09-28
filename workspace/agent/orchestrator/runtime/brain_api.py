"""大脑 HTTP 服务：接收 worker 注册/心跳，查询任务与 worker，并代理派发。

路由组按资源拆分在 :mod:`brain_routers`（router 工厂 + 共享 BrainDeps 装配）；
本模块只保留应用装配（middleware / 静态托管 / include_router）与 HTTP 客户端
基座，并把 brain_routers 的请求模型/helper 原样再导出（`from brain_api import
DispatchReq` 兼容旧引用）。

阶段三可观测性：GET /tasks/{id}/events 提供 SSE 实时事件流（status/callback/done，
鉴权同时接受 X-API-Key 头与 ?api_key= 查询参数，适配 EventSource 无法带 header）;
GET /stats 附带 cost 成本聚合（按日近 7 天 / 按 role / 按 model，源自 TaskRecord.meta）
与 quality 输出质量聚合（按 model 统计退化任务数与比率）。
"""
import os
import sys
from pathlib import Path

import httpx
import requests

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware

from task_store import TaskStore
from worker_registry import WorkerRegistry
from dispatcher import Dispatcher, RemoteDispatcher
from async_dispatcher import AsyncRemoteDispatcher
from config import Config
from security_guard import check_with_llm as security_check
from experience_store import ExperienceStore
from memory_store import FileMemoryStore, MemoryStore
from procedural_store import ProceduralStore
from retry_manager import RetryManager
from providers import ProviderStore
from embedding_model import create_embedding_model
from schedule_store import ScheduleStore
from task_scheduler import TaskScheduler
from auth import add_auth_middleware
from event_bus import TaskEventBus

from brain_routers import (
    BrainDeps,
    RegisterReq,
    HeartbeatReq,
    NodeWorkspaceProxyReq,
    DispatchReq,
    AdvisorReq,
    TranslateReq,
    MemoryWriteReq,
    ProceduralProposalReq,
    ProceduralVoteReq,
    RedispatchReq,
    ApproveReq,
    RejectReq,
    ExperienceSearchReq,
    ScheduleReq,
    create_auth_router,
    create_providers_router,
    create_tasks_router,
    create_approval_router,
    create_dispatch_router,
    create_secretary_router,
    create_sse_router,
    create_stats_router,
    create_memory_router,
    create_procedural_router,
    create_experiences_router,
    create_schedules_router,
    create_proxy_router,
)


def _default_web_dist() -> str:
    """从本文件向上逐级查找 web/dist，兼容不同启动目录与目录层级。"""
    p = Path(__file__).resolve().parent
    for _ in range(8):
        cand = p / "web" / "dist"
        if cand.exists():
            return str(cand)
        if p.parent == p:
            break
        p = p.parent
    return "web/dist"


def _make_provider_resolver(provider_store: ProviderStore,
                            default_provider: str):
    """构造 provider 名解析器：显式名或默认名 → 解析后的三元组。

    显式名/默认名不可用（不存在或 ${ENV} 不可解析）→ 抛 ValueError（→400）；
    两者都空 → None（worker 走自身配置/进程 env）。
    """
    def resolve_name(name: str | None) -> dict | None:
        target = (name or "").strip() or default_provider
        if not target:
            return None
        resolved = provider_store.resolve(target)
        if resolved is None:
            avail = ", ".join(provider_store.names()) or "无"
            raise ValueError(
                f"provider '{target}' 不可用（池中: {avail}）——检查 "
                "providers.json 条目与其引用的环境变量")
        return resolved
    return resolve_name


def create_brain_app(registry: WorkerRegistry, task_store: TaskStore, cfg: Config,
                     dispatcher: Dispatcher | None = None,
                     experience_store: ExperienceStore | None = None,
                     memory_store: MemoryStore | None = None,
                     procedural_store: ProceduralStore | None = None,
                     retry_manager: RetryManager | None = None,
                     schedule_store: ScheduleStore | None = None,
                     task_scheduler: TaskScheduler | None = None,
                     async_dispatcher: AsyncRemoteDispatcher | None = None,
                     event_bus: TaskEventBus | None = None,
                     http_call=None,
                     advisor=None,
                     translator=None,
                     provider_store: ProviderStore | None = None) -> FastAPI:
    """dispatcher 可选：remote 模式传入 RemoteDispatcher，使派发与注册状态同进程。

    所有路由端点按资源拆在 brain_routers 的 router 工厂里；本函数只负责
    装配 BrainDeps（store/派发器/事件总线/Provider 解析器）并 include_router。
    event_bus 可选：SSE 实时流的事件总线；未传入时自建。advisor 可选：
    AdvisorService（独立上下文推荐/解释）；translator 可选：TranslatorService。
    """
    app = FastAPI()
    bus = event_bus or TaskEventBus()
    if http_call is None:
        http_call = _make_http_call(cfg.api_key)

    @app.exception_handler(ValueError)
    def _on_value_error(request, exc):
        from fastapi.responses import JSONResponse
        return JSONResponse(status_code=400, content={"detail": str(exc)})

    # CORS：开发期前端 5173，生产同源；可用 ORCH_CORS_ORIGINS 追加
    origins = os.environ.get("ORCH_CORS_ORIGINS", "http://localhost:5173")
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[o.strip() for o in origins.split(",") if o.strip()],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    add_auth_middleware(app, cfg.api_key)

    # 审计流水只读 API（独立 router：GET /audit，受同一鉴权中间件保护）
    from audit_api import create_audit_router
    app.include_router(create_audit_router())

    provider_store = provider_store if provider_store is not None else ProviderStore(
        cfg.providers_file)
    resolve_provider_name = _make_provider_resolver(provider_store, cfg.default_provider)

    deps = BrainDeps(
        cfg=cfg, registry=registry, task_store=task_store, bus=bus,
        http_call=http_call,
        dispatcher=dispatcher, async_dispatcher=async_dispatcher,
        experience_store=experience_store, memory_store=memory_store,
        procedural_store=procedural_store, retry_manager=retry_manager,
        schedule_store=schedule_store, task_scheduler=task_scheduler,
        advisor=advisor, translator=translator, provider_store=provider_store,
        resolve_provider_name=resolve_provider_name,
    )
    for make_router in (
        create_auth_router, create_providers_router, create_tasks_router,
        create_approval_router, create_dispatch_router, create_secretary_router,
        create_sse_router, create_stats_router, create_memory_router,
        create_procedural_router, create_experiences_router,
        create_schedules_router, create_proxy_router,
    ):
        app.include_router(make_router(deps))

    # 静态托管前端构建产物（生产同源）；目录不存在则跳过，仅提供 API
    web_dist = os.environ.get("ORCH_WEB_DIST") or _default_web_dist()
    if Path(web_dist).exists():
        app.mount("/", StaticFiles(directory=web_dist, html=True), name="web")

    return app


def _make_http_call(api_key: str = ""):
    """创建带 API Key 的同步 HTTP 客户端。"""

    def _http_call(method: str, url: str, **kwargs) -> dict:
        headers = kwargs.pop("headers", {})
        if api_key:
            headers["X-API-Key"] = api_key
        resp = requests.request(method, url, timeout=30, headers=headers, **kwargs)
        try:
            body = resp.json()
        except ValueError:
            body = {"raw": resp.text}
        return {"status_code": resp.status_code, "json": body}

    return _http_call


# 应用级 httpx.AsyncClient 单例：复用连接池，避免每次异步调用都新建/销毁客户端
_async_client: "httpx.AsyncClient | None" = None


def _get_async_client(api_key: str = "") -> "httpx.AsyncClient":
    """应用级单例 httpx.AsyncClient，复用连接池（keep-alive + 连接上限）。"""
    global _async_client
    if _async_client is None or _async_client.is_closed:
        _async_client = httpx.AsyncClient(
            timeout=30,
            limits=httpx.Limits(max_connections=100, max_keepalive_connections=20),
            headers={"X-API-Key": api_key} if api_key else {},
        )
    return _async_client


def _make_async_http_call(api_key: str = ""):
    """创建带 API Key 的异步 HTTP 客户端（连接池复用，不再每次新建客户端）。"""
    async def _async_http_call(method: str, url: str, **kwargs) -> dict:
        headers = kwargs.pop("headers", {})
        if api_key:
            headers["X-API-Key"] = api_key
        client = _get_async_client(api_key)
        resp = await client.request(method, url, headers=headers, **kwargs)
        try:
            body = resp.json()
        except ValueError:
            body = {"raw": resp.text}
        return {"status_code": resp.status_code, "json": body}

    return _async_http_call


def _build_local_core_config(cfg):
    """单机本地执行的 AgentKernel 配置（与 CLI/worker 对等）。

    配置对等（P0）：走与 CLI/worker 相同的 load_agent_config 装配，使
    model / budget / planning / reflection / rag / 各类守卫开关在本地执行
    路径同样生效；permission_mode / max_turns / task_timeout / mcp_config_path
    由 load_agent_config 读 ORCH_* 环境变量兜底，语义不变。此前手搭
    AgentConfig 遗漏大量字段，导致 brain 本地执行与 CLI 行为不一致。
    仅覆盖 brain 已解析的目录/根路径，保证与调度器其他组件共用同一存储位置。
    """
    from agent_core.config import load_agent_config
    return load_agent_config(
        project_root=str(Path(__file__).resolve().parents[4]),
        memory_core_dir=cfg.memory_core_dir,
        memory_context_dir=cfg.memory_context_dir,
        experience_store_dir=cfg.experience_store_dir,
    )


def _build_app() -> FastAPI:
    """从 config 装配 brain_app。

    remote 模式注入 RemoteDispatcher；local 模式默认不注入（行为不变），
    仅当 ORCH_LOCAL_EXEC=1 时注入 LocalExecutorDispatcher（单机 brain 进程内
    直接跑 AgentKernel，使 /dispatch 可真正执行）。
    """
    _project_root = str(Path(__file__).resolve().parents[4])
    if _project_root not in sys.path:
        sys.path.insert(0, _project_root)
    from config import load_config
    cfg = load_config()
    registry = WorkerRegistry(heartbeat_timeout=cfg.heartbeat_timeout)
    store = TaskStore(cfg.task_store_dir)
    # Provider 池（providers.json，gitignore）：重试/定时/派发共用同一 resolver
    provider_store = ProviderStore(cfg.providers_file)
    provider_resolver = _make_provider_resolver(provider_store, cfg.default_provider)
    emb_model = create_embedding_model()
    exp_store = ExperienceStore(cfg.experience_store_dir, embedding_model=emb_model,
                                max_count=cfg.experience_max_count,
                                ttl_days=cfg.experience_ttl_days)
    mem_store = FileMemoryStore(cfg.memory_core_dir, cfg.memory_context_dir)
    # proc_root = workspace/agent（runtime 上两级）；仓库根再上一级
    proc_root = Path(__file__).resolve().parents[2]
    project_root = proc_root.parents[1]
    proc_store = ProceduralStore(
        store_dir=cfg.procedural_store_dir,
        allowed_roots=[str(project_root / "workspace" / "agent" / "skills"),
                       str(project_root / ".claude" / "agents")],
        project_root=str(project_root),
        security_check=(lambda c: security_check(c, use_llm=cfg.security_llm)),
    )
    dispatcher = None
    retry_mgr = None
    async_disp = None
    sched_store = ScheduleStore(cfg.schedule_store_dir)
    scheduler = None
    # 全链路共享事件总线：dispatcher/async_dispatcher/retry/brain 同一实例，
    # 保证 SSE 订阅能收到派发、回调与终态事件
    bus = TaskEventBus()
    if cfg.mode == "remote":
        strategy = os.environ.get("ORCH_SELECT_STRATEGY", "round_robin")
        product_check = (lambda c: security_check(c, use_llm=cfg.security_llm))
        http_call = _make_http_call(cfg.api_key)
        async_http_call = _make_async_http_call(cfg.api_key)
        dispatcher = RemoteDispatcher(task_store=store, registry=registry,
                                      http_call=http_call, select_strategy=strategy,
                                      product_check=product_check,
                                      experience_store=exp_store,
                                      memory_store=mem_store,
                                      max_retries=cfg.max_retries,
                                      check_max_chars=cfg.security_check_max_chars,
                                      event_bus=bus)
        async_disp = AsyncRemoteDispatcher(
            task_store=store, registry=registry,
            async_http=async_http_call, select_strategy=strategy,
            product_check=product_check, experience_store=exp_store,
            memory_store=mem_store,
            max_retries=cfg.max_retries,
            check_max_chars=cfg.security_check_max_chars,
            poll_interval=cfg.async_poll_interval,
            poll_max=cfg.async_poll_max,
            brain_url=cfg.brain_url,
            event_bus=bus,
        )
        retry_mgr = RetryManager(
            task_store=store, registry=registry, dispatcher=dispatcher,
            sweeper_interval=cfg.sweeper_interval,
            retry_base_delay=cfg.retry_base_delay,
            event_bus=bus,
            provider_resolver=provider_resolver,
        )
        retry_mgr.start()
        scheduler = TaskScheduler(schedule_store=sched_store, dispatcher=dispatcher,
                                  provider_resolver=provider_resolver)
        scheduler.start()
    elif cfg.mode == "local" and os.environ.get("ORCH_LOCAL_EXEC", "0") in ("1", "true", "True"):
        # 单机本地执行：brain 进程内直接运行 AgentKernel（默认不启用，行为不变）
        from local_executor_dispatcher import LocalExecutorDispatcher

        # 配置对等（P0）：统一走 _build_local_core_config（内部 load_agent_config），
        # 使 brain 本地执行与 CLI/worker 共享同一套开关与守卫，不再手搭遗漏字段。
        core_cfg = _build_local_core_config(cfg)
        product_check = (lambda c: security_check(c, use_llm=cfg.security_llm))
        dispatcher = LocalExecutorDispatcher(
            task_store=store,
            config=core_cfg,
            experience_store=exp_store,
            memory_store=mem_store,
            product_check=product_check,
            event_bus=bus,
        )
    # AI 推荐/解释顾问：自建独立上下文，local/remote 两种模式均可用（与派发器无耦合）。
    # 构建失败不拖垮 brain 启动（降级为 /recommend 返回未配置）。
    advisor = None
    try:
        from agent_core.advisor import AdvisorService
        advisor = AdvisorService(_build_local_core_config(cfg))
    except Exception:  # noqa: BLE001 - 顾问不可用时 brain 仍正常启动
        advisor = None
    # 结果翻译：同样自建独立上下文，local/remote 两种模式均可用（与派发器无耦合）。
    # 构建失败不拖垮 brain 启动（降级为 /translate 返回未配置）。
    translator = None
    try:
        from agent_core.translator import TranslatorService
        translator = TranslatorService(_build_local_core_config(cfg))
    except Exception:  # noqa: BLE001 - 翻译不可用时 brain 仍正常启动
        translator = None
    return create_brain_app(registry, store, cfg, dispatcher=dispatcher,
                            experience_store=exp_store, memory_store=mem_store,
                            procedural_store=proc_store,
                            retry_manager=retry_mgr,
                            schedule_store=sched_store, task_scheduler=scheduler,
                            async_dispatcher=async_disp, event_bus=bus,
                            advisor=advisor, translator=translator,
                            provider_store=provider_store)


if __name__ == "__main__":
    import uvicorn
    from config import load_config

    cfg = load_config()
    # 监听地址：默认 127.0.0.1（单机）；跨主机时设 ORCH_BRAIN_HOST=0.0.0.0 对外开放
    host = os.environ.get("ORCH_BRAIN_HOST", "127.0.0.1")
    port = int(cfg.brain_url.rsplit(":", 1)[-1]) if ":" in cfg.brain_url else 7000
    uvicorn.run(_build_app, host=host, port=port)