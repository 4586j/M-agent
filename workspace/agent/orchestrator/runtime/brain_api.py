"""大脑 HTTP 服务：接收 worker 注册/心跳，查询任务与 worker，并代理派发。"""
import os
import time
from dataclasses import asdict
from pathlib import Path
from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from task_store import TaskStore
from worker_registry import WorkerRegistry, WorkerInfo
from dispatcher import Dispatcher, RemoteDispatcher, DispatchResult
from async_dispatcher import AsyncRemoteDispatcher
from config import Config
from security_guard import check_with_llm as security_check
from experience_store import ExperienceStore
from retry_manager import RetryManager
from embedding_model import create_embedding_model
from schedule_store import ScheduleStore, ScheduleRecord, compute_next_run
from task_scheduler import TaskScheduler
from auth import add_auth_middleware
from safe_id import validate_id, validate_host


class RegisterReq(BaseModel):
    worker_id: str
    role: str
    host: str
    port: int
    capabilities: dict = {}


class HeartbeatReq(BaseModel):
    worker_id: str
    load: int = 0


class DispatchReq(BaseModel):
    prompt: str
    role: str = "executor"
    context_memory: str | None = None
    model: str | None = None
    force: bool = False  # 跳过 security-guard 检验，用于放行 block 指令


class RedispatchReq(BaseModel):
    model: str | None = None


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


def create_brain_app(registry: WorkerRegistry, task_store: TaskStore, cfg: Config,
                     dispatcher: Dispatcher | None = None,
                     experience_store: ExperienceStore | None = None,
                     retry_manager: RetryManager | None = None,
                     schedule_store: ScheduleStore | None = None,
                     task_scheduler: TaskScheduler | None = None,
                     async_dispatcher: AsyncRemoteDispatcher | None = None,
                     http_call=None) -> FastAPI:
    """dispatcher 可选：remote 模式传入 RemoteDispatcher，使派发与注册状态同进程。"""
    app = FastAPI()
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

    @app.get("/tasks")
    def list_tasks(worker_id: str | None = None):
        recs = task_store.list_by_worker(worker_id) if worker_id else task_store.list_all()
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
        return asdict(task_store.update(task_id, status="cancelled"))

    @app.post("/tasks/{task_id}/retry")
    def retry_task(task_id: str):
        if dispatcher is None:
            raise HTTPException(status_code=400, detail="dispatcher 未配置（local 模式不支持重试）")
        rec = task_store.get(task_id)
        if rec is None:
            raise HTTPException(status_code=404, detail="task not found")
        if rec.status not in ("lost", "failed"):
            raise HTTPException(status_code=400, detail=f"无法重试，任务当前状态为 {rec.status}")
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
        try:
            res = dispatcher.redispatch(task_id, model=req.model)
        except KeyError as e:
            raise HTTPException(status_code=404, detail=str(e))
        except RuntimeError as e:
            return {"status": "failed", "error": str(e)}
        return {
            "task_id": res.task_id, "status": res.status,
            "output": res.output, "artifacts": res.artifacts, "error": res.error,
            "product_verdict": res.verdict,
        }

    @app.post("/dispatch")
    def dispatch(req: DispatchReq):
        if dispatcher is None:
            return {"status": "failed", "error": "dispatcher 未配置（local 模式不提供 /dispatch）"}
        # security-guard 指令检验；force=true 跳过（用于放行 block 指令）
        if not req.force:
            v = security_check(req.prompt, use_llm=cfg.security_llm)
            if v["verdict"] == "block":
                return {"status": "blocked", "verdict": v, "task_id": None,
                        "output": None, "artifacts": [], "error": None}
        try:
            res = dispatcher.dispatch(role=req.role, prompt=req.prompt,
                                      context_memory=req.context_memory, model=req.model)
        except RuntimeError as e:
            return {"status": "failed", "error": str(e)}
        return {
            "task_id": res.task_id, "status": res.status,
            "output": res.output, "artifacts": res.artifacts, "error": res.error,
            "product_verdict": res.verdict,
        }

    @app.post("/dispatch/async")
    async def dispatch_async(req: DispatchReq):
        if async_dispatcher is None:
            return {"status": "failed", "error": "async_dispatcher 未配置"}
        if not req.force:
            v = security_check(req.prompt, use_llm=cfg.security_llm)
            if v["verdict"] == "block":
                return {"status": "blocked", "verdict": v, "task_id": None}
        try:
            task_id = await async_dispatcher.dispatch_async(
                role=req.role, prompt=req.prompt,
                context_memory=req.context_memory, model=req.model,
            )
        except RuntimeError as e:
            return {"status": "failed", "error": str(e)}
        return {"status": "accepted", "task_id": task_id}

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
        }

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


def _make_async_http_call(api_key: str = ""):
    """创建带 API Key 的异步 HTTP 客户端。"""
    import httpx

    async def _async_http_call(method: str, url: str, **kwargs) -> dict:
        headers = kwargs.pop("headers", {})
        if api_key:
            headers["X-API-Key"] = api_key
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.request(method, url, headers=headers, **kwargs)
            try:
                body = resp.json()
            except ValueError:
                body = {"raw": resp.text}
            return {"status_code": resp.status_code, "json": body}

    return _async_http_call


def _build_app() -> FastAPI:
    """从 config 装配 brain_app。remote 模式注入 RemoteDispatcher。"""
    from config import load_config
    cfg = load_config()
    registry = WorkerRegistry(heartbeat_timeout=cfg.heartbeat_timeout)
    store = TaskStore(cfg.task_store_dir)
    emb_model = create_embedding_model()
    exp_store = ExperienceStore(cfg.experience_store_dir, embedding_model=emb_model)
    dispatcher = None
    retry_mgr = None
    async_disp = None
    sched_store = ScheduleStore(cfg.schedule_store_dir)
    scheduler = None
    if cfg.mode == "remote":
        strategy = os.environ.get("ORCH_SELECT_STRATEGY", "round_robin")
        product_check = (lambda c: security_check(c, use_llm=cfg.security_llm))
        http_call = _make_http_call(cfg.api_key)
        async_http_call = _make_async_http_call(cfg.api_key)
        dispatcher = RemoteDispatcher(task_store=store, registry=registry,
                                      http_call=http_call, select_strategy=strategy,
                                      product_check=product_check,
                                      experience_store=exp_store,
                                      max_retries=cfg.max_retries)
        async_disp = AsyncRemoteDispatcher(
            task_store=store, registry=registry,
            async_http=async_http_call, select_strategy=strategy,
            product_check=product_check, experience_store=exp_store,
            max_retries=cfg.max_retries,
            poll_interval=cfg.async_poll_interval,
            poll_max=cfg.async_poll_max,
            brain_url=cfg.brain_url,
        )
        retry_mgr = RetryManager(
            task_store=store, registry=registry, dispatcher=dispatcher,
            sweeper_interval=cfg.sweeper_interval,
            retry_base_delay=cfg.retry_base_delay,
        )
        retry_mgr.start()
        scheduler = TaskScheduler(schedule_store=sched_store, dispatcher=dispatcher)
        scheduler.start()
    return create_brain_app(registry, store, cfg, dispatcher=dispatcher,
                            experience_store=exp_store, retry_manager=retry_mgr,
                            schedule_store=sched_store, task_scheduler=scheduler,
                            async_dispatcher=async_disp)


if __name__ == "__main__":
    import uvicorn
    from config import load_config

    cfg = load_config()
    # 监听地址：默认 127.0.0.1（单机）；跨主机时设 ORCH_BRAIN_HOST=0.0.0.0 对外开放
    host = os.environ.get("ORCH_BRAIN_HOST", "127.0.0.1")
    port = int(cfg.brain_url.rsplit(":", 1)[-1]) if ":" in cfg.brain_url else 7000
    uvicorn.run(_build_app, host=host, port=port)
