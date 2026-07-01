"""大脑 HTTP 服务：接收 worker 注册/心跳，查询任务与 worker，并代理派发。"""
from dataclasses import asdict
from fastapi import FastAPI
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


def create_brain_app(registry: WorkerRegistry, task_store: TaskStore, cfg: Config,
                     dispatcher: Dispatcher | None = None) -> FastAPI:
    """dispatcher 可选：remote 模式传入 RemoteDispatcher，使派发与注册状态同进程。"""
    app = FastAPI()

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

    @app.get("/tasks")
    def list_tasks(worker_id: str | None = None):
        if worker_id:
            recs = task_store.list_by_worker(worker_id)
        else:
            recs = task_store._all()
        return [asdict(r) for r in recs]

    @app.get("/workers")
    def list_workers():
        return [asdict(w) for w in registry.active_workers()]

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
        dispatcher = RemoteDispatcher(task_store=store, registry=registry, http_call=_http_call)
    return create_brain_app(registry, store, cfg, dispatcher=dispatcher)


if __name__ == "__main__":
    import uvicorn
    from config import load_config

    cfg = load_config()
    host = "127.0.0.1"
    port = int(cfg.brain_url.rsplit(":", 1)[-1]) if ":" in cfg.brain_url else 7000
    uvicorn.run(_build_app, host=host, port=port)
