"""大脑 HTTP 服务：接收 worker 注册/心跳，查询任务与 worker，并代理派发。

阶段三可观测性：GET /tasks/{id}/events 提供 SSE 实时事件流（status/callback/done，
鉴权同时接受 X-API-Key 头与 ?api_key= 查询参数，适配 EventSource 无法带 header）;
GET /stats 附带 cost 成本聚合（按日近 7 天 / 按 role / 按 model，源自 TaskRecord.meta）
与 quality 输出质量聚合（按 model 统计退化任务数与比率）。
"""
import asyncio
import os
import time
import uuid
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from task_store import TaskStore, TaskRecord, new_trace_id
from worker_registry import WorkerRegistry, WorkerInfo
from dispatcher import Dispatcher, RemoteDispatcher, DispatchResult
from async_dispatcher import AsyncRemoteDispatcher
from config import Config
from security_guard import check_with_llm as security_check
from experience_store import ExperienceStore
from memory_store import FileMemoryStore, MemoryStore
from procedural_store import ProceduralStore
from retry_manager import RetryManager
from providers import ProviderStore
from embedding_model import create_embedding_model
from schedule_store import ScheduleStore, ScheduleRecord, compute_next_run
from task_scheduler import TaskScheduler
from auth import add_auth_middleware
from safe_id import validate_id, validate_host
from dispatch_common import clamp_context, assemble_context
from audit_log import record_security_event
from event_bus import TaskEvent, TaskEventBus, TERMINAL_STATUSES, publish_task_event


class RegisterReq(BaseModel):
    worker_id: str
    role: str
    host: str
    port: int
    capabilities: dict = {}


class HeartbeatReq(BaseModel):
    worker_id: str
    load: int = 0


class NodeWorkspaceProxyReq(BaseModel):
    """PUT /proxy/node/workspace：代理设置节点对话工作区。"""
    worker_id: str
    path: str


class DispatchReq(BaseModel):
    prompt: str
    role: str = "executor"
    context_memory: str | None = None
    model: str | None = None
    force: bool = False  # 跳过 security-guard 检验，用于放行 block 指令
    context_task_ids: list[str] = []  # 引用历史任务的上下文记忆笔记，并入本次派发
    # 评测消融硬关：默认 True（行为不变）；置 False 时派发层不检索/不注入
    # 历史经验（读关）且不沉淀经验（写关），供「无经验」对照组 A/B 评测。
    use_experience: bool = True
    # 子代理派发每次请求覆盖：None=跟随全局 AGENT_/ORCH_SUBAGENTS_ENABLED；
    # True/False=本次派发强制启用/禁用子代理（单机本地执行生效；远程受全局闸）。
    enable_subagents: bool | None = None
    # 定点派发：非空时按 worker_id 指定节点（定向指挥单机多 agent 的某个实例）。
    # 节点不存在/心跳超时直接 failed 返回，不静默换节点；格式非法归 400。
    worker_id: str | None = None
    # 运行时 Provider：只传 provider 名（密钥不入请求体/审计/审批缓存）。
    # 未指定 → 用 brain 当前默认 provider；都没有 → worker 走自身配置/进程 env。
    provider: str | None = None


class AdvisorReq(BaseModel):
    """AI 推荐/解释顾问请求。顾问运行于独立上下文，绝不污染任务上下文。"""
    task: str
    mode: str = "all"  # pre_flight | plan | explain | all
    finished_output: str | None = None  # explain 模式附上已完成产出
    enable_subagents: bool | None = None  # auto-apply 派发时透传的每次请求覆盖
    auto_apply: bool | None = None  # None=跟随全局 advisor_auto_apply


class TranslateReq(BaseModel):
    """结果翻译请求。翻译运行于独立上下文，绝不污染任务上下文。"""
    text: str
    target_lang: str | None = None  # None=跟随全局 translate_target_lang（默认 zh）


class MemoryWriteReq(BaseModel):
    content: str


class ProceduralProposalReq(BaseModel):
    target_path: str  # 相对项目根，限 skills/ 与 .claude/agents/ 白名单下既有文件
    content: str  # 提议的新内容
    reason: str
    evidence_exp_ids: list[str] = []


class ProceduralVoteReq(BaseModel):
    voter: str  # brain | executor | security（security 由规则引擎自动投）
    verdict: str  # pass | block
    reason: str = ""


class RedispatchReq(BaseModel):
    model: str | None = None
    # 定点重派：非空时强制指到该节点；缺省后端按原任务 meta.pinned 继承。
    worker_id: str | None = None
    # provider 覆盖：非空用指名 provider；缺省继承原任务 meta.provider（同名重新解析）。
    provider: str | None = None


class ApproveReq(BaseModel):
    # 修订后的 prompt；缺省=按原拦截指令人工放行（不再重检）
    prompt: str | None = None


class RejectReq(BaseModel):
    reason: str = ""


class ExperienceSearchReq(BaseModel):
    query: str
    role: str | None = None
    successful_only: bool = True
    limit: int = 5


class ScheduleReq(BaseModel):
    name: str
    cron_expr: str
    role: str = "executor"
    prompt: str
    context_memory: str | None = None
    model: str | None = None
    provider: str | None = None  # 定时任务使用的 provider 名（触发时按当前环境再解析）


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


def _request_operator(request: Request) -> str | None:
    """尽力还原请求来源（审计 operator 字段）：优先 X-Operator 头，其次客户端 host。"""
    op = request.headers.get("X-Operator")
    if op:
        return op
    client = request.client
    return client.host if client else None


_USAGE_KEYS = ("input_tokens", "output_tokens",
               "cache_read_input_tokens", "cache_creation_input_tokens")
_COST_DAY_WINDOW = 7 * 86400  # 按日聚合窗口：近 7 天


def _usage_tokens(usage) -> int | None:
    """usage dict -> 总 token 数；无有效数值时返回 None（口径与 eval/run_eval 一致）。"""
    if not isinstance(usage, dict):
        return None
    nums = [usage[k] for k in _USAGE_KEYS if isinstance(usage.get(k), (int, float))]
    return int(sum(nums)) if nums else None


def _aggregate_costs(tasks: list) -> dict:
    """从 TaskRecord.meta 聚合 token 与成本：按日（近 7 天）/ 按 role / 按 model。

    数据源为 dispatcher 终态化时入库的 worker callback meta（阶段三新增）；
    无 meta 的任务仍计入 tasks 计数，tokens/cost_usd 缺失时置 null。
    """
    now = time.time()

    def _new_bucket() -> dict:
        return {"tasks": 0, "tokens": 0, "cost_usd": 0.0,
                "_has_tokens": False, "_has_cost": False}

    def _acc(b: dict, tokens: int | None, cost: float | None) -> None:
        b["tasks"] += 1
        if tokens is not None:
            b["tokens"] += tokens
            b["_has_tokens"] = True
        if cost is not None:
            b["cost_usd"] += cost
            b["_has_cost"] = True

    by_day: dict[str, dict] = {}
    by_role: dict[str, dict] = {}
    by_model: dict[str, dict] = {}
    total = _new_bucket()
    for t in tasks:
        meta = t.meta if isinstance(t.meta, dict) else {}
        tokens = _usage_tokens(meta.get("usage"))
        cost = meta.get("cost_usd")
        cost = cost if isinstance(cost, (int, float)) else None
        model = meta.get("model")
        _acc(total, tokens, cost)
        ts = None
        try:
            ts = float(t.updated_at)
        except (TypeError, ValueError):
            ts = None
        if ts is not None and now - ts <= _COST_DAY_WINDOW:
            day = datetime.fromtimestamp(ts).strftime("%Y-%m-%d")
            _acc(by_day.setdefault(day, _new_bucket()), tokens, cost)
        _acc(by_role.setdefault(t.role, _new_bucket()), tokens, cost)
        if model:
            _acc(by_model.setdefault(str(model), _new_bucket()), tokens, cost)

    def _clean(b: dict) -> dict:
        return {"tasks": b["tasks"],
                "tokens": b["tokens"] if b["_has_tokens"] else None,
                "cost_usd": round(b["cost_usd"], 6) if b["_has_cost"] else None}

    return {
        "total": _clean(total),
        "by_day": [{"date": d, **_clean(by_day[d])}
                   for d in sorted(by_day)],
        "by_role": {k: _clean(v) for k, v in sorted(by_role.items())},
        "by_model": {k: _clean(v) for k, v in sorted(by_model.items())},
    }


def _aggregate_quality(tasks: list) -> dict:
    """从 TaskRecord.meta 聚合输出质量：按 model 统计任务数与退化率。

    degenerate 标记由内核兜底流程写入 KernelResult.meta，经 worker 侧
    agent_runner / brain 本地 dispatcher 的 _kernel_meta 并入 TaskRecord.meta；
    meta 缺失视为非退化。model 缺失的任务归入 "" 键（缺失为空）。
    口径：发生过退化检出（detected=True）即计入，含重试恢复与熔断。
    """
    by_model: dict[str, dict] = {}
    for t in tasks:
        meta = t.meta if isinstance(t.meta, dict) else {}
        model = str(meta.get("model") or "")
        degen = meta.get("degenerate")
        detected = bool(isinstance(degen, dict) and degen.get("detected"))
        b = by_model.setdefault(model, {"tasks": 0, "degenerate": 0})
        b["tasks"] += 1
        if detected:
            b["degenerate"] += 1
    return {"by_model": {
        k: {"tasks": v["tasks"], "degenerate": v["degenerate"],
            "rate": round(v["degenerate"] / v["tasks"], 4) if v["tasks"] else 0.0}
        for k, v in sorted(by_model.items())
    }}


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

    event_bus 可选：SSE 实时流的事件总线；未传入时自建（派发器未接总线时
    仅 /tasks/{id}/events 的快照+取消/lost 终态可发布，回调解路径共享实例才有事件）。
    advisor 可选：AdvisorService（独立上下文推荐/解释）；未传入时 /recommend 返回未配置。
    translator 可选：TranslatorService（独立上下文结果翻译）；未传入时 /translate 返回未配置。
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

    def _route_model(req: DispatchReq) -> str | None:
        """启发式模型路由（ORCH_MODEL_ROUTING=heuristic 时启用，默认 off）。

        简单 executor 任务（无技能标记、prompt 不超阈值）路由到便宜模型
        （业界共识：轻量子任务用 Haiku 是最大降本杠杆之一）。
        返回最终使用的 model；None = executor.md frontmatter 的默认模型。
        """
        if cfg.model_routing != "heuristic" or req.model or req.role != "executor":
            return req.model
        if any(tag in req.prompt for tag in
               ("[web-expert]", "[back-expert]", "[data-expert]")):
            return None  # 技能流程任务有质量约定，不降级
        if len(req.prompt) <= cfg.route_max_prompt_chars:
            return cfg.cheap_model
        return None

    provider_store = provider_store if provider_store is not None else ProviderStore(
        cfg.providers_file)
    resolve_provider_name = _make_provider_resolver(provider_store, cfg.default_provider)

    def _resolve_provider(model: str | None,
                          provider_name: str | None) -> tuple[str | None, dict | None]:
        """把 provider 名解析为三元组 + 返回最终 model。

        优先级：显式 provider 名 > brain 默认 provider（热切）> 无（worker 自身配置）。
        名不存在/环境变量不可解析 → ValueError（全局 handler → 400）。
        model 优先 req.model / 启发式路由，其次 provider.model（网关默认模型）。
        """
        resolved = resolve_provider_name(provider_name)
        return (model or (resolved or {}).get("model"), resolved)

    @app.post("/register")
    def register(req: RegisterReq):
        validate_id(req.worker_id, "worker_id")
        validate_host(req.host)
        if not (1 <= req.port <= 65535):
            raise ValueError(f"invalid port: {req.port}")
        registry.register(WorkerInfo(
            worker_id=req.worker_id, role=req.role, host=req.host, port=req.port,
            capabilities=req.capabilities, last_heartbeat=0.0,
        ))
        healed = 0
        if retry_manager:
            healed = retry_manager.check_worker_healing(req.worker_id)
        return {"status": "registered", "worker_id": req.worker_id, "healed_tasks": healed}

    @app.post("/heartbeat")
    def heartbeat(req: HeartbeatReq):
        registry.heartbeat(req.worker_id, req.load)
        return {"status": "ok"}

    @app.get("/workers")
    def list_workers():
        return [asdict(w) for w in registry.active_workers()]

    @app.delete("/workers/{worker_id}")
    def delete_worker(worker_id: str):
        if not registry.unregister(worker_id):
            raise HTTPException(status_code=404, detail="worker not found")
        return {"status": "unregistered", "worker_id": worker_id}

    # ---- 运行时 Provider 池管理（密钥只写 providers.json，gitignore；GET 回显遮蔽）----

    class ProviderPutReq(BaseModel):
        name: str
        model: str | None = None
        base_url: str | None = None
        auth_token: str | None = None

    class DefaultProviderReq(BaseModel):
        name: str = ""  # 空=清空默认

    @app.get("/providers")
    def list_providers():
        """遮蔽密钥后的池视图 + 当前默认名（真实密钥绝不回显）。"""
        return provider_store.list_all()

    @app.put("/providers")
    def put_provider(req: ProviderPutReq):
        name = req.name.strip()
        validate_id(name, "provider name")
        provider_store.upsert(name, model=req.model or "",
                              base_url=req.base_url or "",
                              auth_token=req.auth_token or "")
        return {"status": "saved", "name": name}

    @app.delete("/providers/{name}")
    def delete_provider(name: str):
        if not provider_store.delete(name):
            raise HTTPException(status_code=404, detail="provider not found")
        return {"status": "deleted", "name": name}

    @app.get("/providers/default")
    def get_default_provider():
        return {"default": provider_store.default()}

    @app.put("/providers/default")
    def set_default_provider(req: DefaultProviderReq):
        """热切默认：只影响后续新任务；running/排队任务用已解析值。"""
        name = req.name.strip()
        if name:
            validate_id(name, "provider name")
        try:
            provider_store.set_default(name)
        except KeyError:
            raise HTTPException(status_code=404,
                                detail=f"provider '{name}' 不存在")
        return {"status": "default_set", "default": provider_store.default()}

    @app.get("/tasks")
    def list_tasks(worker_id: str | None = None, status: str | None = None):
        recs = task_store.list_by_worker(worker_id) if worker_id else task_store.list_all()
        if status:
            recs = [r for r in recs if r.status == status]
        return [asdict(r) for r in recs]

    @app.get("/tasks/{task_id}")
    def get_task(task_id: str):
        rec = task_store.get(task_id)
        if rec is None:
            raise HTTPException(status_code=404, detail="task not found")
        return asdict(rec)

    @app.post("/tasks/{task_id}/cancel")
    def cancel_task(task_id: str):
        rec = task_store.get(task_id)
        if rec is None:
            raise HTTPException(status_code=404, detail="task not found")
        if rec.status not in ("pending", "running"):
            raise HTTPException(status_code=400, detail=f"无法取消，任务当前状态为 {rec.status}")
        if rec.worker_id:
            w = registry.get_worker(rec.worker_id)
            if w:
                try:
                    http_call("POST", f"http://{w.host}:{w.port}/task/{task_id}/cancel")
                except Exception:
                    pass
        # 本地执行（ORCH_LOCAL_EXEC）：worker_id=="local"、无远程 worker 可通知，
        # 需直接信号派发器置位该任务的 cancel_event，否则本地执行收不到取消。
        if dispatcher is not None and hasattr(dispatcher, "cancel"):
            try:
                dispatcher.cancel(task_id)
            except Exception:  # noqa: BLE001 - 取消信号失败不阻断状态置终
                pass
        rec = task_store.update(task_id, status="cancelled")
        # cancelled 属终态：发布 done 事件让 SSE 订阅流关闭
        publish_task_event(bus, task_id, event="done", state="cancelled",
                           trace_id=rec.trace_id, summary="cancelled by request")
        return asdict(rec)

    @app.post("/tasks/{task_id}/retry")
    def retry_task(task_id: str):
        if dispatcher is None:
            raise HTTPException(status_code=400, detail="dispatcher 未配置（local 模式不支持重试）")
        rec = task_store.get(task_id)
        if rec is None:
            raise HTTPException(status_code=404, detail="task not found")
        if rec.status not in ("lost", "failed"):
            raise HTTPException(status_code=400, detail=f"无法重试，任务当前状态为 {rec.status}")
        # 被人工拒绝的指令不允许经 retry 绕过安全关卡重新执行
        rec_meta = rec.meta if isinstance(rec.meta, dict) else {}
        if (rec_meta.get("approval") or {}).get("status") == "rejected":
            raise HTTPException(status_code=400,
                                detail="被人工拒绝的指令不可重试，请修订后重新派发")
        try:
            res = dispatcher.dispatch(role=rec.role, prompt=rec.prompt)
        except RuntimeError as e:
            return {"status": "failed", "error": str(e)}
        task_store.update(rec.task_id, status=res.status,
                          retry_count=rec.retry_count + 1,
                          last_retry_at=str(int(time.time())))
        return {
            "task_id": res.task_id, "status": res.status,
            "output": res.output, "artifacts": res.artifacts, "error": res.error,
            "product_verdict": res.verdict,
        }

    @app.post("/tasks/{task_id}/redispatch")
    def redispatch_task(task_id: str, req: RedispatchReq):
        if dispatcher is None:
            raise HTTPException(status_code=400, detail="dispatcher 未配置（local 模式不支持重派）")
        if req.worker_id:
            validate_id(req.worker_id, "worker_id")
        # 待审批/派发占位中的任务不可重派：需先走 approve/reject 人工裁决流程
        rec0 = task_store.get(task_id)
        if rec0 is not None and rec0.status == "awaiting_approval":
            raise HTTPException(status_code=400,
                                detail="待审批任务不可重派，请先走 approve/reject 流程")
        if rec0 is not None and rec0.status == "dispatching":
            raise HTTPException(status_code=400,
                                detail="任务正在审批派发中，不可重派")
        # 被人工拒绝的指令不允许经 redispatch 绕过安全关卡重新执行（与 retry 同检查）
        if rec0 is not None:
            rec_meta = rec0.meta if isinstance(rec0.meta, dict) else {}
            if (rec_meta.get("approval") or {}).get("status") == "rejected":
                raise HTTPException(status_code=400,
                                    detail="被人工拒绝的指令不可重派，请修订后重新派发")
        # Provider 继承：显式 req.provider 优先；缺省沿用原任务 meta.provider（同名
        # 重新从环境解析，密钥不落盘）。模型同理：显式 req.model 优先，缺省沿用原 model。
        provider_name = (req.provider or "").strip() or (
            (rec0.meta if isinstance(rec0.meta, dict) else {}).get("provider") or None)
        model, provider = _resolve_provider(req.model, provider_name)
        try:
            res = dispatcher.redispatch(task_id, model=model,
                                        worker_id=req.worker_id, provider=provider)
        except KeyError as e:
            raise HTTPException(status_code=404, detail=str(e))
        except RuntimeError as e:
            return {"status": "failed", "error": str(e)}
        return {
            "task_id": res.task_id, "status": res.status,
            "output": res.output, "artifacts": res.artifacts, "error": res.error,
            "product_verdict": res.verdict,
            "provider": provider["name"] if provider else None,
        }

    def _approval_meta(rec: TaskRecord) -> dict:
        """读取任务 meta 中的 approval 详情（缺省空 dict，类型容错）。"""
        meta = rec.meta if isinstance(rec.meta, dict) else {}
        approval = meta.get("approval")
        return dict(approval) if isinstance(approval, dict) else {}

    def _await_approval(req: DispatchReq, verdict: dict, request: Request) -> dict:
        """block 指令转入待审批：任务挂起而非直接终结（阶段四审批挂起态）。

        被拦截指令与裁决详情存 TaskRecord.meta["approval"]，状态置
        awaiting_approval（非终态：不参与自动重试、不被 sweeper 标 lost、
        SSE 流保持打开）；进入该状态发布 status 事件（时间线可见）。
        force=true 仍直接放行（兼容旧有行为）。
        """
        task_id = f"task-{uuid.uuid4().hex[:8]}"
        trace_id = new_trace_id()
        ts = str(int(time.time()))
        model = _route_model(req)
        task_store.write(TaskRecord(
            task_id=task_id, role=req.role, prompt=req.prompt, worker_id=None,
            status="awaiting_approval", created_at=ts, updated_at=ts,
            output=None, artifacts=[], error=None, trace_id=trace_id,
            meta={"approval": {
                "status": "pending",
                "verdict": verdict,
                "original_prompt": req.prompt,
                "context_memory": req.context_memory,
                "context_task_ids": req.context_task_ids,
                "model": model,
                "worker_id": req.worker_id,  # 定点续传：approve 时恢复对同一节点派发
                "provider": req.provider,   # 只缓存 provider 名，不缓存三元组/<密钥>
                "requested_by": _request_operator(request),
            }},
        ))
        publish_task_event(bus, task_id, event="status", state="awaiting_approval",
                           trace_id=trace_id,
                           summary=str(verdict.get("reason") or "指令被安全关卡拦截，待人工审批"))
        return {"status": "awaiting_approval", "task_id": task_id,
                "verdict": verdict, "output": None, "artifacts": [], "error": None}

    @app.post("/tasks/{task_id}/approve")
    def approve_task(task_id: str, req: ApproveReq, request: Request):
        """人工批准待审批任务：修订 prompt 则重过 security-guard，通过后按原流程派发。"""
        if dispatcher is None:
            raise HTTPException(status_code=400,
                                detail="dispatcher 未配置（local 模式不支持审批派发）")
        rec = task_store.get(task_id)
        if rec is None:
            raise HTTPException(status_code=404, detail="task not found")
        if rec.status != "awaiting_approval":
            raise HTTPException(status_code=400,
                                detail=f"任务当前状态为 {rec.status}，非待审批")
        # 原子占位（compare-and-set）：awaiting_approval -> dispatching。本端点是
        # sync def，在线程池并发运行，「读状态→校验→派发（最长500s）→更新」窗口
        # 内并发 approve 会双重派发同一危险指令；先占位使并发裁决者拿到 409。
        if not task_store.transition(task_id, "awaiting_approval", "dispatching"):
            raise HTTPException(status_code=409,
                                detail="任务已被其他请求处理（并发裁决冲突），请刷新后重试")
        approval = _approval_meta(rec)
        operator = _request_operator(request)
        revised_prompt = (req.prompt or "").strip()
        revised = bool(revised_prompt and revised_prompt != rec.prompt)
        prompt = revised_prompt or rec.prompt
        if revised:
            # 修订后的指令必须重过安全关卡；不通过则回滚占位并更新裁决详情
            v = security_check(prompt, use_llm=cfg.security_llm)
            record_security_event(
                task_id=task_id, stage="approve",
                decision="warn" if (v["verdict"] == "pass" and v.get("matched_rules"))
                else v["verdict"],
                matched_rules=v.get("matched_rules"), operator=operator,
                llm_enhanced=bool(v.get("llm_enhanced", cfg.security_llm)),
                reason=v.get("reason", ""))
            if v["verdict"] == "block":
                task_store.transition(task_id, "dispatching", "awaiting_approval")
                approval["status"] = "pending"
                approval["verdict"] = v
                approval["revised_prompt"] = prompt
                meta = dict(rec.meta or {})
                meta["approval"] = approval
                task_store.update(task_id, meta=meta, updated_at=str(int(time.time())))
                publish_task_event(bus, task_id, event="status",
                                   state="awaiting_approval", trace_id=rec.trace_id,
                                   summary="修订后的指令仍未通过安全检验，维持待审批")
                return {"status": "awaiting_approval", "task_id": task_id,
                        "approved": False, "verdict": v,
                        "error": "修订后的指令仍未通过安全检验，请继续修订或拒绝"}
        else:
            # 原指令人工放行：人工裁决即放行，不再重检；审计留痕关联原拦截裁决
            record_security_event(
                task_id=task_id, stage="approve", decision="approve",
                matched_rules=approval.get("verdict", {}).get("matched_rules"),
                operator=operator, llm_enhanced=cfg.security_llm,
                reason="人工批准放行被拦截指令")
        # 按原流程派发：沿用挂起时缓存的 context/model，修订时用修订后 prompt
        context = approval.get("context_memory") or ""
        ctx_task_ids = approval.get("context_task_ids") or []
        if memory_store is not None and ctx_task_ids:
            context = assemble_context(memory_store, context, ctx_task_ids)
        context = clamp_context(context, cfg.context_memory_max_chars)
        model = approval.get("model")
        # Provider：approve 时按缓存名重新解析（不缓存三元组/密钥于 approval meta）；
        # 老任务该键缺失 → 走当前默认 provider（与「新任务」语义一致）
        model, provider = _resolve_provider(model, approval.get("provider"))
        # 定点续传：被拦截时缓存了 worker_id，approve 时恢复对同一节点派发；
        # 老任务该键缺失 → None → 自动选节点（向后兼容）
        worker_id = approval.get("worker_id")
        try:
            res = dispatcher.dispatch(role=rec.role, prompt=prompt,
                                      context_memory=context or None, model=model,
                                      worker_id=worker_id, provider=provider)
        except Exception as e:  # noqa: BLE001 - 派发失败回滚占位，任务回到待审批
            task_store.transition(task_id, "dispatching", "awaiting_approval")
            publish_task_event(bus, task_id, event="status", state="awaiting_approval",
                               trace_id=rec.trace_id,
                               summary=f"approve 派发失败，回到待审批: {e}")
            # 响应契约（与前端约定）：approved=false 才视为未成功放行
            return {"approved": False, "status": "failed",
                    "error": str(e), "task_id": task_id}
        approval["status"] = "approved"
        approval["approved_prompt"] = prompt
        approval["approved_by"] = operator
        approval["dispatched_task_id"] = res.task_id
        meta = dict(rec.meta or {})
        meta["approval"] = approval
        task_store.update(task_id, status="done",
                          output=f"已人工批准并派发为 {res.task_id}",
                          meta=meta, updated_at=str(int(time.time())))
        publish_task_event(bus, task_id, event="done", state="done",
                           trace_id=rec.trace_id,
                           summary=f"approved by {operator or 'operator'} -> {res.task_id}")
        return {"status": "approved", "approved": True, "task_id": task_id,
                "dispatched_task_id": res.task_id, "dispatch_status": res.status}

    @app.post("/tasks/{task_id}/reject")
    def reject_task(task_id: str, req: RejectReq, request: Request):
        """人工拒绝待审批任务：终态 failed + 审计留痕。

        与 approve 的 dispatching 占位互斥：先原子 CAS 到 failed，
        并发 approve/reject 同一任务时落败方拿到 409。
        """
        rec = task_store.get(task_id)
        if rec is None:
            raise HTTPException(status_code=404, detail="task not found")
        if rec.status != "awaiting_approval":
            raise HTTPException(status_code=400,
                                detail=f"任务当前状态为 {rec.status}，非待审批")
        if not task_store.transition(task_id, "awaiting_approval", "failed"):
            raise HTTPException(status_code=409,
                                detail="任务已被其他请求处理（并发裁决冲突），请刷新后重试")
        approval = _approval_meta(rec)
        operator = _request_operator(request)
        approval["status"] = "rejected"
        approval["rejected_by"] = operator
        if req.reason:
            approval["reject_reason"] = req.reason
        meta = dict(rec.meta or {})
        meta["approval"] = approval
        rec = task_store.update(
            task_id,
            error=req.reason or "指令被安全关卡拦截，人工裁决拒绝执行",
            meta=meta, updated_at=str(int(time.time())))
        record_security_event(
            task_id=task_id, stage="approve", decision="reject",
            matched_rules=approval.get("verdict", {}).get("matched_rules"),
            operator=operator, llm_enhanced=cfg.security_llm,
            reason=req.reason or "人工拒绝执行被拦截指令")
        # failed 属终态：发布 done 事件让 SSE 订阅流关闭
        publish_task_event(bus, task_id, event="done", state="failed",
                           trace_id=rec.trace_id,
                           summary=f"rejected by {operator or 'operator'}")
        return asdict(rec)

    @app.post("/dispatch")
    def dispatch(req: DispatchReq, request: Request):
        if dispatcher is None:
            return {"status": "failed", "error": "dispatcher 未配置（local 模式不提供 /dispatch）"}
        # 定点 worker_id 格式校验放最前：非法输入 400，且不烧 security_check 的 LLM 调用
        if req.worker_id:
            validate_id(req.worker_id, "worker_id")
        # security-guard 指令检验；force=true 跳过（用于放行 block 指令）
        if not req.force:
            v = security_check(req.prompt, use_llm=cfg.security_llm)
            # 前置关卡审计：block/warn/pass 全量留痕（写入失败不阻断主流程）
            record_security_event(
                task_id=None, stage="dispatch",
                decision="warn" if (v["verdict"] == "pass" and v.get("matched_rules"))
                else v["verdict"],
                matched_rules=v.get("matched_rules"),
                operator=_request_operator(request),
                llm_enhanced=bool(v.get("llm_enhanced", cfg.security_llm)),
                reason=v.get("reason", ""),
            )
            if v["verdict"] == "block":
                # 阶段四审批挂起态：不再直接终结，任务挂起等人工 approve/reject
                return _await_approval(req, v, request)
        else:
            # force 强制放行：审计记录决策与来源（web Command「强制放行」按钮走此处）
            record_security_event(
                task_id=None, stage="dispatch", decision="force",
                operator=_request_operator(request),
                llm_enhanced=cfg.security_llm)
        context = req.context_memory
        if memory_store is not None and req.context_task_ids:
            context = assemble_context(memory_store, context, req.context_task_ids)
        context = clamp_context(context, cfg.context_memory_max_chars)
        model = _route_model(req)
        # 运行时 Provider：名 → 三元组（密钥不落盘）；显式名/默认名不可用 → 400
        model, provider = _resolve_provider(model, req.provider)
        try:
            res = dispatcher.dispatch(role=req.role, prompt=req.prompt,
                                      context_memory=context or None, model=model,
                                      use_experience=req.use_experience,
                                      enable_subagents=req.enable_subagents,
                                      worker_id=req.worker_id,
                                      provider=provider)
        except RuntimeError as e:
            return {"status": "failed", "error": str(e)}
        return {
            "task_id": res.task_id, "status": res.status,
            "output": res.output, "artifacts": res.artifacts, "error": res.error,
            "product_verdict": res.verdict, "model": model,
            "provider": provider["name"] if provider else None,
        }

    @app.post("/dispatch/async")
    async def dispatch_async(req: DispatchReq, request: Request):
        if async_dispatcher is None:
            return {"status": "failed", "error": "async_dispatcher 未配置"}
        if req.worker_id:
            validate_id(req.worker_id, "worker_id")
        if not req.force:
            # security_llm 同步等待秒级：挪到线程池执行，避免阻塞 brain 事件循环
            # （同步 /dispatch 路径保持原样不动）
            v = await asyncio.to_thread(security_check, req.prompt,
                                        use_llm=cfg.security_llm)
            # 前置关卡审计：与同步 /dispatch 同语义，block/warn/pass 全量留痕
            record_security_event(
                task_id=None, stage="dispatch",
                decision="warn" if (v["verdict"] == "pass" and v.get("matched_rules"))
                else v["verdict"],
                matched_rules=v.get("matched_rules"),
                operator=_request_operator(request),
                llm_enhanced=bool(v.get("llm_enhanced", cfg.security_llm)),
                reason=v.get("reason", ""),
            )
            if v["verdict"] == "block":
                # 阶段四审批挂起态：与同步 /dispatch 同语义，挂起等人工裁决
                return _await_approval(req, v, request)
        else:
            # force 强制放行：审计记录决策与来源
            record_security_event(
                task_id=None, stage="dispatch", decision="force",
                operator=_request_operator(request),
                llm_enhanced=cfg.security_llm)
        context = req.context_memory
        if memory_store is not None and req.context_task_ids:
            context = assemble_context(memory_store, context, req.context_task_ids)
        context = clamp_context(context, cfg.context_memory_max_chars)
        model = _route_model(req)
        # 运行时 Provider：名 → 三元组（密钥不落盘）；显式名/默认名不可用 → 400
        model, provider = _resolve_provider(model, req.provider)
        try:
            task_id = await async_dispatcher.dispatch_async(
                role=req.role, prompt=req.prompt,
                context_memory=context or None, model=model,
                use_experience=req.use_experience,
                enable_subagents=req.enable_subagents,
                worker_id=req.worker_id,
                provider=provider,
            )
        except RuntimeError as e:
            return {"status": "failed", "error": str(e)}
        return {"status": "accepted", "task_id": task_id, "model": model,
                "provider": provider["name"] if provider else None}

    @app.post("/recommend")
    async def recommend(req: AdvisorReq, request: Request):
        """AI 推荐/解释顾问：运行于独立上下文，绝不污染任务上下文。

        默认仅返回建议（should_dispatch/roles/plan/explanation/risks）；
        auto_apply（请求覆盖或全局 advisor_auto_apply）且顾问建议派发时，
        经既有安全关卡把原始任务派发给建议的首要角色（默认 executor）。
        安全关卡拦截时不自动派发（返回 blocked），由用户走 /dispatch 人工审批。
        """
        if advisor is None or not getattr(advisor.config, "advisor_enabled", True):
            return {"status": "failed", "error": "advisor 未配置/未启用"}
        # 顾问是同步 + LLM 调用：挪到线程池，避免阻塞 brain 事件循环
        result = await asyncio.to_thread(
            advisor.recommend, req.task, mode=req.mode,
            finished_output=req.finished_output)
        auto_apply = (req.auto_apply if req.auto_apply is not None
                      else bool(getattr(advisor.config, "advisor_auto_apply", False)))
        payload: dict = {
            "status": "ok",
            "recommendation": result.to_dict(),
            "auto_applied": False,
            "dispatched": [],
        }
        if not (auto_apply and result.should_dispatch):
            return payload
        if dispatcher is None:
            payload["error"] = "auto_apply 需要已配置 dispatcher（本地执行或远程）"
            return payload
        role = (result.roles[0] if result.roles else "executor")
        # auto-apply 仍走安全关卡：绝不因推荐而绕过拦截/审批
        v = await asyncio.to_thread(security_check, req.task, use_llm=cfg.security_llm)
        record_security_event(
            task_id=None, stage="recommend_auto_apply",
            decision="warn" if (v["verdict"] == "pass" and v.get("matched_rules"))
            else v["verdict"],
            matched_rules=v.get("matched_rules"),
            operator=_request_operator(request),
            llm_enhanced=bool(v.get("llm_enhanced", cfg.security_llm)),
            reason=v.get("reason", ""),
        )
        if v["verdict"] == "block":
            payload["dispatched"].append({"role": role, "status": "blocked",
                                          "reason": v.get("reason", "")})
            return payload
        try:
            res = await asyncio.to_thread(
                dispatcher.dispatch, role=role, prompt=req.task,
                context_memory=None, model=None,
                enable_subagents=req.enable_subagents)
        except RuntimeError as e:
            payload["error"] = str(e)
            return payload
        payload["auto_applied"] = True
        payload["dispatched"].append({
            "role": role, "task_id": res.task_id, "status": res.status,
            "output": res.output, "artifacts": res.artifacts, "error": res.error,
        })
        return payload

    @app.post("/translate")
    async def translate(req: TranslateReq):
        """结果翻译：运行于独立上下文，把结果文本译为目标语言（默认中文）。

        思考过程/流式 token 保持原语言（通常英文），本端点只对给定文本做一次
        旁路翻译（绝不污染任务上下文）。翻译服务是同步 + LLM 调用：挪到线程池，
        避免阻塞 brain 事件循环（与 /recommend 同姿态）。
        """
        if translator is None:
            return {"status": "failed", "error": "translator 未配置"}
        result = await asyncio.to_thread(
            translator.translate, req.text, target_lang=req.target_lang)
        return {"status": "ok", "translation": result.to_dict()}

    @app.get("/tasks/{task_id}/events")
    async def task_events(task_id: str):
        """SSE 实时事件流：status（迁移/轮询进展）| callback（回调到达）| done（终态后关闭）。

        EventSource 无法自定义 header，鉴权同时接受 X-API-Key 头与 ?api_key= 查询参数
        （auth 中间件已支持 query key，无需本端点特殊处理）。先补发当前状态快照，
        终态任务直接结束；空闲 15s 发心跳注释行防中间层断开。
        """
        rec = task_store.get(task_id)
        if rec is None:
            raise HTTPException(status_code=404, detail="task not found")

        async def gen():
            sid, q = bus.subscribe(task_id)
            try:
                # 先补发快照（订阅与快照之间的窗口事件已入队兜底），终态则直接收流
                cur = task_store.get(task_id)
                if cur is not None:
                    yield TaskEvent("status", {
                        "task_id": task_id, "trace_id": cur.trace_id,
                        "state": cur.status, "timestamp": time.time(),
                        "snapshot": True,
                    }).sse_frame()
                    if cur.status in TERMINAL_STATUSES:
                        return
                while True:
                    try:
                        ev = await asyncio.wait_for(q.get(), timeout=15.0)
                    except asyncio.TimeoutError:
                        yield ": keepalive\n\n"
                        continue
                    yield ev.sse_frame()
                    if ev.event == "done":
                        return
            finally:
                bus.unsubscribe(sid)

        return StreamingResponse(gen(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache",
                                          "X-Accel-Buffering": "no"})

    @app.get("/tasks/{task_id}/status")
    def task_status(task_id: str):
        rec = task_store.get(task_id)
        if rec is None:
            raise HTTPException(status_code=404, detail="task not found")
        return {"task_id": rec.task_id, "status": rec.status,
                "worker_id": rec.worker_id, "error": rec.error}

    @app.post("/tasks/{task_id}/callback")
    async def task_callback(task_id: str, data: dict):
        if async_dispatcher is None:
            raise HTTPException(status_code=400, detail="async_dispatcher 未配置")
        handled = await async_dispatcher.handle_callback(task_id, data)
        if not handled:
            rec = task_store.get(task_id)
            if rec is None:
                raise HTTPException(status_code=404, detail="task not found")
        return {"status": "ok", "handled": handled}

    @app.get("/stats")
    def stats():
        workers = registry.all_workers()
        alive = registry.active_workers()
        tasks = task_store.list_all()
        status_counts: dict[str, int] = {}
        for t in tasks:
            status_counts[t.status] = status_counts.get(t.status, 0) + 1
        recent = sorted(tasks, key=lambda r: r.updated_at, reverse=True)[:10]
        exp_count = len(experience_store.list_all()) if experience_store else 0
        return {
            "mode": cfg.mode,
            "brain_url": cfg.brain_url,
            "heartbeat_timeout": cfg.heartbeat_timeout,
            "worker_total": len(workers),
            "worker_active": len(alive),
            "task_total": len(tasks),
            "task_status": status_counts,
            "experience_total": exp_count,
            "recent_tasks": [asdict(r) for r in recent],
            # 成本聚合（近 7 天按日 / 按 role / 按 model，源自 TaskRecord.meta）
            "cost": _aggregate_costs(tasks),
            # 输出质量聚合（按 model 统计退化任务数与比率，与 cost 同级）
            "quality": _aggregate_quality(tasks),
        }

    # ---- 记忆管理（V2 接线：此前 MemoryStore 只有抽象无调用方） ----
    # core 为持久记忆（写入门控由大脑流程负责：V1 三方共识 / 用户显式操作）
    @app.get("/memory/core")
    def list_core_memories():
        if memory_store is None:
            raise HTTPException(status_code=400, detail="memory_store 未配置")
        names = memory_store.list_core()
        return {"memories": [{"name": n, "content": memory_store.read_core(n)}
                             for n in names]}

    @app.get("/memory/core/{name}")
    def get_core_memory(name: str):
        if memory_store is None:
            raise HTTPException(status_code=400, detail="memory_store 未配置")
        content = memory_store.read_core(name)
        if content is None:
            raise HTTPException(status_code=404, detail="memory not found")
        return {"name": name, "content": content}

    @app.put("/memory/core/{name}")
    def put_core_memory(name: str, req: MemoryWriteReq):
        if memory_store is None:
            raise HTTPException(status_code=400, detail="memory_store 未配置")
        memory_store.write_core(name, req.content)
        return {"status": "written", "name": name}

    @app.delete("/memory/core/{name}")
    def delete_core_memory(name: str):
        if memory_store is None:
            raise HTTPException(status_code=400, detail="memory_store 未配置")
        memory_store.delete_core(name)
        return {"status": "deleted", "name": name}

    @app.get("/memory/context/{task_id}")
    def get_context_memory(task_id: str):
        if memory_store is None:
            raise HTTPException(status_code=400, detail="memory_store 未配置")
        notes = {k: memory_store.read_context(task_id, k)
                 for k in memory_store.list_context(task_id)}
        return {"task_id": task_id, "notes": notes}

    @app.put("/memory/context/{task_id}/{key}")
    def put_context_memory(task_id: str, key: str, req: MemoryWriteReq):
        if memory_store is None:
            raise HTTPException(status_code=400, detail="memory_store 未配置")
        memory_store.write_context(task_id, key, req.content)
        return {"status": "written", "task_id": task_id, "key": key}

    @app.delete("/memory/context/{task_id}")
    def archive_context_memory(task_id: str):
        """任务结束归档清理该任务的全部上下文记忆。"""
        if memory_store is None:
            raise HTTPException(status_code=400, detail="memory_store 未配置")
        archived = memory_store.delete_context(task_id)
        return {"status": "archived" if archived else "noop", "task_id": task_id}

    # ---- 程序记忆演化（技能定义的提案-共识-应用，LangGraph Reflection 式） ----
    @app.get("/memory/procedural/reflection")
    def procedural_reflection(window: int = 50):
        """近期经验的确定性反思聚合，供大脑起草技能定义修订提案。"""
        if experience_store is None:
            raise HTTPException(status_code=400, detail="experience_store 未配置")
        recs = sorted(experience_store.list_all(),
                      key=lambda r: r.created_at, reverse=True)
        recent = recs[:max(0, window)]
        by_role: dict[str, dict] = {}
        for r in recent:
            d = by_role.setdefault(r.role, {"total": 0, "success": 0})
            d["total"] += 1
            d["success"] += 1 if r.success else 0
        return {
            "window": window,
            "total": len(recent),
            "success": sum(1 for r in recent if r.success),
            "by_role": by_role,
            "failed_recent": [{"exp_id": r.exp_id, "prompt": r.prompt[:120]}
                              for r in recent if not r.success][:10],
            "most_hit": [{"exp_id": r.exp_id, "hits": r.hits,
                          "prompt": r.prompt[:80]}
                         for r in sorted(recent, key=lambda x: x.hits,
                                         reverse=True)[:5] if r.hits > 0],
        }

    @app.post("/memory/procedural/proposals")
    def create_procedural_proposal(req: ProceduralProposalReq):
        if procedural_store is None:
            raise HTTPException(status_code=400, detail="procedural_store 未配置")
        try:
            rec = procedural_store.propose(req.target_path, req.content,
                                           req.reason, req.evidence_exp_ids)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        return asdict(rec)

    @app.get("/memory/procedural/proposals")
    def list_procedural_proposals(status: str | None = None):
        if procedural_store is None:
            raise HTTPException(status_code=400, detail="procedural_store 未配置")
        return [asdict(r) for r in procedural_store.list_all(status)]

    @app.post("/memory/procedural/proposals/{proposal_id}/vote")
    def vote_procedural_proposal(proposal_id: str, req: ProceduralVoteReq):
        if procedural_store is None:
            raise HTTPException(status_code=400, detail="procedural_store 未配置")
        try:
            rec = procedural_store.vote(proposal_id, req.voter, req.verdict, req.reason)
        except KeyError as e:
            raise HTTPException(status_code=404, detail=str(e))
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        return asdict(rec)

    @app.delete("/memory/procedural/proposals/{proposal_id}")
    def delete_procedural_proposal(proposal_id: str):
        if procedural_store is None:
            raise HTTPException(status_code=400, detail="procedural_store 未配置")
        if not procedural_store.delete(proposal_id):
            raise HTTPException(status_code=404, detail="proposal not found")
        return {"status": "deleted", "proposal_id": proposal_id}

    @app.get("/experiences")
    def list_experiences(role: str | None = None, successful_only: bool = True):
        if experience_store is None:
            raise HTTPException(status_code=400, detail="experience_store 未配置")
        if role:
            recs = experience_store.list_by_role(role)
        else:
            recs = experience_store.list_all()
        if successful_only:
            recs = [r for r in recs if r.success]
        return [asdict(r) for r in recs]

    @app.post("/experiences/search")
    def search_experiences(req: ExperienceSearchReq):
        if experience_store is None:
            raise HTTPException(status_code=400, detail="experience_store 未配置")
        recs = experience_store.search(
            req.query, role=req.role,
            successful_only=req.successful_only, limit=req.limit,
        )
        return [asdict(r) for r in recs]

    @app.get("/experiences/{exp_id}")
    def get_experience(exp_id: str):
        if experience_store is None:
            raise HTTPException(status_code=400, detail="experience_store 未配置")
        rec = experience_store.get(exp_id)
        if rec is None:
            raise HTTPException(status_code=404, detail="experience not found")
        return asdict(rec)

    @app.delete("/experiences/{exp_id}")
    def delete_experience(exp_id: str):
        if experience_store is None:
            raise HTTPException(status_code=400, detail="experience_store 未配置")
        if not experience_store.delete(exp_id):
            raise HTTPException(status_code=404, detail="experience not found")
        return {"status": "deleted", "exp_id": exp_id}

    @app.post("/experiences/reindex")
    def reindex_experiences():
        if experience_store is None:
            raise HTTPException(status_code=400, detail="experience_store 未配置")
        count = experience_store.reindex()
        return {"status": "ok", "reindexed": count}

    @app.post("/schedules")
    def create_schedule(req: ScheduleReq):
        if schedule_store is None:
            raise HTTPException(status_code=400, detail="schedule_store 未配置")
        try:
            compute_next_run(req.cron_expr, time.time())
        except (ValueError, RuntimeError) as e:
            raise HTTPException(status_code=400, detail=str(e))
        import uuid
        sid = f"sched-{uuid.uuid4().hex[:8]}"
        next_run = str(int(compute_next_run(req.cron_expr, time.time())))
        rec = ScheduleRecord(
            schedule_id=sid, name=req.name, cron_expr=req.cron_expr,
            role=req.role, prompt=req.prompt,
            context_memory=req.context_memory, model=req.model,
            provider=req.provider,
            created_at=str(int(time.time())), next_run=next_run,
        )
        schedule_store.write(rec)
        return asdict(rec)

    @app.get("/schedules")
    def list_schedules():
        if schedule_store is None:
            raise HTTPException(status_code=400, detail="schedule_store 未配置")
        return [asdict(r) for r in schedule_store.list_all()]

    @app.delete("/schedules/{schedule_id}")
    def delete_schedule(schedule_id: str):
        if schedule_store is None:
            raise HTTPException(status_code=400, detail="schedule_store 未配置")
        if not schedule_store.delete(schedule_id):
            raise HTTPException(status_code=404, detail="schedule not found")
        return {"status": "deleted", "schedule_id": schedule_id}

    @app.post("/schedules/{schedule_id}/toggle")
    def toggle_schedule(schedule_id: str):
        if schedule_store is None:
            raise HTTPException(status_code=400, detail="schedule_store 未配置")
        rec = schedule_store.toggle_enabled(schedule_id)
        if rec is None:
            raise HTTPException(status_code=404, detail="schedule not found")
        return asdict(rec)

    @app.post("/schedules/{schedule_id}/trigger")
    def trigger_schedule(schedule_id: str):
        if task_scheduler is None:
            raise HTTPException(status_code=400, detail="task_scheduler 未配置")
        ok = task_scheduler.trigger_manual(schedule_id)
        if not ok:
            raise HTTPException(status_code=404, detail="schedule not found or dispatch failed")
        return {"status": "triggered", "schedule_id": schedule_id}

    @app.get("/proxy/artifact")
    def proxy_artifact(worker_id: str, task_id: str, path: str):
        # 防御性校验：worker 端还会再做一次，这里先快速拒绝明显恶意参数
        validate_id(task_id, "task_id")
        if ".." in path or path.startswith(("/", "\\")) or "\\" in path:
            raise HTTPException(status_code=400, detail="invalid path")
        w = next((x for x in registry.all_workers() if x.worker_id == worker_id), None)
        if w is None:
            raise HTTPException(status_code=404, detail="worker not found")
        base = f"http://{w.host}:{w.port}"
        resp = http_call("GET", f"{base}/artifact/{task_id}", params={"path": path})
        if resp.get("status_code") != 200:
            raise HTTPException(status_code=502, detail=f"worker 返回: {resp}")
        return resp.get("json", {})

    def _proxy_worker(worker_id: str):
        """代理端 worker 解析：复用 /proxy/artifact 的注册表查找，未注册 404。"""
        w = next((x for x in registry.all_workers() if x.worker_id == worker_id), None)
        if w is None:
            raise HTTPException(status_code=404, detail=f"worker not found: {worker_id}")
        return f"http://{w.host}:{w.port}"

    @app.get("/proxy/node/status")
    def proxy_node_status(worker_id: str):
        """节点详情（技能/MCP/工作区/run 计数）：经同 key 通道转调 worker /agent/status。
        worker 未开面板时该端点 404 → 502，语义即「此节点无面板/状态不可达」。"""
        base = _proxy_worker(worker_id)
        resp = http_call("GET", f"{base}/agent/status")
        if resp.get("status_code") != 200:
            raise HTTPException(status_code=502, detail=f"worker 返回: {resp}")
        return resp.get("json", {})

    @app.put("/proxy/node/workspace")
    def proxy_node_workspace(req: NodeWorkspaceProxyReq):
        """规定节点对话工作区：转调 worker PUT /agent/workspace。
        worker 端 400（目录不存在/越界）原样透传，其余失败 502。"""
        base = _proxy_worker(req.worker_id)
        resp = http_call("PUT", f"{base}/agent/workspace", json={"path": req.path})
        sc = resp.get("status_code")
        if sc == 200:
            return resp.get("json", {})
        body = resp.get("json", {})
        if sc == 400:
            raise HTTPException(status_code=400,
                                detail=body.get("detail", body) or f"worker 返回: {resp}")
        raise HTTPException(status_code=502, detail=f"worker 返回: {resp}")

    # 静态托管前端构建产物（生产同源）；目录不存在则跳过，仅提供 API
    web_dist = os.environ.get("ORCH_WEB_DIST") or _default_web_dist()
    if Path(web_dist).exists():
        app.mount("/", StaticFiles(directory=web_dist, html=True), name="web")

    return app


def _make_http_call(api_key: str = ""):
    """创建带 API Key 的同步 HTTP 客户端。"""
    import requests

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
        import httpx
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
    import sys
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
