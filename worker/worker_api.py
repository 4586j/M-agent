"""worker HTTP 服务：接受任务、查询状态、回传产出文件；启动即向大脑注册并定期心跳。"""
import logging
import os
import sys
import threading
import time
from pathlib import Path

# 项目根入 sys.path（必须在 `from agent_runner import ...` 之前：其模块级
# re-export 已引用 agent_core.tools/context）。否则 `python -m worker_api`
# 从 worker/ 启动时，进程里 PYTHONPATH / editable install 的其他 agent_core
# 会抢占，导致面板与 SDK 执行拿到陈旧内核代码（如 run_oneshot 无 run_id）。
# 与 worker_config 基于 __file__ 锚定仓库默认路径的约定一致，保证本仓库内核
# 永远优先，与进程 CWD 无关。
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from agent_runner import AgentRunner, TaskRequest, TaskResult
from worker_config import WorkerConfig

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "workspace" / "agent" / "orchestrator" / "runtime"))
from auth import add_auth_middleware
from safe_id import validate_id

log = logging.getLogger(__name__)


class TaskBody(BaseModel):
    task_id: str
    prompt: str
    context_memory: str = ""
    model: str | None = None
    callback_url: str | None = None
    trace_id: str | None = None  # brain 派发时生成的链路追踪 ID，日志与 callback 透传
    # 运行时 Provider 四字段（brain 解析后透传；密钥只走本请求体/内存，不落盘）：
    # provider = provider 名（进 meta 对账）；provider_base_url/auth_token = 解析后的
    # 端点/密钥（仅本次任务生效，SdkAgentRunner.run 并入 persona）；provider_pool =
    # 自动故障切换候选（有池才切）。
    provider: str | None = None
    provider_base_url: str | None = None
    provider_auth_token: str | None = None
    provider_pool: list[dict] | None = None


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
                   api_key: str = "", trace_id: str | None = None) -> None:
    """任务完成后主动回调 Brain，通知结果。失败静默忽略。

    trace_id 作为顶层字段回传（不依赖 agent_core 结果 meta），供 brain 关联
    派发链路；meta 为 worker 执行元数据（usage/cost_usd/num_turns 等），
    brain 终态化时入库并在 /tasks 与 /stats 透出。
    """
    if result is None:
        return
    import requests
    payload = {
        "status": result.status, "output": result.output,
        "artifacts": result.artifacts, "error": result.error,
        "meta": result.meta or {},
    }
    if trace_id:
        payload["trace_id"] = trace_id
    headers = {"X-API-Key": api_key} if api_key else {}
    try:
        requests.post(callback_url, json=payload, timeout=10, headers=headers)
    except Exception:
        pass


def create_worker_app(runner: AgentRunner, cfg: WorkerConfig) -> FastAPI:
    app = FastAPI()
    state: dict[str, TaskResult] = {}
    cancelled: set[str] = set()
    cancel_events: dict[str, threading.Event] = {}
    lock = threading.Lock()
    state_max = int(os.environ.get("ORCH_WORKER_STATE_MAX", "1000"))

    # CORS：开发期前端直连 worker 查任务/产物
    origins = os.environ.get("ORCH_CORS_ORIGINS", "http://localhost:5173")
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[o.strip() for o in origins.split(",") if o.strip()],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # 面板静态页（/panel、/）无需鉴权即公开（页面本身只是入口，/agent/* 仍受控）
    add_auth_middleware(app, cfg.api_key, public_paths={"/", "/panel"})

    # 节点 Agent 面板：把单 agent 控制台挂到本节点（ORCH_WORKER_PANEL=0 关闭）
    if cfg.worker_panel:
        from agent_panel import build_agent_panel_router
        app.include_router(build_agent_panel_router(cfg))

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
                cancel_events.pop(tid, None)

    def _execute(body: TaskBody):
        event = threading.Event()
        with lock:
            cancel_events[body.task_id] = event
        req = TaskRequest(body.task_id, body.prompt, body.context_memory,
                          body.model, cancel_event=event, trace_id=body.trace_id,
                          provider=body.provider,
                          provider_base_url=body.provider_base_url,
                          provider_auth_token=body.provider_auth_token,
                          provider_pool=body.provider_pool)
        log.info("[trace=%s] task=%s execute start (model=%s)",
                 body.trace_id, body.task_id, body.model)
        res = runner.run(req)
        # cancel 竞态保护：执行期间被取消 → 终态保持 cancelled，
        # 不被 runner 迟到的 done/failed 覆盖（产物/输出保留在 res 中备查）
        with lock:
            if body.task_id in cancelled:
                res.status = "cancelled"
        _record(body.task_id, res)
        log.info("[trace=%s] task=%s execute end (status=%s)",
                 body.trace_id, body.task_id, res.status)

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
                _send_callback(body.callback_url, res, api_key=cfg.api_key,
                               trace_id=body.trace_id)

        threading.Thread(target=_bg, daemon=True, name=f"task-{body.task_id}").start()
        return {"task_id": body.task_id, "status": "accepted"}

    @app.post("/task/{task_id}/cancel")
    def cancel_task(task_id: str):
        with lock:
            cancelled.add(task_id)
            res = state.get(task_id)
            if res is not None:
                state[task_id] = TaskResult(task_id, "cancelled")
        # 传播到执行体：runner 在消息/turn 边界协作退出
        with lock:
            ev = cancel_events.get(task_id)
        if ev is not None:
            ev.set()
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
            "meta": res.meta,
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
    if cfg.skillhub_auto:
        # skillhub 自动同步：把本项目 catalog 里 verified 技能对账到自己。
        # 只修技能不碰 MCP；任何失败仅告警，不阻断启动。
        try:
            from agent_core.skillhub import auto_sync
        except Exception as e:  # 模块缺失/导入失败也降级为告警
            log.warning("skillhub 自动同步不可用（%s）", e)
        else:
            rep = auto_sync(cfg.project_root)
            if rep.get("error"):
                log.warning("skillhub 自动同步失败：%s", rep["error"])
            elif rep.get("refused"):
                log.warning("skillhub 自动同步：%s 个技能被拒绝（源漂移？先 "
                            "skillhub update）：%s", len(rep["refused"]),
                            list(rep["refused"]))
            elif rep.get("installed"):
                log.info("skillhub 自动同步：已补齐/修复 %s",
                         ", ".join(rep["installed"]))
    runner_kind = os.environ.get("ORCH_RUNNER", "fake").lower()
    if runner_kind == "sdk":
        # 人设文件缺失 = 配置错误，启动即失败并列出可用人设（fail-fast）。
        # 只在这里检查而非 load_worker_config：FakeAgentRunner 不读人设，
        # WorkerConfig() 直接构造也不应被文件存在性绑架。
        _persona = Path(cfg.persona_def_path)
        if not _persona.is_file():
            _agents_dir = Path(cfg.project_root) / ".claude" / "agents"
            _available = sorted(
                p.name for p in _agents_dir.glob("*.md")) if _agents_dir.is_dir() else []
            raise RuntimeError(
                f"人设文件不存在: {cfg.persona_def_path}（role={cfg.role}）。"
                f"当前 .claude/agents/ 可用: {_available or '无'}。"
                "设置 ORCH_WORKER_ROLE 指定人设名，或用 ORCH_WORKER_PERSONA_DEF 指定文件路径。")
        from agent_runner import load_mcp_servers
        runner = SdkAgentRunner(work_dir=cfg.work_dir, persona_def_path=cfg.persona_def_path,
                                permission_mode=cfg.permission_mode,
                                project_root=cfg.project_root,
                                task_timeout=cfg.task_timeout,
                                max_turns=cfg.max_turns,
                                max_thinking_tokens=cfg.max_thinking_tokens or None,
                                task_budget_usd=cfg.task_budget_usd or None,
                                experience_store_dir=cfg.experience_store_dir,
                                output_guard_enabled=cfg.output_guard_enabled,
                                output_guard_max_retries=cfg.output_guard_max_retries,
                                fallback_model=cfg.fallback_model,
                                mcp_servers=load_mcp_servers(cfg.mcp_config_path))
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
