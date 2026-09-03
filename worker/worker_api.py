"""worker HTTP 服务：接受任务、查询状态、回传产出文件；启动即向大脑注册并定期心跳。"""
import os
import sys
import threading
import time
from pathlib import Path
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from agent_runner import AgentRunner, TaskRequest, TaskResult
from worker_config import WorkerConfig

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "workspace" / "agent" / "orchestrator" / "runtime"))
from auth import add_auth_middleware
from safe_id import validate_id


class TaskBody(BaseModel):
    task_id: str
    prompt: str
    context_memory: str = ""
    model: str | None = None
    callback_url: str | None = None


def register_to_brain(cfg: WorkerConfig, http_post=None) -> bool:
    """向大脑注册本 worker；失败重试 3 次。http_post 可注入便于单测。"""
    import requests
    http_post = http_post or requests.post
    url = f"{cfg.brain_url}/register"
    body = {"worker_id": cfg.worker_id, "role": cfg.role,
            "host": cfg.listen_host, "port": cfg.listen_port, "capabilities": {}}
    headers = {"X-API-Key": cfg.api_key} if cfg.api_key else {}
    for _ in range(3):
        try:
            r = http_post(url, json=body, timeout=5, headers=headers)
            if getattr(r, "status_code", 0) == 200:
                return True
        except Exception:
            pass
        time.sleep(2)
    return False


def start_auto_heartbeat(cfg: WorkerConfig, interval: int | None = None,
                         stop_event: threading.Event | None = None,
                         http_post=None, load_fn=None) -> threading.Thread:
    """后台线程：先注册，再按 interval 定期发心跳。daemon，随主进程退出。"""
    interval = interval or cfg.heartbeat_interval
    stop_event = stop_event or threading.Event()

    def loop():
        import requests
        post = http_post or requests.post
        register_to_brain(cfg, http_post=post)
        hb_url = f"{cfg.brain_url}/heartbeat"
        headers = {"X-API-Key": cfg.api_key} if cfg.api_key else {}
        while not stop_event.is_set():
            try:
                post(hb_url, json={"worker_id": cfg.worker_id,
                                   "load": load_fn() if load_fn else 0},
                     timeout=5, headers=headers)
            except Exception:
                pass
            stop_event.wait(interval)

    t = threading.Thread(target=loop, daemon=True, name="worker-heartbeat")
    t.start()
    return t


def _send_callback(callback_url: str, result: "TaskResult | None",
                   api_key: str = "") -> None:
    """任务完成后主动回调 Brain，通知结果。失败静默忽略。"""
    if result is None:
        return
    import requests
    payload = {
        "status": result.status, "output": result.output,
        "artifacts": result.artifacts, "error": result.error,
    }
    headers = {"X-API-Key": api_key} if api_key else {}
    try:
        requests.post(callback_url, json=payload, timeout=10, headers=headers)
    except Exception:
        pass


def create_worker_app(runner: AgentRunner, cfg: WorkerConfig) -> FastAPI:
    app = FastAPI()
    state: dict[str, TaskResult] = {}
    cancelled: set[str] = set()
    lock = threading.Lock()
    state_max = int(os.environ.get("ORCH_WORKER_STATE_MAX", "1000"))

    # CORS：开发期前端直连 worker 查任务/产物
    origins = __import__("os").environ.get("ORCH_CORS_ORIGINS", "http://localhost:5173")
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[o.strip() for o in origins.split(",") if o.strip()],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    add_auth_middleware(app, cfg.api_key)

    # 参数校验（validate_id 等）抛的 ValueError 统一转 400
    from fastapi.responses import JSONResponse

    @app.exception_handler(ValueError)
    def _on_value_error(request, exc):
        return JSONResponse(status_code=400, content={"detail": str(exc)})

    def _record(task_id: str, res: TaskResult):
        with lock:
            state[task_id] = res
            # 已终态结果封顶，防长期运行内存增长；running 不清理
            finished = [tid for tid, r in state.items() if r.status != "running"]
            excess = len(finished) - state_max
            for tid in finished[:excess]:
                del state[tid]
                cancelled.discard(tid)

    def _execute(body: TaskBody):
        req = TaskRequest(body.task_id, body.prompt, body.context_memory, body.model)
        res = runner.run(req)
        _record(body.task_id, res)

    @app.post("/task")
    def post_task(body: TaskBody):
        # 异步执行：起后台线程跑 runner，立即返回 accepted；
        # state 先占位 running 供 brain 轮询，完成或异常时覆盖。
        _record(body.task_id, TaskResult(body.task_id, "running"))

        def _bg():
            try:
                _execute(body)
            except Exception as e:
                _record(body.task_id, TaskResult(
                    body.task_id, "failed", error=f"execute error: {e}"))
            with lock:
                should_callback = body.task_id not in cancelled
                res = state.get(body.task_id)
                cancelled.discard(body.task_id)
            if body.callback_url and should_callback:
                _send_callback(body.callback_url, res, api_key=cfg.api_key)

        threading.Thread(target=_bg, daemon=True, name=f"task-{body.task_id}").start()
        return {"task_id": body.task_id, "status": "accepted"}

    @app.post("/task/{task_id}/cancel")
    def cancel_task(task_id: str):
        with lock:
            cancelled.add(task_id)
            res = state.get(task_id)
            if res is not None:
                state[task_id] = TaskResult(task_id, "cancelled")
        return {"task_id": task_id, "status": "cancelled"}

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
        validate_id(task_id, "task_id")
        # 产物落在 work_dir/{task_id}/ 下，path 相对该子目录
        base = Path(cfg.work_dir).resolve()
        task_base = base / task_id
        full = (task_base / path).resolve()
        # 防路径穿越：必须仍在该任务自己的子目录内（拒绝 ../ 跨任务读取）
        try:
            full.relative_to(task_base)
        except ValueError:
            raise HTTPException(status_code=400, detail="invalid path")
        if not full.is_file():
            raise HTTPException(status_code=404, detail="artifact not found")
        size = full.stat().st_size
        if size > cfg.artifact_max_bytes:
            raise HTTPException(status_code=413, detail=f"artifact too large: {size} bytes")
        data = full.read_bytes()
        try:
            return {"content": data.decode("utf-8"), "encoding": "utf-8", "size": size}
        except UnicodeDecodeError:
            import base64
            return {"content": base64.b64encode(data).decode("ascii"),
                    "encoding": "base64", "size": size}

    def _running_count() -> int:
        with lock:
            return sum(1 for r in state.values() if r.status == "running")

    app.state.get_load = _running_count

    return app


def _build_app():
    """从配置装配 worker_app，Fake runner 默认，可经环境变量切换 Sdk。"""
    import os
    from worker_config import load_worker_config
    from agent_runner import FakeAgentRunner, SdkAgentRunner

    cfg = load_worker_config()
    runner_kind = os.environ.get("ORCH_RUNNER", "fake").lower()
    if runner_kind == "sdk":
        runner = SdkAgentRunner(work_dir=cfg.work_dir, executor_def_path=cfg.executor_def_path,
                                permission_mode=cfg.permission_mode)
    else:
        runner = FakeAgentRunner(work_dir=cfg.work_dir)
    app = create_worker_app(runner, cfg)
    if cfg.auto_register:
        start_auto_heartbeat(cfg, load_fn=app.state.get_load)
    return app


if __name__ == "__main__":
    import uvicorn
    from worker_config import load_worker_config

    cfg = load_worker_config()
    uvicorn.run(_build_app, host=cfg.listen_host, port=cfg.listen_port)
