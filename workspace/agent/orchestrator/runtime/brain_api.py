"""大脑 HTTP 服务：接收 worker 注册/心跳，查询任务与 worker。"""
from dataclasses import asdict
from fastapi import FastAPI
from pydantic import BaseModel

from task_store import TaskStore
from worker_registry import WorkerRegistry, WorkerInfo
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


def create_brain_app(registry: WorkerRegistry, task_store: TaskStore, cfg: Config) -> FastAPI:
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

    return app


def _build_app() -> FastAPI:
    """从 config 装配 brain_app，供 uvicorn 启动。"""
    from config import load_config
    cfg = load_config()
    registry = WorkerRegistry(heartbeat_timeout=cfg.heartbeat_timeout)
    store = TaskStore(cfg.task_store_dir)
    return create_brain_app(registry, store, cfg)


if __name__ == "__main__":
    import uvicorn
    from config import load_config

    cfg = load_config()
    host = "127.0.0.1"
    port = int(cfg.brain_url.rsplit(":", 1)[-1]) if ":" in cfg.brain_url else 7000
    uvicorn.run(_build_app, host=host, port=port)

