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
