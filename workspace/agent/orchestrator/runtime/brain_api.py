"""大脑 HTTP 服务：接收 worker 注册/心跳，查询任务与 worker，并代理派发。"""
import os
from dataclasses import asdict
from pathlib import Path
from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from task_store import TaskStore
from worker_registry import WorkerRegistry, WorkerInfo
from dispatcher import Dispatcher, RemoteDispatcher, DispatchResult
from config import Config


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


class RedispatchReq(BaseModel):
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
                     dispatcher: Dispatcher | None = None) -> FastAPI:
    """dispatcher 可选：remote 模式传入 RemoteDispatcher，使派发与注册状态同进程。"""
    app = FastAPI()

    # CORS：开发期前端 5173，生产同源；可用 ORCH_CORS_ORIGINS 追加
    origins = os.environ.get("ORCH_CORS_ORIGINS", "http://localhost:5173")
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[o.strip() for o in origins.split(",") if o.strip()],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.post("/register")
    def register(req: RegisterReq):
        registry.register(WorkerInfo(
            worker_id=req.worker_id, role=req.role, host=req.host, port=req.port,
            capabilities=req.capabilities, last_heartbeat=0.0,
        ))
        return {"status": "registered", "worker_id": req.worker_id}

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
        return asdict(task_store.update(task_id, status="cancelled"))

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
        }

    @app.post("/dispatch")
    def dispatch(req: DispatchReq):
        if dispatcher is None:
            return {"status": "failed", "error": "dispatcher 未配置（local 模式不提供 /dispatch）"}
        try:
            res = dispatcher.dispatch(role=req.role, prompt=req.prompt,
                                      context_memory=req.context_memory, model=req.model)
        except RuntimeError as e:
            return {"status": "failed", "error": str(e)}
        return {
            "task_id": res.task_id, "status": res.status,
            "output": res.output, "artifacts": res.artifacts, "error": res.error,
        }

    @app.get("/stats")
    def stats():
        workers = registry.all_workers()
        alive = registry.active_workers()
        tasks = task_store.list_all()
        status_counts: dict[str, int] = {}
        for t in tasks:
            status_counts[t.status] = status_counts.get(t.status, 0) + 1
        recent = sorted(tasks, key=lambda r: r.updated_at, reverse=True)[:10]
        return {
            "mode": cfg.mode,
            "brain_url": cfg.brain_url,
            "heartbeat_timeout": cfg.heartbeat_timeout,
            "worker_total": len(workers),
            "worker_active": len(alive),
            "task_total": len(tasks),
            "task_status": status_counts,
            "recent_tasks": [asdict(r) for r in recent],
        }

    @app.get("/proxy/artifact")
    def proxy_artifact(worker_id: str, task_id: str, path: str):
        w = next((x for x in registry.all_workers() if x.worker_id == worker_id), None)
        if w is None:
            raise HTTPException(status_code=404, detail="worker not found")
        base = f"http://{w.host}:{w.port}"
        resp = _http_call("GET", f"{base}/artifact/{task_id}", params={"path": path})
        if resp.get("status_code") != 200:
            raise HTTPException(status_code=502, detail=f"worker 返回: {resp}")
        return resp.get("json", {})

    # 静态托管前端构建产物（生产同源）；目录不存在则跳过，仅提供 API
    web_dist = os.environ.get("ORCH_WEB_DIST") or _default_web_dist()
    if Path(web_dist).exists():
        app.mount("/", StaticFiles(directory=web_dist, html=True), name="web")

    return app


def _http_call(method: str, url: str, **kwargs) -> dict:
    """真实 HTTP 客户端，供 brain_api 内的 RemoteDispatcher 调 worker。"""
    import requests
    resp = requests.request(method, url, timeout=30, **kwargs)
    try:
        body = resp.json()
    except ValueError:
        body = {"raw": resp.text}
    return {"status_code": resp.status_code, "json": body}


def _build_app() -> FastAPI:
    """从 config 装配 brain_app。remote 模式注入 RemoteDispatcher。"""
    from config import load_config
    cfg = load_config()
    registry = WorkerRegistry(heartbeat_timeout=cfg.heartbeat_timeout)
    store = TaskStore(cfg.task_store_dir)
    dispatcher = None
    if cfg.mode == "remote":
        strategy = os.environ.get("ORCH_SELECT_STRATEGY", "round_robin")
        dispatcher = RemoteDispatcher(task_store=store, registry=registry,
                                      http_call=_http_call, select_strategy=strategy)
    return create_brain_app(registry, store, cfg, dispatcher=dispatcher)


if __name__ == "__main__":
    import uvicorn
    from config import load_config

    cfg = load_config()
    host = "127.0.0.1"
    port = int(cfg.brain_url.rsplit(":", 1)[-1]) if ":" in cfg.brain_url else 7000
    uvicorn.run(_build_app, host=host, port=port)
