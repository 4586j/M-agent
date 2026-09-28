"""worker 节点 Agent 面板：把 agent_core 单 agent 控制台挂到每个编排节点上。

面板页复用 ``agent_core/panel/index.html``（单文件深色 UI），API 面与
``agent_core.service`` 对齐（run/stream/async、runs、events、workspace、
terminal、trace）。内核配方与 brain 派发的任务执行共用
``agent_runner.build_worker_kernel``，保证「同一节点、同一人设/模型/权限/
工作区」；面板 run 与 /task 派发各自独立 kernel，并行无串扰。

鉴权口径与 worker_api 其余端点一致：
- 静态面板页（``/panel``、``/``）经 auth 的 public_paths 公开；
- ``/agent/*`` 走通用 auth（X-API-Key 或 ``?api_key=``；未配 key 时仅环回直通）。
  面板页本身在浏览器侧从 ``localStorage.orch_api_key`` 自动附加 ``?api_key=``
  （含内嵌终端 WS），与单机 agent_core.service 面板同款行为。

执行层抽象 PanelExecutor 可注入测试替身（不触发真实内核/LLM）。
"""
import asyncio
import dataclasses
import json
import logging
import os
import re
import subprocess
import threading
import time
import uuid
from dataclasses import asdict
from pathlib import Path
from typing import Any, AsyncIterator

from fastapi import APIRouter, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, RedirectResponse, StreamingResponse
from pydantic import BaseModel, Field

from agent_core.errors import humanize_sdk_error
from agent_core.kernel import AgentEvent
from agent_core.persona import Persona, load_persona
from agent_runner import build_worker_kernel, scan_skills, load_mcp_servers

log = logging.getLogger(__name__)

# 注意：agent_core.service 在模块级执行 `app = create_app(...)`（import 副作用），
# 所以它的工具函数 _clamp_sse_strings / _scan_outside_paths 只做**惰性**导入，
# 避免 worker 进程引入整套单 agent 服务的装配开销与会话状态。


def _clamp_sse_strings(value, limit):
    from agent_core.service import _clamp_sse_strings as _fn
    return _fn(value, limit)


def _scan_outside_paths(text, root=None):
    from agent_core.service import _scan_outside_paths as _fn
    return _fn(text, root)


def _panel_html_path() -> Path:
    """单 agent 面板静态页（agent_core/panel/index.html）。

    以本模块位置锚定仓库（worker/ 上一级 = 项目根），避免进程里 PYTHONPATH /
    editable install 的其他 agent_core 抢占——`from agent_core import` 在
    `python -m worker_api` 下解析到陈旧安装（无 panel/）会令 /panel 404。
    与 worker_config 里基于 __file__ 锚定默认路径的约定一致。
    """
    return Path(__file__).resolve().parent.parent / "agent_core" / "panel" / "index.html"


# ---------------------------------------------------------------------------
# PanelExecutor：面板 run 的执行抽象（可注入测试替身）
# ---------------------------------------------------------------------------


class PanelExecutor:
    """面板 run 的执行抽象。``KernelPanelExecutor`` 委托 build_worker_kernel。

    - ``stream_events``：产出 AgentEvent 流（SSE 用），内部经 sink 持久化事件；
    - ``oneshot``：同步执行一轮，返回 (KernelResult dict, status)；内部同样经
      sink 持久化事件（含 seq）。
    sink(run_id, dict) 由路由提供，把 ``{seq,ts,type,data}`` 追加到 run JSONL。
    """

    def stream_events(self, *, prompt: str, persona: Persona, cwd: str,
                      run_id: str, sink: Any) -> AsyncIterator[AgentEvent]:
        raise NotImplementedError

    def oneshot(self, *, prompt: str, persona: Persona, cwd: str,
                cancel_event: threading.Event, run_id: str,
                sink: Any) -> tuple[dict, str]:
        raise NotImplementedError


class KernelPanelExecutor(PanelExecutor):
    """真实实现：按规划开关缓存两个内核（plain / planning_override），
    面板 run 复用而非每次重建（同节点同配置；内核自身在 run 起始重置
    _last_progress，_build_options 指纹缓存对复用是安全的）。"""

    def __init__(self, cfg):
        self.cfg = cfg
        self._plain_kernel: Any | None = None
        self._planning_kernel: Any | None = None

    def _kernel_for(self, planning: bool):
        if planning:
            if self._planning_kernel is None:
                kernel = build_worker_kernel(self.cfg)
                # 请求级规划开关：replace 出独立 config 对象，绝不改写共享内核
                kernel.config = dataclasses.replace(kernel.config,
                                                    planning_enabled=True)
                self._planning_kernel = kernel
            return self._planning_kernel
        if self._plain_kernel is None:
            self._plain_kernel = build_worker_kernel(self.cfg)
        return self._plain_kernel

    def stream_events(self, *, prompt, persona, cwd, run_id, sink):
        return self._kernel_for(planning=False).stream(
            prompt, persona, cwd, run_id=run_id, event_sink=sink)

    def oneshot(self, *, prompt, persona, cwd, cancel_event, run_id, sink):
        kernel = self._kernel_for(planning=False)
        result = kernel.run_oneshot(
            prompt=prompt, persona=persona, cwd=cwd,
            cancel_event=cancel_event, run_id=run_id, event_sink=sink)
        d = asdict(result)
        status = ("cancelled" if result.cancelled
                  else "failed" if result.is_error else "completed")
        return d, status


# ---------------------------------------------------------------------------
# 请求模型
# ---------------------------------------------------------------------------


class RunReq(BaseModel):
    prompt: str
    model: str | None = None
    cwd: str | None = None
    context_memory: str = ""
    planning_enabled: bool = False


class WorkspaceReq(BaseModel):
    path: str = Field(..., description="工作区绝对路径（须已存在的目录）")


# ---------------------------------------------------------------------------
# 路由工厂
# ---------------------------------------------------------------------------


def build_agent_panel_router(cfg, executor: PanelExecutor | None = None) -> APIRouter:
    """装配 worker 节点 Agent 面板路由。

    cfg：WorkerConfig（决定人设路径 / 工作区 / 内核配方 / 鉴权）。
    executor：PanelExecutor 实现，测试可注入 FakePanelExecutor（不烧 LLM）。
    """
    executor = executor or KernelPanelExecutor(cfg)
    router = APIRouter()

    # 每个 worker 一个面板状态：workspace（独立于 agent_core 的服务级工作区）
    work_root = Path(cfg.work_dir).resolve()
    state = {
        "workspace": work_root,
        "runs": {},  # run_id -> {status, prompt, persona, cwd, created_at, result, error, cancel_event, planning}
    }
    runs_dir = work_root / "agent_runs"
    runs_dir.mkdir(parents=True, exist_ok=True)

    # ---- 小工具（与 agent_core.service 同款但 worker 自持） ----

    def _runs_dir() -> Path:
        return runs_dir

    def _run_meta_path(run_id: str) -> Path:
        return runs_dir / f"{run_id}.meta.json"

    def _write_run_meta(run_id: str, **updates: Any) -> None:
        p = _run_meta_path(run_id)
        meta: dict = {}
        if p.exists():
            try:
                meta = json.loads(p.read_text(encoding="utf-8"))
            except Exception:  # noqa: BLE001
                meta = {}
        meta.update(updates)
        meta["run_id"] = run_id
        p.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")

    def _make_event_sink(run_id: str):
        path = runs_dir / f"{run_id}.jsonl"

        def sink(rid: str, event: dict) -> None:
            with open(path, "a", encoding="utf-8") as f:
                f.write(json.dumps(event, ensure_ascii=False, default=str) + "\n")

        return sink

    def _prepare_cwd(explicit: str | None) -> str:
        """面板任务目录：显式指定须在工作区内；缺省在 <workspace>/.agent_work 下开子目录。"""
        ws_root = state["workspace"]
        if explicit:
            p = Path(explicit).expanduser().resolve()
            if not (p == ws_root or ws_root in p.parents):
                raise HTTPException(status_code=400,
                                    detail=f"cwd 越出工作区（{ws_root}）：{p}")
            p.mkdir(parents=True, exist_ok=True)
            return str(p)
        base = ws_root / ".agent_work"
        p = base / f"panel-{uuid.uuid4().hex[:8]}"
        p.mkdir(parents=True, exist_ok=True)
        return str(p)

    def _resolve_persona(model: str | None) -> Persona:
        """加载节点人设（.claude/agents/{role}.md），请求级 model 覆盖。"""
        from agent_core.persona import replace_persona
        persona = load_persona(cfg.persona_def_path)
        if model:
            persona = replace_persona(persona, model=model)
        return persona

    # ---- SSE 帧格式化（与 agent_core.service 对齐） ----

    def _event_payload(ev: AgentEvent) -> dict:
        if ev.type == "text_delta":
            return {"text": ev.data if isinstance(ev.data, str) else str(ev.data)}
        if ev.type == "tool_use":
            d = ev.data if isinstance(ev.data, dict) else {"name": ev.data}
            return {"name": d.get("name"), "input": d.get("input")}
        if ev.type == "tool_result":
            return {"content": ev.data}
        if ev.type in ("budget_warning", "result", "error"):
            return dict(ev.data) if isinstance(ev.data, dict) else {"detail": str(ev.data)}
        return {"data": ev.data}

    def _sse_format(event_type: str, payload: Any, limit: int = 2000) -> str:
        data = json.dumps(_clamp_sse_strings(payload, limit),
                          ensure_ascii=False, default=str)
        return f"event: {event_type}\ndata: {data}\n\n"

    async def _sse_stream(events: AsyncIterator[AgentEvent]):
        try:
            async for ev in events:
                yield _sse_format(ev.type, _event_payload(ev))
        except Exception as e:  # noqa: BLE001
            yield _sse_format("error", {"message": f"流式转发异常：{e}"})

    def _streaming_response(events) -> StreamingResponse:
        return StreamingResponse(
            _sse_stream(events), media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    # ---- 端点 ----

    @router.get("/health")
    def health() -> dict:
        return {"status": "ok", "service": cfg.worker_id}

    @router.post("/agent/run/stream")
    async def run_stream(req: RunReq):
        persona = _resolve_persona(req.model)
        cwd = _prepare_cwd(req.cwd)
        run_id = f"run-{uuid.uuid4().hex[:12]}"
        _write_run_meta(run_id, cwd=cwd, prompt=req.prompt[:2000],
                        persona=persona.name, model=req.model or "",
                        status="running", started_at=round(time.time(), 3))
        sink = _make_event_sink(run_id)
        events = executor.stream_events(prompt=req.prompt, persona=persona,
                                        cwd=cwd, run_id=run_id, sink=sink)

        async def _with_bookkeeping():
            final: dict | None = None
            try:
                async for ev in events:
                    if ev.type == "result" and isinstance(ev.data, dict):
                        final = dict(ev.data)
                    yield ev
            except Exception as e:  # noqa: BLE001
                _write_run_meta(run_id, status="failed", error=f"流式转发异常：{e}")
                yield AgentEvent("error", f"流式转发异常：{e}")
            finally:
                if final is not None:
                    _write_run_meta(
                        run_id,
                        status="failed" if final.get("is_error") else "completed",
                        num_turns=final.get("num_turns"), cost_usd=final.get("cost_usd"),
                        duration_ms=final.get("duration_ms"))
                else:
                    _write_run_meta(run_id, status="interrupted")

        return _streaming_response(_with_bookkeeping())

    @router.post("/agent/run/async", status_code=202)
    async def run_async(req: RunReq):
        persona = _resolve_persona(req.model)
        cwd = _prepare_cwd(req.cwd)
        run_id = uuid.uuid4().hex[:12]
        cancel_event = threading.Event()
        entry: dict = {
            "run_id": run_id, "status": "running", "prompt": req.prompt,
            "persona": persona.name, "cwd": cwd, "cancel_event": cancel_event,
            "planning_enabled": req.planning_enabled,
            "result": None, "error": None, "task": None,
            "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        state["runs"][run_id] = entry
        _write_run_meta(run_id, cwd=cwd, prompt=req.prompt[:2000],
                        persona=persona.name, model=req.model or "",
                        planning_enabled=req.planning_enabled,
                        status="running", started_at=round(time.time(), 3))
        sink = _make_event_sink(run_id)

        async def _worker():
            try:
                result, status = await asyncio.to_thread(
                    executor.oneshot, prompt=req.prompt, persona=persona,
                    cwd=cwd, cancel_event=cancel_event, run_id=run_id, sink=sink)
                entry["result"] = result
                entry["status"] = status
                _write_run_meta(run_id, status=status)
            except Exception as e:  # noqa: BLE001
                entry["status"] = "failed"
                error_type, message, hint = humanize_sdk_error(e)
                entry["error"] = {"code": error_type, "message": message,
                                  "hint": hint, "raw": str(e)}
                _write_run_meta(run_id, status="failed",
                                error=message or str(e))

        entry["task"] = asyncio.create_task(_worker())
        return {"run_id": run_id, "status": "running"}

    @router.get("/agent/run/{run_id}")
    def run_get(run_id: str):
        entry = state["runs"].get(run_id)
        if entry is None:
            raise HTTPException(status_code=404, detail=f"run not found: {run_id}")
        resp: dict = {"run_id": run_id, "status": entry["status"],
                      "persona": entry["persona"], "prompt": entry["prompt"],
                      "cwd": entry["cwd"], "created_at": entry["created_at"]}
        if entry.get("result") is not None:
            resp["result"] = entry["result"]
        if entry.get("error") is not None:
            resp["error"] = entry["error"]
        return resp

    @router.delete("/agent/run/{run_id}")
    def run_cancel(run_id: str):
        entry = state["runs"].get(run_id)
        if entry is None:
            raise HTTPException(status_code=404, detail=f"run not found: {run_id}")
        entry["cancel_event"].set()
        return {"run_id": run_id, "status": entry["status"],
                "cancel_requested": True}

    @router.get("/agent/run/{run_id}/events")
    def run_events(run_id: str, limit: int = 5000):
        events_path = runs_dir / f"{run_id}.jsonl"
        if not events_path.exists():
            raise HTTPException(status_code=404, detail=f"run 事件不存在: {run_id}")
        out = []
        with open(events_path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except ValueError:
                    continue
        return out[:max(0, limit)]

    @router.get("/agent/runs")
    def list_runs(limit: int = 50):
        items = []
        for meta_path in sorted(runs_dir.glob("*.meta.json"),
                                key=lambda p: p.stat().st_mtime,
                                reverse=True)[:max(0, limit)]:
            try:
                items.append(json.loads(meta_path.read_text(encoding="utf-8")))
            except Exception:  # noqa: BLE001
                continue
        return {"runs": items}

    @router.get("/agent/trace/{run_id}")
    def trace(run_id: str):
        """无 trace 树（面板 run 走流式/异步，不落 trace），从事件流末帧还原终态统计。"""
        events_path = runs_dir / f"{run_id}.jsonl"
        if not events_path.exists():
            raise HTTPException(status_code=404, detail=f"运行记录不存在: {run_id}")
        last_result = None
        with open(events_path, encoding="utf-8") as f:
            for line in f:
                try:
                    e = json.loads(line)
                except ValueError:
                    continue
                if e.get("type") == "result":
                    last_result = e.get("data")
        if last_result is None:
            return {"run_id": run_id, "trace": None, "summary": None,
                    "note": "该 run 无终态 result 事件"}
        return {"run_id": run_id, "trace": None,
                "summary": {k: last_result.get(k) for k in
                            ("num_turns", "cost_usd", "duration_ms", "is_error")},
                "note": "该 run 未落 trace 树，以上为终态统计"}

    @router.get("/agent/workspace")
    def get_workspace() -> dict:
        ws = state["workspace"]
        return {"path": str(ws), "name": ws.name or str(ws), "exists": ws.is_dir()}

    @router.put("/agent/workspace")
    def set_workspace(req: WorkspaceReq) -> dict:
        p = Path(req.path).expanduser().resolve()
        if not p.is_dir():
            raise HTTPException(status_code=400, detail=f"目录不存在: {req.path}")
        state["workspace"] = p
        log.info("[%s] 工作区已切换为 %s", cfg.worker_id, p)
        return {"path": str(p), "name": p.name or str(p)}

    @router.get("/agent/status")
    def agent_status() -> dict:
        """节点详情：技能 / MCP 挂载 / 当前工作区 / 面板 run 计数。

        供编排面板的 brain 代理（/proxy/node/status）拉取；不依赖 executor
        （Fake runner 下同样可读），skills 与 mcp 取与 kernel 注入同源的扫描结果。
        """
        skills = scan_skills(cfg.project_root)
        mcp = load_mcp_servers(cfg.mcp_config_path) or {}
        ws = state["workspace"]
        return {
            "worker_id": cfg.worker_id,
            "role": cfg.role,
            "panel_enabled": cfg.worker_panel,
            "workspace": {"path": str(ws), "name": ws.name or str(ws),
                          "exists": ws.is_dir()},
            "skills": [{"name": s["name"], "description": s["description"],
                        "triggers": s.get("triggers", [])} for s in skills],
            "mcp_servers": [{"name": k, "command": v.get("command"), "url": v.get("url")}
                            for k, v in mcp.items()],
            "runs": {
                "running": sum(1 for e in state["runs"].values()
                               if e["status"] == "running"),
                "total": len(state["runs"]),
            },
        }

    # ---- 内嵌终端（与 agent_core.service 同协议；工作区边界 = 节点工作区） ----

    def _ws_authorized(ws: WebSocket) -> bool:
        """WS 鉴权：与 worker 其余端点同口径（ORCH_API_KEY 或仅环回）。"""
        if not cfg.api_key:
            host = (ws.client.host if ws.client else "") or ""
            return host in ("127.0.0.1", "::1", "localhost", "testclient")
        supplied = ws.query_params.get("api_key") or ws.headers.get("x-api-key", "")
        return supplied == cfg.api_key

    @router.websocket("/agent/terminal")
    async def terminal(ws: WebSocket):
        """内嵌终端：协议 ready/output/done/error/confirm，cwd 会话 + 工作区边界。"""
        if not _ws_authorized(ws):
            await ws.close(code=4401)
            return
        await ws.accept()
        ws_root = state["workspace"]
        cwd = str(ws_root)
        shell = os.environ.get("AGENT_TERMINAL_SHELL") or (
            "cmd.exe" if os.name == "nt" else (os.environ.get("SHELL") or "/bin/sh"))
        session = {"cwd": cwd, "proc": None, "outside_allowed": False}
        await ws.send_json({"type": "ready", "cwd": cwd, "shell": shell,
                            "workspace": str(ws_root)})
        current: asyncio.Task | None = None

        def _in_workspace(p: Path) -> bool:
            try:
                p = p.resolve()
                root = ws_root.resolve()
            except OSError:
                return False
            return p == root or root in p.parents

        async def _stream(cmd: str, allow_once: bool = False) -> None:
            raw = cmd.strip()
            if not raw:
                await ws.send_json({"type": "done", "code": 0, "cwd": session["cwd"]})
                return
            allowed = allow_once or session.get("outside_allowed")
            m = re.match(r'^(cd|chdir|set-location)\s*(.*)$', raw, re.I)
            if m:
                target = m.group(2).strip().strip('"').strip("'")
                target = re.sub(r'^/d\s+', '', target, flags=re.I) or "~"
                if target == "~":
                    target = os.path.expanduser("~")
                p = Path(target)
                if not p.is_absolute():
                    p = Path(session["cwd"]) / p
                try:
                    p = p.resolve()
                except OSError:
                    p = None
                if p is not None and p.is_dir():
                    if not allowed and not _in_workspace(p):
                        await ws.send_json({
                            "type": "confirm", "kind": "cd", "cmd": raw,
                            "paths": [str(p)], "workspace": str(ws_root),
                            "message": "目标目录在工作区之外，是否允许进入？"})
                        await ws.send_json({"type": "done", "code": 1,
                                            "cwd": session["cwd"]})
                        return
                    session["cwd"] = str(p)
                    await ws.send_json({"type": "output", "data": str(p) + "\n"})
                    await ws.send_json({"type": "done", "code": 0, "cwd": session["cwd"]})
                else:
                    await ws.send_json({"type": "error", "message": f"目录不存在: {target}"})
                    await ws.send_json({"type": "done", "code": 1, "cwd": session["cwd"]})
                return
            if not allowed:
                outside = _scan_outside_paths(raw, ws_root)
                if outside:
                    await ws.send_json({
                        "type": "confirm", "kind": "path", "cmd": raw,
                        "paths": outside, "workspace": str(ws_root),
                        "message": "该命令涉及工作区外的路径，是否允许访问？"})
                    await ws.send_json({"type": "done", "code": 1,
                                        "cwd": session["cwd"]})
                    return

            loop = asyncio.get_running_loop()
            queue: asyncio.Queue = asyncio.Queue()
            kwargs: dict = dict(shell=True, cwd=session["cwd"], stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                text=True, encoding="utf-8", errors="replace", bufsize=1)
            if os.name == "nt":
                kwargs["creationflags"] = 0x08000000  # CREATE_NO_WINDOW
            try:
                proc = subprocess.Popen(raw, **kwargs)
            except Exception as e:  # noqa: BLE001
                await ws.send_json({"type": "error", "message": f"启动失败: {e}"})
                await ws.send_json({"type": "done", "code": 1, "cwd": session["cwd"]})
                return
            session["proc"] = proc

            def _reader() -> None:
                try:
                    for line in proc.stdout:  # type: ignore[union-attr]
                        loop.call_soon_threadsafe(queue.put_nowait, line)
                except Exception:  # noqa: BLE001
                    pass
                finally:
                    loop.call_soon_threadsafe(queue.put_nowait, None)

            threading.Thread(target=_reader, daemon=True, name="term-reader").start()
            while True:
                item = await queue.get()
                if item is None:
                    break
                await ws.send_json({"type": "output", "data": item})
            code = proc.wait()
            session["proc"] = None
            await ws.send_json({"type": "done", "code": code, "cwd": session["cwd"]})

        try:
            while True:
                msg = await ws.receive_json()
                mtype = msg.get("type")
                if mtype == "run":
                    if current is not None and not current.done():
                        await ws.send_json({"type": "error",
                                            "message": "有命令正在执行，请先中断（Ctrl+C）"})
                        continue
                    if msg.get("allow_session"):
                        session["outside_allowed"] = True
                        await ws.send_json({"type": "output",
                                            "data": "[本次会话已允许访问工作区外]\n",
                                            "cls": "info"})
                    current = asyncio.create_task(
                        _stream(str(msg.get("cmd") or ""),
                                allow_once=bool(msg.get("allow_once"))))
                elif mtype == "interrupt":
                    proc = session.get("proc")
                    if proc is not None:
                        try:
                            proc.terminate()
                        except Exception:  # noqa: BLE001
                            pass
                    await ws.send_json({"type": "output", "data": "^C\n"})
                elif mtype == "ping":
                    await ws.send_json({"type": "pong"})
        except WebSocketDisconnect:
            pass
        except Exception as e:  # noqa: BLE001
            log.info("terminal ws closed: %s", e)
        finally:
            if current is not None and not current.done():
                current.cancel()
            proc = session.get("proc")
            if proc is not None:
                try:
                    proc.terminate()
                except Exception:  # noqa: BLE001
                    pass

    # ---- 静态面板页 ----

    @router.get("/panel")
    def serve_panel():
        panel = _panel_html_path()
        if not panel.exists():
            raise HTTPException(status_code=404, detail="panel 未安装")
        return FileResponse(panel, media_type="text/html")

    @router.get("/")
    def root_redirect():
        return RedirectResponse(url="/panel")

    return router