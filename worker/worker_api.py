"""worker HTTP 服务：接受任务、查询状态、回传产出文件；启动即向大脑注册并定期心跳。"""
import threading
import time
from pathlib import Path
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from agent_runner import AgentRunner, TaskRequest, TaskResult
from worker_config import WorkerConfig


class TaskBody(BaseModel):
    task_id: str
    prompt: str
    context_memory: str = ""
    model: str | None = None


def register_to_brain(cfg: WorkerConfig, http_post=None) -> bool:
    """向大脑注册本 worker；失败重试 3 次。http_post 可注入便于单测。"""
    import requests
    http_post = http_post or requests.post
    url = f"{cfg.brain_url}/register"
    body = {"worker_id": cfg.worker_id, "role": cfg.role,
            "host": cfg.listen_host, "port": cfg.listen_port, "capabilities": {}}
    for _ in range(3):
        try:
            r = http_post(url, json=body, timeout=5)
            if getattr(r, "status_code", 0) == 200:
                return True
        except Exception:
            pass
        time.sleep(2)
    return False


def start_auto_heartbeat(cfg: WorkerConfig, interval: int | None = None,
                         stop_event: threading.Event | None = None,
                         http_post=None) -> threading.Thread:
    """后台线程：先注册，再按 interval 定期发心跳。daemon，随主进程退出。"""
    http_post = http_post  # 真实默认在循环内懒加载，便于测试注入
    interval = interval or cfg.heartbeat_interval
    stop_event = stop_event or threading.Event()

    def loop():
        import requests
        post = http_post or requests.post
        register_to_brain(cfg, http_post=post)
        hb_url = f"{cfg.brain_url}/heartbeat"
        while not stop_event.is_set():
            try:
                post(hb_url, json={"worker_id": cfg.worker_id, "load": 0}, timeout=5)
            except Exception:
                pass
            stop_event.wait(interval)

    t = threading.Thread(target=loop, daemon=True, name="worker-heartbeat")
    t.start()
    return t


def create_worker_app(runner: AgentRunner, cfg: WorkerConfig) -> FastAPI:
    app = FastAPI()
    state: dict[str, TaskResult] = {}
    lock = threading.Lock()

    # CORS：开发期前端直连 worker 查任务/产物
    origins = __import__("os").environ.get("ORCH_CORS_ORIGINS", "http://localhost:5173")
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[o.strip() for o in origins.split(",") if o.strip()],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    def _execute(body: TaskBody):
        req = TaskRequest(body.task_id, body.prompt, body.context_memory, body.model)
        res = runner.run(req)
        with lock:
            state[body.task_id] = res

    @app.post("/task")
    def post_task(body: TaskBody):
        # 异步执行：起后台线程跑 runner，立即返回 accepted；
        # state 先占位 running 供 brain 轮询，完成或异常时覆盖。
        with lock:
            state[body.task_id] = TaskResult(body.task_id, "running")

        def _bg():
            try:
                _execute(body)
            except Exception as e:
                with lock:
                    state[body.task_id] = TaskResult(
                        body.task_id, "failed", error=f"execute error: {e}")

        threading.Thread(target=_bg, daemon=True, name=f"task-{body.task_id}").start()
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
        # 产物落在 work_dir/{task_id}/ 下，path 相对该子目录
        base = Path(cfg.work_dir).resolve()
        full = (base / task_id / path).resolve()
        # 防路径穿越：必须在 work_dir 内
        try:
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
    if cfg.auto_register:
        start_auto_heartbeat(cfg)
    return create_worker_app(runner, cfg)


if __name__ == "__main__":
    import uvicorn
    from worker_config import load_worker_config

    cfg = load_worker_config()
    uvicorn.run(_build_app, host=cfg.listen_host, port=cfg.listen_port)
