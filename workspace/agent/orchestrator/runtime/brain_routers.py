"""大脑 HTTP 路由组：按资源拆分的 APIRouter 工厂 + 共享装配依赖。

把原 create_brain_app（969 行/56 个内层闭包）按资源拆成多个小 router 工厂，
每个工厂接收一个 BrainDeps（装配态实例集合），在 create_brain_app 中组装。
安全前置关卡 /dispatch 与 /dispatch/async 共用 _preflight，杜绝判定逻辑分叉。
"""
import asyncio
import logging
import os
import time
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any, Callable

from async_dispatcher import AsyncRemoteDispatcher
from audit_log import record_security_event
from budget_store import BudgetStore
from config import Config
from dispatch_context import approved_dispatch
from dispatcher import Dispatcher
from event_bus import TERMINAL_STATUSES, TaskEvent, TaskEventBus, publish_task_event
from experience_store import ExperienceStore
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from memory_store import MemoryStore
from procedural_store import ProceduralStore
from providers import ProviderStore
from pydantic import BaseModel
from retry_manager import RetryManager
from safe_id import validate_host, validate_id
from schedule_store import ScheduleRecord, ScheduleStore, compute_next_run
from security_guard import check_with_llm as security_check
from session_store import SessionStore
from task_scheduler import TaskScheduler
from worker_registry import WorkerInfo, WorkerRegistry

from dispatch_common import USAGE_KEYS, assemble_context, clamp_context
from task_store import TaskRecord, TaskStore, new_trace_id

log = logging.getLogger(__name__)


# ---- 请求体模型（原 brain_api 顶层；brain_api 再导出以兼容既有 import）----

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
    # 持久会话 id（POST /sessions 创建）：历史轮次注入 context_memory 延续对话，
    # 本轮 prompt+task_id 落盘会话文件；缺省 None = 单发独立任务（行为不变）。
    session_id: str | None = None


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


class _ProviderPutReq(BaseModel):
    name: str
    model: str | None = None
    base_url: str | None = None
    auth_token: str | None = None


class _DefaultProviderReq(BaseModel):
    name: str = ""  # 空=清空默认


# ---- 共享装配依赖 ----

@dataclass
class BrainDeps:
    """create_brain_app 装配的实例集合，传给各 router 工厂。

    store 类依赖可缺省（未配置时端点返回「未配置」而非崩溃）；
    resolve_provider_name 由 _make_provider_resolver 在装配时预绑定。
    """
    cfg: Config
    registry: WorkerRegistry
    task_store: TaskStore
    bus: TaskEventBus
    http_call: Any
    dispatcher: Dispatcher | None = None
    async_dispatcher: AsyncRemoteDispatcher | None = None
    experience_store: ExperienceStore | None = None
    memory_store: MemoryStore | None = None
    procedural_store: ProceduralStore | None = None
    retry_manager: RetryManager | None = None
    schedule_store: ScheduleStore | None = None
    task_scheduler: TaskScheduler | None = None
    advisor: Any = None
    translator: Any = None
    provider_store: ProviderStore | None = None
    resolve_provider_name: Callable[[str | None], dict | None] | None = None
    budget_store: BudgetStore | None = None
    session_store: SessionStore | None = None
    knowledge_store: Any = None  # KnowledgeStore（runtime/knowledge_store.py；可 None）
    webhook: Any = None  # WebhookNotifier（runtime/webhook.py；None=未配置外发）


# ---- 共享 helper（原 create_brain_app 内层闭包，提为依赖显式传入的纯函数）----

def _request_operator(request: Request) -> str | None:
    """尽力还原请求来源（审计 operator 字段）：优先 X-Operator 头，其次客户端 host。"""
    op = request.headers.get("X-Operator")
    if op:
        return op
    client = request.client
    return client.host if client else None


_COST_DAY_WINDOW = 7 * 86400  # 按日聚合窗口：近 7 天


def _usage_tokens(usage) -> int | None:
    """usage dict -> 总 token 数；无有效数值时返回 None（口径与 eval/worker 一致）。"""
    if not isinstance(usage, dict):
        return None
    nums = [usage[k] for k in USAGE_KEYS if isinstance(usage.get(k), (int, float))]
    return int(sum(nums)) if nums else None


def _aggregate_costs(tasks: list) -> dict:
    """从 TaskRecord.meta 聚合 token 与成本：按日（近 7 天）/ 按 role / 按 model。"""
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
    """从 TaskRecord.meta 聚合输出质量：按 model 统计任务数与退化率。"""
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


def _route_model(deps: BrainDeps, req: DispatchReq) -> str | None:
    """启发式模型路由（ORCH_MODEL_ROUTING=heuristic 时启用，默认 off）。"""
    cfg = deps.cfg
    if cfg.model_routing != "heuristic" or req.model or req.role != "executor":
        return req.model
    if any(tag in req.prompt for tag in
           ("[web-expert]", "[back-expert]", "[data-expert]")):
        return None  # 技能流程任务有质量约定，不降级
    if len(req.prompt) <= cfg.route_max_prompt_chars:
        return cfg.cheap_model
    return None


def _resolve_provider(deps: BrainDeps, model: str | None,
                      provider_name: str | None) -> tuple[str | None, dict | None]:
    """把 provider 名解析为三元组 + 返回最终 model。

    优先级：显式 provider 名 > brain 默认 provider（热切）> 无（worker 自身配置）。
    名不存在/环境变量不可解析 → ValueError（全局 handler → 400）。
    model 优先 req.model / 启发式路由，其次 provider.model（网关默认模型）。
    """
    resolved = deps.resolve_provider_name(provider_name)
    return (model or (resolved or {}).get("model"), resolved)


def _approval_meta(rec: TaskRecord) -> dict:
    """读取任务 meta 中的 approval 详情（缺省空 dict，类型容错）。"""
    meta = rec.meta if isinstance(rec.meta, dict) else {}
    approval = meta.get("approval")
    return dict(approval) if isinstance(approval, dict) else {}


def _await_approval(deps: BrainDeps, req: DispatchReq, verdict: dict,
                    request: Request) -> dict:
    """block 指令转入待审批：任务挂起而非直接终结（阶段四审批挂起态）。

    被拦截指令与裁决详情存 TaskRecord.meta["approval"]，状态置
    awaiting_approval（非终态：不参与自动重试、不被 sweeper 标 lost、
    SSE 流保持打开）；进入该状态发布 status 事件（时间线可见）。
    force=true 仍直接放行（兼容旧有行为）。
    """
    task_id = f"task-{uuid.uuid4().hex[:8]}"
    trace_id = new_trace_id()
    ts = str(int(time.time()))
    model = _route_model(deps, req)
    deps.task_store.write(TaskRecord(
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
    publish_task_event(deps.bus, task_id, event="status", state="awaiting_approval",
                       trace_id=trace_id,
                       summary=str(verdict.get("reason") or "指令被安全关卡拦截，待人工审批"))
    # 关键事件外发：待审批需要人工介入，操作者可能不在面板前（失败不阻断）
    if deps.webhook is not None:
        deps.webhook.submit("awaiting_approval", {
            "task_id": task_id, "role": req.role, "trace_id": trace_id,
            "reason": str(verdict.get("reason") or ""),
            "matched_rules": verdict.get("matched_rules") or [],
            "operator": _request_operator(request),
        })
    return {"status": "awaiting_approval", "task_id": task_id,
            "verdict": verdict, "output": None, "artifacts": [], "error": None}


@dataclass
class _PreflightOk:
    """/dispatch 前置关卡的结果：approval 非 None 即被拦截转入待审批。"""
    model: str | None
    provider: dict | None
    context: str | None
    approval: dict | None = None


def _preflight(deps: BrainDeps, req: DispatchReq, request: Request) -> _PreflightOk:
    """/dispatch 与 /dispatch/async 共用前置安全管线。

    预算总闸 → 校验 worker_id →（非 force）security-guard + 全量审计 →
    block 转 _await_approval → force 分支审计 → 装配上下文（引用任务并入 +
    clamp）→ 启发式模型路由 → provider 名解析。返回派发所需上下文；
    approval 非 None 时调用方直接返回它。预算超限与审批语义不同：直接 429
    阻断（新任务无 task_id，不存在转待审批的载体），并落审计流水。
    security_check 可能含 LLM 调用：sync /dispatch 直接调用本函数，async 由
    调用方以 asyncio.to_thread 包裹，避免阻塞 brain 事件循环。
    """
    # 预算总闸：全局/当日超限直接阻断新派发（进行中任务不受影响）
    if deps.budget_store is not None:
        allowed, reason = deps.budget_store.check()
        if not allowed:
            record_security_event(
                task_id=None, stage="dispatch", decision="budget_exceeded",
                operator=_request_operator(request), reason=reason)
            if deps.webhook is not None:
                deps.webhook.submit("budget_exceeded",
                                    {"reason": reason,
                                     "snapshot": deps.budget_store.snapshot()})
            raise HTTPException(status_code=429, detail=reason)
    if req.worker_id:
        validate_id(req.worker_id, "worker_id")
    if not req.force:
        v = security_check(req.prompt, use_llm=deps.cfg.security_llm)
        # 前置关卡审计：block/warn/pass 全量留痕（写入失败不阻断主流程）
        record_security_event(
            task_id=None, stage="dispatch",
            decision="warn" if (v["verdict"] == "pass" and v.get("matched_rules"))
            else v["verdict"],
            matched_rules=v.get("matched_rules"),
            operator=_request_operator(request),
            llm_enhanced=bool(v.get("llm_enhanced", deps.cfg.security_llm)),
            reason=v.get("reason", ""),
        )
        if v["verdict"] == "block":
            return _PreflightOk(None, None, None,
                                _await_approval(deps, req, v, request))
    else:
        # force 强制放行：审计记录决策与来源（web Command「强制放行」按钮走此处）
        record_security_event(
            task_id=None, stage="dispatch", decision="force",
            operator=_request_operator(request),
            llm_enhanced=deps.cfg.security_llm)
    context = req.context_memory
    if deps.memory_store is not None and req.context_task_ids:
        context = assemble_context(deps.memory_store, context, req.context_task_ids)
    # 持久会话：历史轮次注入上下文（连续对话），先会话块再用户自带上下文
    if req.session_id and deps.session_store is not None:
        session_ctx = deps.session_store.history_block(
            req.session_id, task_lookup=deps.task_store.get)
        if session_ctx:
            context = f"{session_ctx}\n\n{context}" if context else session_ctx
    # 知识库：自动检索与 prompt 相关片段（空库/无命中零注入，行为不变）
    if deps.knowledge_store is not None and deps.cfg.knowledge_inject_enabled:
        kb_block = deps.knowledge_store.context_block(req.prompt)
        if kb_block:
            context = f"{context}\n\n{kb_block}" if context else kb_block
    context = clamp_context(context, deps.cfg.context_memory_max_chars)
    model = _route_model(deps, req)
    # 运行时 Provider：名 → 三元组（密钥不落盘）；显式名/默认名不可用 → 400
    model, provider = _resolve_provider(deps, model, req.provider)
    return _PreflightOk(model, provider, context)


# ---- 路由组 ---

def create_auth_router(deps: BrainDeps) -> APIRouter:
    """注册 / 心跳 / 节点列表：worker 生命周期管理。"""
    router = APIRouter()

    @router.post("/register")
    def register(req: RegisterReq):
        validate_id(req.worker_id, "worker_id")
        validate_host(req.host)
        if not (1 <= req.port <= 65535):
            raise ValueError(f"invalid port: {req.port}")
        deps.registry.register(WorkerInfo(
            worker_id=req.worker_id, role=req.role, host=req.host, port=req.port,
            capabilities=req.capabilities, last_heartbeat=0.0,
        ))
        healed = 0
        if deps.retry_manager:
            healed = deps.retry_manager.check_worker_healing(req.worker_id)
        return {"status": "registered", "worker_id": req.worker_id,
                "healed_tasks": healed}

    @router.post("/heartbeat")
    def heartbeat(req: HeartbeatReq):
        known = deps.registry.heartbeat(req.worker_id, req.load)
        return {"status": "ok" if known else "registration_required"}

    @router.get("/workers")
    def list_workers():
        return [asdict(w) for w in deps.registry.active_workers()]

    @router.delete("/workers/{worker_id}")
    def delete_worker(worker_id: str):
        if not deps.registry.unregister(worker_id):
            raise HTTPException(status_code=404, detail="worker not found")
        return {"status": "unregistered", "worker_id": worker_id}

    return router


def create_providers_router(deps: BrainDeps) -> APIRouter:
    """运行时 Provider 池管理（密钥只写 providers.json，gitignore；GET 回显遮蔽）。"""
    router = APIRouter()

    @router.get("/providers")
    def list_providers():
        """遮蔽密钥后的池视图 + 当前默认名（真实密钥绝不回显）。"""
        return deps.provider_store.list_all()

    @router.put("/providers")
    def put_provider(req: _ProviderPutReq):
        name = req.name.strip()
        validate_id(name, "provider name")
        deps.provider_store.upsert(name, model=req.model or "",
                                   base_url=req.base_url or "",
                                   auth_token=req.auth_token or "")
        return {"status": "saved", "name": name}

    @router.delete("/providers/{name}")
    def delete_provider(name: str):
        if not deps.provider_store.delete(name):
            raise HTTPException(status_code=404, detail="provider not found")
        return {"status": "deleted", "name": name}

    @router.get("/providers/default")
    def get_default_provider():
        return {"default": deps.provider_store.default()}

    @router.put("/providers/default")
    def set_default_provider(req: _DefaultProviderReq):
        """热切默认：只影响后续新任务；running/排队任务用已解析值。"""
        name = req.name.strip()
        if name:
            validate_id(name, "provider name")
        try:
            deps.provider_store.set_default(name)
        except KeyError:
            raise HTTPException(status_code=404,
                                detail=f"provider '{name}' 不存在")
        return {"status": "default_set", "default": deps.provider_store.default()}

    return router


def create_tasks_router(deps: BrainDeps) -> APIRouter:
    """任务查询 / 取消 / 重试 / 重派 / 状态 / 回调。"""
    router = APIRouter()

    @router.get("/tasks")
    def list_tasks(worker_id: str | None = None, status: str | None = None):
        recs = (deps.task_store.list_by_worker(worker_id)
                if worker_id else deps.task_store.list_all())
        if status:
            recs = [r for r in recs if r.status == status]
        return [asdict(r) for r in recs]

    @router.get("/tasks/{task_id}")
    def get_task(task_id: str):
        rec = deps.task_store.get(task_id)
        if rec is None:
            raise HTTPException(status_code=404, detail="task not found")
        return asdict(rec)

    @router.post("/tasks/{task_id}/cancel")
    def cancel_task(task_id: str):
        rec = deps.task_store.get(task_id)
        if rec is None:
            raise HTTPException(status_code=404, detail="task not found")
        if rec.status not in ("pending", "running"):
            raise HTTPException(status_code=400,
                                detail=f"无法取消，任务当前状态为 {rec.status}")
        if rec.worker_id:
            w = deps.registry.get_worker(rec.worker_id)
            if w:
                try:
                    deps.http_call("POST", f"http://{w.host}:{w.port}/task/{task_id}/cancel")
                except Exception:  # noqa: BLE001
                    pass
        # 本地执行（ORCH_LOCAL_EXEC）：worker_id=="local"、无远程 worker 可通知，
        # 需直接信号派发器置位该任务的 cancel_event，否则本地执行收不到取消。
        if deps.dispatcher is not None and hasattr(deps.dispatcher, "cancel"):
            try:
                deps.dispatcher.cancel(task_id)
            except Exception:  # noqa: BLE001 - 取消信号失败不阻断状态置终
                pass
        rec = deps.task_store.finalize_if(task_id, ("pending", "running"), status="cancelled")
        if rec is None:
            raise HTTPException(status_code=409, detail="任务已完成或已被其他请求处理")
        # cancelled 属终态：发布 done 事件让 SSE 订阅流关闭
        publish_task_event(deps.bus, task_id, event="done", state="cancelled",
                           trace_id=rec.trace_id, summary="cancelled by request")
        return asdict(rec)

    @router.post("/tasks/{task_id}/retry")
    def retry_task(task_id: str):
        if deps.dispatcher is None:
            raise HTTPException(status_code=400,
                                detail="dispatcher 未配置（local 模式不支持重试）")
        rec = deps.task_store.get(task_id)
        if rec is None:
            raise HTTPException(status_code=404, detail="task not found")
        if rec.status not in ("lost", "failed"):
            raise HTTPException(status_code=400,
                                detail=f"无法重试，任务当前状态为 {rec.status}")
        # 被人工拒绝的指令不允许经 retry 绕过安全关卡重新执行
        rec_meta = rec.meta if isinstance(rec.meta, dict) else {}
        if (rec_meta.get("approval") or {}).get("status") == "rejected":
            raise HTTPException(status_code=400,
                                detail="被人工拒绝的指令不可重试，请修订后重新派发")
        try:
            res = deps.dispatcher.dispatch(role=rec.role, prompt=rec.prompt)
        except RuntimeError as e:
            return {"status": "failed", "error": str(e)}
        deps.task_store.update(rec.task_id, status=res.status,
                               retry_count=rec.retry_count + 1,
                               last_retry_at=str(int(time.time())))
        return {
            "task_id": res.task_id, "status": res.status,
            "output": res.output, "artifacts": res.artifacts, "error": res.error,
            "product_verdict": res.verdict,
        }

    @router.post("/tasks/{task_id}/redispatch")
    def redispatch_task(task_id: str, req: RedispatchReq):
        if deps.dispatcher is None:
            raise HTTPException(status_code=400,
                                detail="dispatcher 未配置（local 模式不支持重派）")
        if req.worker_id:
            validate_id(req.worker_id, "worker_id")
        # 待审批/派发占位中的任务不可重派：需先走 approve/reject 人工裁决流程
        rec0 = deps.task_store.get(task_id)
        if rec0 is None:
            raise HTTPException(status_code=404, detail="task not found")
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
        model, provider = _resolve_provider(deps, req.model, provider_name)
        try:
            res = deps.dispatcher.redispatch(task_id, model=model,
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

    @router.get("/tasks/{task_id}/status")
    def task_status(task_id: str):
        rec = deps.task_store.get(task_id)
        if rec is None:
            raise HTTPException(status_code=404, detail="task not found")
        return {"task_id": rec.task_id, "status": rec.status,
                "worker_id": rec.worker_id, "error": rec.error}

    @router.post("/tasks/{task_id}/callback")
    async def task_callback(task_id: str, data: dict):
        if deps.async_dispatcher is None:
            raise HTTPException(status_code=400, detail="async_dispatcher 未配置")
        handled = await deps.async_dispatcher.handle_callback(task_id, data)
        if not handled:
            rec = deps.task_store.get(task_id)
            if rec is None:
                raise HTTPException(status_code=404, detail="task not found")
        return {"status": "ok", "handled": handled}

    return router


def create_approval_router(deps: BrainDeps) -> APIRouter:
    """人工审批：approve（可修订 prompt 重过安全关卡）/ reject。"""
    router = APIRouter()

    @router.post("/tasks/{task_id}/approve")
    def approve_task(task_id: str, req: ApproveReq, request: Request):
        """人工批准待审批任务：修订 prompt 则重过 security-guard，通过后按原流程派发。"""
        if deps.dispatcher is None:
            raise HTTPException(status_code=400,
                                detail="dispatcher 未配置（local 模式不支持审批派发）")
        rec = deps.task_store.get(task_id)
        if rec is None:
            raise HTTPException(status_code=404, detail="task not found")
        if rec.status != "awaiting_approval":
            raise HTTPException(status_code=400,
                                detail=f"任务当前状态为 {rec.status}，非待审批")
        # 原子占位（compare-and-set）：awaiting_approval -> dispatching。本端点是
        # sync def，在线程池并发运行，「读状态→校验→派发（最长500s）→更新」窗口
        # 内并发 approve 会双重派发同一危险指令；先占位使并发裁决者拿到 409。
        if not deps.task_store.transition(task_id, "awaiting_approval", "dispatching"):
            raise HTTPException(status_code=409,
                                detail="任务已被其他请求处理（并发裁决冲突），请刷新后重试")
        approval = _approval_meta(rec)
        operator = _request_operator(request)
        revised_prompt = (req.prompt or "").strip()
        revised = bool(revised_prompt and revised_prompt != rec.prompt)
        prompt = revised_prompt or rec.prompt
        if revised:
            # 修订后的指令必须重过安全关卡；不通过则回滚占位并更新裁决详情
            v = security_check(prompt, use_llm=deps.cfg.security_llm)
            record_security_event(
                task_id=task_id, stage="approve",
                decision="warn" if (v["verdict"] == "pass" and v.get("matched_rules"))
                else v["verdict"],
                matched_rules=v.get("matched_rules"), operator=operator,
                llm_enhanced=bool(v.get("llm_enhanced", deps.cfg.security_llm)),
                reason=v.get("reason", ""))
            if v["verdict"] == "block":
                deps.task_store.transition(task_id, "dispatching", "awaiting_approval")
                approval["status"] = "pending"
                approval["verdict"] = v
                approval["revised_prompt"] = prompt
                meta = dict(rec.meta or {})
                meta["approval"] = approval
                deps.task_store.update(task_id, meta=meta,
                                       updated_at=str(int(time.time())))
                publish_task_event(deps.bus, task_id, event="status",
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
                operator=operator, llm_enhanced=deps.cfg.security_llm,
                reason="人工批准放行被拦截指令")
        # 按原流程派发：沿用挂起时缓存的 context/model，修订时用修订后 prompt
        context = approval.get("context_memory") or ""
        ctx_task_ids = approval.get("context_task_ids") or []
        if deps.memory_store is not None and ctx_task_ids:
            context = assemble_context(deps.memory_store, context, ctx_task_ids)
        context = clamp_context(context, deps.cfg.context_memory_max_chars)
        model = approval.get("model")
        # Provider：approve 时按缓存名重新解析（不缓存三元组/密钥于 approval meta）；
        # 老任务该键缺失 → 走当前默认 provider（与「新任务」语义一致）
        model, provider = _resolve_provider(deps, model, approval.get("provider"))
        # 定点续传：被拦截时缓存了 worker_id，approve 时恢复对同一节点派发；
        # 老任务该键缺失 → None → 自动选节点（向后兼容）
        worker_id = approval.get("worker_id")
        try:
            with approved_dispatch(prompt):
                res = deps.dispatcher.dispatch(role=rec.role, prompt=prompt,
                                               context_memory=context or None, model=model,
                                               worker_id=worker_id, provider=provider)
        except Exception as e:  # noqa: BLE001 - 派发失败回滚占位，任务回到待审批
            deps.task_store.transition(task_id, "dispatching", "awaiting_approval")
            publish_task_event(deps.bus, task_id, event="status", state="awaiting_approval",
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
        deps.task_store.update(task_id, status="done",
                               output=f"已人工批准并派发为 {res.task_id}",
                               meta=meta, updated_at=str(int(time.time())))
        publish_task_event(deps.bus, task_id, event="done", state="done",
                           trace_id=rec.trace_id,
                           summary=f"approved by {operator or 'operator'} -> {res.task_id}")
        return {"status": "approved", "approved": True, "task_id": task_id,
                "dispatched_task_id": res.task_id, "dispatch_status": res.status}

    @router.post("/tasks/{task_id}/reject")
    def reject_task(task_id: str, req: RejectReq, request: Request):
        """人工拒绝待审批任务：终态 failed + 审计留痕。

        与 approve 的 dispatching 占位互斥：先原子 CAS 到 failed，
        并发 approve/reject 同一任务时落败方拿到 409。
        """
        rec = deps.task_store.get(task_id)
        if rec is None:
            raise HTTPException(status_code=404, detail="task not found")
        if rec.status != "awaiting_approval":
            raise HTTPException(status_code=400,
                                detail=f"任务当前状态为 {rec.status}，非待审批")
        if not deps.task_store.transition(task_id, "awaiting_approval", "failed"):
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
        rec = deps.task_store.update(
            task_id,
            error=req.reason or "指令被安全关卡拦截，人工裁决拒绝执行",
            meta=meta, updated_at=str(int(time.time())))
        record_security_event(
            task_id=task_id, stage="approve", decision="reject",
            matched_rules=approval.get("verdict", {}).get("matched_rules"),
            operator=operator, llm_enhanced=deps.cfg.security_llm,
            reason=req.reason or "人工拒绝执行被拦截指令")
        # failed 属终态：发布 done 事件让 SSE 订阅流关闭
        publish_task_event(deps.bus, task_id, event="done", state="failed",
                           trace_id=rec.trace_id,
                           summary=f"rejected by {operator or 'operator'}")
        return asdict(rec)

    return router


def create_dispatch_router(deps: BrainDeps) -> APIRouter:
    """同步 / 异步派发：共享 _preflight 安全管线。"""
    router = APIRouter()

    @router.post("/dispatch")
    def dispatch(req: DispatchReq, request: Request):
        if deps.dispatcher is None:
            return {"status": "failed",
                    "error": "dispatcher 未配置（local 模式不提供 /dispatch）"}
        pf = _preflight(deps, req, request)
        if pf.approval is not None:
            return pf.approval
        try:
            with approved_dispatch(req.prompt):
                res = deps.dispatcher.dispatch(
                    role=req.role, prompt=req.prompt,
                    context_memory=pf.context or None, model=pf.model,
                    use_experience=req.use_experience,
                    enable_subagents=req.enable_subagents,
                    worker_id=req.worker_id, provider=pf.provider)
        except RuntimeError as e:
            return {"status": "failed", "error": str(e)}
        _record_session_turn(deps, req, res.task_id)
        return {
            "task_id": res.task_id, "status": res.status,
            "output": res.output, "artifacts": res.artifacts, "error": res.error,
            "product_verdict": res.verdict, "model": pf.model,
            "provider": pf.provider["name"] if pf.provider else None,
        }

    @router.post("/dispatch/async")
    async def dispatch_async(req: DispatchReq, request: Request):
        if deps.async_dispatcher is None:
            return {"status": "failed", "error": "async_dispatcher 未配置"}
        # security_llm 同步等待秒级：挪到线程池执行，避免阻塞 brain 事件循环
        # （同步 /dispatch 路径保持原样不动）
        pf = await asyncio.to_thread(_preflight, deps, req, request)
        if pf.approval is not None:
            return pf.approval
        try:
            with approved_dispatch(req.prompt):
                task_id = await deps.async_dispatcher.dispatch_async(
                    role=req.role, prompt=req.prompt,
                    context_memory=pf.context or None, model=pf.model,
                    use_experience=req.use_experience,
                    enable_subagents=req.enable_subagents,
                    worker_id=req.worker_id, provider=pf.provider,
                )
        except RuntimeError as e:
            return {"status": "failed", "error": str(e)}
        _record_session_turn(deps, req, task_id)
        return {"status": "accepted", "task_id": task_id, "model": pf.model,
                "provider": pf.provider["name"] if pf.provider else None}

    return router


def _record_session_turn(deps: BrainDeps, req: DispatchReq, task_id: str) -> None:
    """派发成功后把本轮 prompt+task_id 记入持久会话（失败/缺省零影响）。"""
    if not req.session_id or deps.session_store is None:
        return
    try:
        deps.session_store.append_turn(req.session_id, req.prompt, task_id)
    except KeyError as e:
        log.warning("会话轮次记录被跳过：%s", e)
    except Exception as e:  # noqa: BLE001 - 会话记录失败不拖垮派发
        log.warning("会话轮次记录失败 session=%s: %r", req.session_id, e)


def create_secretary_router(deps: BrainDeps) -> APIRouter:
    """AI 顾问（推荐/解释）与结果翻译：运行于独立上下文，不污染任务上下文。"""
    router = APIRouter()

    @router.post("/recommend")
    async def recommend(req: AdvisorReq, request: Request):
        """AI 推荐/解释顾问：运行于独立上下文，绝不污染任务上下文。"""
        if deps.advisor is None or not getattr(deps.advisor.config, "advisor_enabled", True):
            return {"status": "failed", "error": "advisor 未配置/未启用"}
        # 顾问是同步 + LLM 调用：挪到线程池，避免阻塞 brain 事件循环
        result = await asyncio.to_thread(
            deps.advisor.recommend, req.task, mode=req.mode,
            finished_output=req.finished_output)
        auto_apply = (req.auto_apply if req.auto_apply is not None
                      else bool(getattr(deps.advisor.config, "advisor_auto_apply", False)))
        payload: dict = {
            "status": "ok",
            "recommendation": result.to_dict(),
            "auto_applied": False,
            "dispatched": [],
        }
        if not (auto_apply and result.should_dispatch):
            return payload
        if deps.dispatcher is None:
            payload["error"] = "auto_apply 需要已配置 dispatcher（本地执行或远程）"
            return payload
        role = (result.roles[0] if result.roles else "executor")
        # auto-apply 仍走安全关卡：绝不因推荐而绕过拦截/审批
        v = await asyncio.to_thread(security_check, req.task,
                                    use_llm=deps.cfg.security_llm)
        record_security_event(
            task_id=None, stage="recommend_auto_apply",
            decision="warn" if (v["verdict"] == "pass" and v.get("matched_rules"))
            else v["verdict"],
            matched_rules=v.get("matched_rules"),
            operator=_request_operator(request),
            llm_enhanced=bool(v.get("llm_enhanced", deps.cfg.security_llm)),
            reason=v.get("reason", ""),
        )
        if v["verdict"] == "block":
            payload["dispatched"].append({"role": role, "status": "blocked",
                                          "reason": v.get("reason", "")})
            return payload
        try:
            res = await asyncio.to_thread(
                deps.dispatcher.dispatch, role=role, prompt=req.task,
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

    @router.post("/translate")
    async def translate(req: TranslateReq):
        """结果翻译：思考过程/流式 token 保持原语言，只对给定文本做一次旁路翻译。"""
        if deps.translator is None:
            return {"status": "failed", "error": "translator 未配置"}
        result = await asyncio.to_thread(
            deps.translator.translate, req.text, target_lang=req.target_lang)
        return {"status": "ok", "translation": result.to_dict()}

    return router


def create_sse_router(deps: BrainDeps) -> APIRouter:
    """任务 SSE 实时事件流。"""
    router = APIRouter()

    @router.get("/tasks/{task_id}/events")
    async def task_events(task_id: str):
        """SSE 实时事件流：status（迁移/轮询进展）| callback（回调到达）| done（终态后关闭）。

        EventSource 无法自定义 header，鉴权同时接受 X-API-Key 头与 ?api_key= 查询参数
        （auth 中间件已支持 query key，无需本端点特殊处理）。先补发当前状态快照，
        终态任务直接结束；空闲 15s 发心跳注释行防中间层断开。
        """
        rec = deps.task_store.get(task_id)
        if rec is None:
            raise HTTPException(status_code=404, detail="task not found")

        async def gen():
            sid, q = deps.bus.subscribe(task_id)
            try:
                # 先补发快照（订阅与快照之间的窗口事件已入队兜底），终态则直接收流
                cur = deps.task_store.get(task_id)
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
                deps.bus.unsubscribe(sid)

        return StreamingResponse(gen(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache",
                                          "X-Accel-Buffering": "no"})

    return router


def create_stats_router(deps: BrainDeps) -> APIRouter:
    """全局统计：worker / 任务 / 经验 / 成本 / 质量聚合。"""
    router = APIRouter()

    @router.get("/stats")
    def stats():
        workers = deps.registry.all_workers()
        alive = deps.registry.active_workers()
        tasks = deps.task_store.list_all()
        status_counts: dict[str, int] = {}
        for t in tasks:
            status_counts[t.status] = status_counts.get(t.status, 0) + 1
        recent = sorted(tasks, key=lambda r: r.updated_at, reverse=True)[:10]
        exp_count = len(deps.experience_store.list_all()) if deps.experience_store else 0
        return {
            "mode": deps.cfg.mode,
            "brain_url": deps.cfg.brain_url,
            "heartbeat_timeout": deps.cfg.heartbeat_timeout,
            "worker_total": len(workers),
            "worker_active": len(alive),
            "task_total": len(tasks),
            "task_status": status_counts,
            "experience_total": exp_count,
            "recent_tasks": [asdict(r) for r in recent],
            "cost": _aggregate_costs(tasks),
            "quality": _aggregate_quality(tasks),
        }

    return router


class _BudgetPutReq(BaseModel):
    """PUT /budgets 请求体：None/0 均视为不限制。"""
    global_limit_usd: float | None = None
    daily_limit_usd: float | None = None


def create_budget_router(deps: BrainDeps) -> APIRouter:
    """全局/每日预算：查看快照 + 热调上限（账本 budgets.json 持久化）。"""
    router = APIRouter()

    @router.get("/budgets")
    def get_budgets():
        if deps.budget_store is None:
            raise HTTPException(status_code=400, detail="budget_store 未配置")
        return deps.budget_store.snapshot()

    @router.put("/budgets")
    def put_budgets(req: _BudgetPutReq):
        if deps.budget_store is None:
            raise HTTPException(status_code=400, detail="budget_store 未配置")
        return deps.budget_store.set_limits(
            global_limit_usd=req.global_limit_usd,
            daily_limit_usd=req.daily_limit_usd)

    return router


def _session_detail(deps: BrainDeps, rec: dict, max_output: int = 3000) -> dict:
    """会话详情：轮次联表 TaskRecord，补 status/output/error（读取时联表，
    单一事实源是任务库，无需在终态回写会话）。"""
    turns = []
    for t in rec.get("turns") or []:
        item = dict(t)
        task = None
        try:
            task = deps.task_store.get(t.get("task_id") or "")
        except Exception:  # noqa: BLE001 - 联表失败降级展示
            task = None
        if task is not None:
            item["status"] = task.status
            item["error"] = task.error
            out = str(task.output) if task.output is not None else ""
            item["output"] = out[:max_output]
        else:
            item["status"] = "lost"
        turns.append(item)
    return {**rec, "turns": turns}


def create_sessions_router(deps: BrainDeps) -> APIRouter:
    """持久会话（派发链路连续对话）：创建/列表/详情（轮次联表任务输出）/删除。"""
    router = APIRouter()

    @router.post("/sessions")
    def create_session():
        if deps.session_store is None:
            raise HTTPException(status_code=400, detail="session_store 未配置")
        rec = deps.session_store.create()
        return {"session_id": rec["session_id"]}

    @router.get("/sessions")
    def list_sessions():
        if deps.session_store is None:
            raise HTTPException(status_code=400, detail="session_store 未配置")
        return {"sessions": deps.session_store.list_all()}

    @router.get("/sessions/{session_id}")
    def get_session(session_id: str):
        if deps.session_store is None:
            raise HTTPException(status_code=400, detail="session_store 未配置")
        rec = deps.session_store.get(session_id)
        if rec is None:
            raise HTTPException(status_code=404, detail="session not found")
        return _session_detail(deps, rec)

    @router.delete("/sessions/{session_id}")
    def delete_session(session_id: str):
        if deps.session_store is None:
            raise HTTPException(status_code=400, detail="session_store 未配置")
        if not deps.session_store.delete(session_id):
            raise HTTPException(status_code=404, detail="session not found")
        return {"status": "deleted", "session_id": session_id}

    return router


class _KnowledgePutReq(BaseModel):
    title: str | None = None
    content: str | None = None


def create_knowledge_router(deps: BrainDeps) -> APIRouter:
    """文档知识库（人工上传的确定性资料层；派发时自动检索注入）。"""
    router = APIRouter()

    @router.post("/knowledge")
    def add_knowledge(req: _KnowledgePutReq):
        if deps.knowledge_store is None:
            raise HTTPException(status_code=400, detail="knowledge_store 未配置")
        if not (req.content or "").strip():
            raise HTTPException(status_code=400, detail="content 不能为空")
        return deps.knowledge_store.add(req.title or "", req.content)

    @router.get("/knowledge")
    def list_knowledge():
        if deps.knowledge_store is None:
            raise HTTPException(status_code=400, detail="knowledge_store 未配置")
        return {"docs": deps.knowledge_store.list_all()}

    @router.get("/knowledge/{doc_id}")
    def get_knowledge(doc_id: str):
        if deps.knowledge_store is None:
            raise HTTPException(status_code=400, detail="knowledge_store 未配置")
        doc = deps.knowledge_store.get(doc_id, full=True)
        if doc is None:
            raise HTTPException(status_code=404, detail="doc not found")
        return doc

    @router.put("/knowledge/{doc_id}")
    def update_knowledge(doc_id: str, req: _KnowledgePutReq):
        if deps.knowledge_store is None:
            raise HTTPException(status_code=400, detail="knowledge_store 未配置")
        try:
            return deps.knowledge_store.update(doc_id, title=req.title,
                                               content=req.content)
        except KeyError as e:
            raise HTTPException(status_code=404, detail=str(e))

    @router.delete("/knowledge/{doc_id}")
    def delete_knowledge(doc_id: str):
        if deps.knowledge_store is None:
            raise HTTPException(status_code=400, detail="knowledge_store 未配置")
        if not deps.knowledge_store.delete(doc_id):
            raise HTTPException(status_code=404, detail="doc not found")
        return {"status": "deleted", "doc_id": doc_id}

    @router.post("/knowledge/search")
    def search_knowledge(req: ExperienceSearchReq):
        if deps.knowledge_store is None:
            raise HTTPException(status_code=400, detail="knowledge_store 未配置")
        return {"results": deps.knowledge_store.search(req.query, top_k=req.limit)}

    return router


def create_memory_router(deps: BrainDeps) -> APIRouter:
    """核心记忆 / 任务上下文记忆（MemoryStore 接线）。"""
    router = APIRouter()

    @router.get("/memory/core")
    def list_core_memories():
        if deps.memory_store is None:
            raise HTTPException(status_code=400, detail="memory_store 未配置")
        names = deps.memory_store.list_core()
        return {"memories": [{"name": n, "content": deps.memory_store.read_core(n)}
                             for n in names]}

    @router.get("/memory/core/{name}")
    def get_core_memory(name: str):
        if deps.memory_store is None:
            raise HTTPException(status_code=400, detail="memory_store 未配置")
        content = deps.memory_store.read_core(name)
        if content is None:
            raise HTTPException(status_code=404, detail="memory not found")
        return {"name": name, "content": content}

    @router.put("/memory/core/{name}")
    def put_core_memory(name: str, req: MemoryWriteReq):
        if deps.memory_store is None:
            raise HTTPException(status_code=400, detail="memory_store 未配置")
        deps.memory_store.write_core(name, req.content)
        return {"status": "written", "name": name}

    @router.delete("/memory/core/{name}")
    def delete_core_memory(name: str):
        if deps.memory_store is None:
            raise HTTPException(status_code=400, detail="memory_store 未配置")
        deps.memory_store.delete_core(name)
        return {"status": "deleted", "name": name}

    @router.get("/memory/context/{task_id}")
    def get_context_memory(task_id: str):
        if deps.memory_store is None:
            raise HTTPException(status_code=400, detail="memory_store 未配置")
        notes = {k: deps.memory_store.read_context(task_id, k)
                 for k in deps.memory_store.list_context(task_id)}
        return {"task_id": task_id, "notes": notes}

    @router.put("/memory/context/{task_id}/{key}")
    def put_context_memory(task_id: str, key: str, req: MemoryWriteReq):
        if deps.memory_store is None:
            raise HTTPException(status_code=400, detail="memory_store 未配置")
        deps.memory_store.write_context(task_id, key, req.content)
        return {"status": "written", "task_id": task_id, "key": key}

    @router.delete("/memory/context/{task_id}")
    def archive_context_memory(task_id: str):
        """任务结束归档清理该任务的全部上下文记忆。"""
        if deps.memory_store is None:
            raise HTTPException(status_code=400, detail="memory_store 未配置")
        archived = deps.memory_store.delete_context(task_id)
        return {"status": "archived" if archived else "noop", "task_id": task_id}

    return router


def create_procedural_router(deps: BrainDeps) -> APIRouter:
    """程序记忆演化：技能定义的提案-共识-应用。"""
    router = APIRouter()

    @router.get("/memory/procedural/reflection")
    def procedural_reflection(window: int = 50):
        """近期经验的确定性反思聚合，供大脑起草技能定义修订提案。"""
        if deps.experience_store is None:
            raise HTTPException(status_code=400, detail="experience_store 未配置")
        recs = sorted(deps.experience_store.list_all(),
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

    @router.post("/memory/procedural/proposals")
    def create_procedural_proposal(req: ProceduralProposalReq):
        if deps.procedural_store is None:
            raise HTTPException(status_code=400, detail="procedural_store 未配置")
        try:
            rec = deps.procedural_store.propose(req.target_path, req.content,
                                                req.reason, req.evidence_exp_ids)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        return asdict(rec)

    @router.get("/memory/procedural/proposals")
    def list_procedural_proposals(status: str | None = None):
        if deps.procedural_store is None:
            raise HTTPException(status_code=400, detail="procedural_store 未配置")
        return [asdict(r) for r in deps.procedural_store.list_all(status)]

    @router.post("/memory/procedural/proposals/{proposal_id}/vote")
    def vote_procedural_proposal(proposal_id: str, req: ProceduralVoteReq):
        if deps.procedural_store is None:
            raise HTTPException(status_code=400, detail="procedural_store 未配置")
        try:
            rec = deps.procedural_store.vote(proposal_id, req.voter, req.verdict, req.reason)
        except KeyError as e:
            raise HTTPException(status_code=404, detail=str(e))
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        return asdict(rec)

    @router.delete("/memory/procedural/proposals/{proposal_id}")
    def delete_procedural_proposal(proposal_id: str):
        if deps.procedural_store is None:
            raise HTTPException(status_code=400, detail="procedural_store 未配置")
        if not deps.procedural_store.delete(proposal_id):
            raise HTTPException(status_code=404, detail="proposal not found")
        return {"status": "deleted", "proposal_id": proposal_id}

    return router


def create_experiences_router(deps: BrainDeps) -> APIRouter:
    """经验库查询 / 检索 / 管理。"""
    router = APIRouter()

    @router.get("/experiences")
    def list_experiences(role: str | None = None, successful_only: bool = True):
        if deps.experience_store is None:
            raise HTTPException(status_code=400, detail="experience_store 未配置")
        if role:
            recs = deps.experience_store.list_by_role(role)
        else:
            recs = deps.experience_store.list_all()
        if successful_only:
            recs = [r for r in recs if r.success]
        return [asdict(r) for r in recs]

    @router.post("/experiences/search")
    def search_experiences(req: ExperienceSearchReq):
        if deps.experience_store is None:
            raise HTTPException(status_code=400, detail="experience_store 未配置")
        recs = deps.experience_store.search(
            req.query, role=req.role,
            successful_only=req.successful_only, limit=req.limit,
        )
        return [asdict(r) for r in recs]

    @router.get("/experiences/{exp_id}")
    def get_experience(exp_id: str):
        if deps.experience_store is None:
            raise HTTPException(status_code=400, detail="experience_store 未配置")
        rec = deps.experience_store.get(exp_id)
        if rec is None:
            raise HTTPException(status_code=404, detail="experience not found")
        return asdict(rec)

    @router.delete("/experiences/{exp_id}")
    def delete_experience(exp_id: str):
        if deps.experience_store is None:
            raise HTTPException(status_code=400, detail="experience_store 未配置")
        if not deps.experience_store.delete(exp_id):
            raise HTTPException(status_code=404, detail="experience not found")
        return {"status": "deleted", "exp_id": exp_id}

    @router.post("/experiences/reindex")
    def reindex_experiences():
        if deps.experience_store is None:
            raise HTTPException(status_code=400, detail="experience_store 未配置")
        count = deps.experience_store.reindex()
        return {"status": "ok", "reindexed": count}

    return router


def create_schedules_router(deps: BrainDeps) -> APIRouter:
    """定时任务 CRUD / 开关 / 手动触发。"""
    router = APIRouter()

    @router.post("/schedules")
    def create_schedule(req: ScheduleReq):
        if deps.schedule_store is None:
            raise HTTPException(status_code=400, detail="schedule_store 未配置")
        try:
            compute_next_run(req.cron_expr, time.time())
        except (ValueError, RuntimeError) as e:
            raise HTTPException(status_code=400, detail=str(e))
        sid = f"sched-{uuid.uuid4().hex[:8]}"
        next_run = str(int(compute_next_run(req.cron_expr, time.time())))
        rec = ScheduleRecord(
            schedule_id=sid, name=req.name, cron_expr=req.cron_expr,
            role=req.role, prompt=req.prompt,
            context_memory=req.context_memory, model=req.model,
            provider=req.provider,
            created_at=str(int(time.time())), next_run=next_run,
        )
        deps.schedule_store.write(rec)
        return asdict(rec)

    @router.get("/schedules")
    def list_schedules():
        if deps.schedule_store is None:
            raise HTTPException(status_code=400, detail="schedule_store 未配置")
        return [asdict(r) for r in deps.schedule_store.list_all()]

    @router.delete("/schedules/{schedule_id}")
    def delete_schedule(schedule_id: str):
        if deps.schedule_store is None:
            raise HTTPException(status_code=400, detail="schedule_store 未配置")
        if not deps.schedule_store.delete(schedule_id):
            raise HTTPException(status_code=404, detail="schedule not found")
        return {"status": "deleted", "schedule_id": schedule_id}

    @router.post("/schedules/{schedule_id}/toggle")
    def toggle_schedule(schedule_id: str):
        if deps.schedule_store is None:
            raise HTTPException(status_code=400, detail="schedule_store 未配置")
        rec = deps.schedule_store.toggle_enabled(schedule_id)
        if rec is None:
            raise HTTPException(status_code=404, detail="schedule not found")
        return asdict(rec)

    @router.post("/schedules/{schedule_id}/trigger")
    def trigger_schedule(schedule_id: str):
        if deps.task_scheduler is None:
            raise HTTPException(status_code=400, detail="task_scheduler 未配置")
        ok = deps.task_scheduler.trigger_manual(schedule_id)
        if not ok:
            raise HTTPException(status_code=404,
                                detail="schedule not found or dispatch failed")
        return {"status": "triggered", "schedule_id": schedule_id}

    return router


def create_proxy_router(deps: BrainDeps) -> APIRouter:
    """worker 代理：artifact 拉取 / 节点状态 / 工作区设置。"""
    router = APIRouter()

    def _proxy_worker(worker_id: str) -> str:
        """代理端 worker 解析：未注册 404。"""
        w = next((x for x in deps.registry.all_workers()
                  if x.worker_id == worker_id), None)
        if w is None:
            raise HTTPException(status_code=404, detail=f"worker not found: {worker_id}")
        return f"http://{w.host}:{w.port}"

    @router.get("/proxy/artifact")
    def proxy_artifact(worker_id: str, task_id: str, path: str):
        # 防御性校验：worker 端还会再做一次，这里先快速拒绝明显恶意参数
        validate_id(task_id, "task_id")
        if ".." in path or path.startswith(("/", "\\")) or "\\" in path:
            raise HTTPException(status_code=400, detail="invalid path")
        base = _proxy_worker(worker_id)
        resp = deps.http_call("GET", f"{base}/artifact/{task_id}", params={"path": path})
        if resp.get("status_code") != 200:
            raise HTTPException(status_code=502, detail=f"worker 返回: {resp}")
        return resp.get("json", {})

    @router.get("/proxy/node/status")
    def proxy_node_status(worker_id: str):
        """节点详情（技能/MCP/工作区/run 计数）：经同 key 通道转调 worker /agent/status。"""
        base = _proxy_worker(worker_id)
        resp = deps.http_call("GET", f"{base}/agent/status")
        if resp.get("status_code") != 200:
            raise HTTPException(status_code=502, detail=f"worker 返回: {resp}")
        return resp.get("json", {})

    @router.put("/proxy/node/workspace")
    def proxy_node_workspace(req: NodeWorkspaceProxyReq):
        """规定节点对话工作区：转调 worker PUT /agent/workspace。"""
        base = _proxy_worker(req.worker_id)
        resp = deps.http_call("PUT", f"{base}/agent/workspace", json={"path": req.path})
        sc = resp.get("status_code")
        if sc == 200:
            return resp.get("json", {})
        body = resp.get("json", {})
        if sc == 400:
            raise HTTPException(status_code=400,
                                detail=body.get("detail", body) or f"worker 返回: {resp}")
        raise HTTPException(status_code=502, detail=f"worker 返回: {resp}")

    return router
