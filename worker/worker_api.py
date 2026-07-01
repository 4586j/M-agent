"""worker HTTP 服务：接受任务、查询状态、回传产出文件。"""
import threading
from pathlib import Path
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from agent_runner import AgentRunner, TaskRequest, TaskResult
from worker_config import WorkerConfig


class TaskBody(BaseModel):
    task_id: str
    prompt: str
    context_memory: str = ""
    model: str | None = None


def create_worker_app(runner: AgentRunner, cfg: WorkerConfig) -> FastAPI:
    app = FastAPI()
    state: dict[str, TaskResult] = {}
    lock = threading.Lock()

    def _execute(body: TaskBody):
        req = TaskRequest(body.task_id, body.prompt, body.context_memory, body.model)
        res = runner.run(req)
        with lock:
            state[body.task_id] = res

    @app.post("/task")
    def post_task(body: TaskBody):
        # 同步执行（Fake 即时）；真实 SDK 可在此起线程异步
        _execute(body)
        return {"task_id": body.task_id, "status": "accepted"}

    @app.get("/task/{task_id}")
    def get_task(task_id: str):
        with lock:
            res = state.get(task_id)
        if res is None:
            raise HTTPException(status_code=404, detail="task not found")
        return {
            "task_id": res.task_id, "status": res.status,
            "output": res.output, "artifacts": res.artifacts,
            "error": res.error, "memory_candidates": res.memory_candidates,
        }

    @app.get("/artifact/{task_id}")
    def get_artifact(task_id: str, path: str):
        full = Path(cfg.work_dir) / path
        # 防路径穿越
        try:
            full = full.resolve()
            base = Path(cfg.work_dir).resolve()
            full.relative_to(base)
        except ValueError:
            raise HTTPException(status_code=400, detail="invalid path")
        if not full.exists():
            raise HTTPException(status_code=404, detail="artifact not found")
        return {"content": full.read_text(encoding="utf-8")}

    return app


def _build_app():
    """从配置装配 worker_app，Fake runner 默认，可经环境变量切换 Sdk。"""
    import os
    from worker_config import load_worker_config
    from agent_runner import FakeAgentRunner, SdkAgentRunner

    cfg = load_worker_config()
    runner_kind = os.environ.get("ORCH_RUNNER", "fake").lower()
    if runner_kind == "sdk":
        runner = SdkAgentRunner(work_dir=cfg.work_dir, executor_def_path=cfg.executor_def_path)
    else:
        runner = FakeAgentRunner(work_dir=cfg.work_dir)
    return create_worker_app(runner, cfg)


if __name__ == "__main__":
    import uvicorn
    from worker_config import load_worker_config

    cfg = load_worker_config()
    uvicorn.run(_build_app, host=cfg.listen_host, port=cfg.listen_port)

