"""单机 Agent HTTP 服务。

把 ``agent_core`` 内核的 Agent 能力暴露为 HTTP API，供前端 / 脚本 / 其他进程调用。
**不含** brain 注册/心跳逻辑——那是集群 worker 的职责；本服务只专注单机 Agent 执行。

端点：
  POST   /agent/run                       单发执行，返回 KernelResult JSON（含 usage_summary）
  POST   /agent/run/stream                单发流式执行（SSE：text_delta/tool_use/
                                           tool_result/budget_warning/result/error）
  POST   /agent/run/async                 异步单发：返回 202 + {run_id}，后台执行
  GET    /agent/run/{run_id}              查询异步运行状态/结果
  DELETE /agent/run/{run_id}              请求取消异步运行（协作式 cancel_event）
  POST   /agent/session                   创建多轮会话，返回 {session_id}
  POST   /agent/session/{id}/message      向会话发消息，返回本轮结果 JSON
  POST   /agent/session/{id}/message/stream  向会话发消息（SSE 流式）
  POST   /agent/session/{id}/interrupt    中断会话当前轮
  DELETE /agent/session/{id}              关闭会话
  GET    /agent/sessions                  列出活跃会话元数据
  GET    /agent/memory/core               列出核心记忆
  GET    /agent/memory/core/{name}        读取一条核心记忆
  PUT    /agent/memory/core/{name}        写入一条核心记忆
  GET    /agent/tools                     列出可用工具/技能/MCP
  GET    /agent/status                    内核状态（会话/oneshot 用量分列）
  GET    /agent/metrics                   可观测性指标（延迟分布、错误分类）
  GET    /agent/history                   运行历史查询（分页 + 状态过滤）
  GET    /agent/stats                     用量趋势统计（按天/小时聚合）
  GET    /agent/trace/{run_id}            查询单次执行的 trace 详情
  GET    /health                          健康检查（含 doctor 自检 checks 汇总）

启动：
  python -m agent_core.service --port 8000 --persona executor
环境变量：AGENT_API_KEY（可选，设置后除 /health 外需 X-API-Key 头）；
未设置 key 时默认仅放行环回地址（127.0.0.1/::1），非环回请求拒绝（403），
AGENT_ALLOW_UNAUTHENTICATED=1 可显式恢复全开放（仅限开发/受控环境）；
其余模型/路径/MCP 变量见 agent_core.config.load_agent_config。

错误响应统一形状：{"code", "message", "detail"}。
设计约束：SDK 延迟导入（内核内部按需 import），FastAPI/uvicorn 为本服务硬依赖。
"""
import argparse
import asyncio
import json
import logging
import os
import threading
import time
import uuid
from dataclasses import asdict
from pathlib import Path
from typing import Any, AsyncIterator

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from agent_core.config import AgentConfig, load_agent_config
from agent_core.doctor import run_checks, summarize
from agent_core.errors import humanize_sdk_error
from agent_core.kernel import AgentEvent, AgentKernel
from agent_core.persona import Persona, list_persona_names, load_persona_by_name, parse_subagents, strip_frontmatter
from agent_core.run_history import RunHistoryStore, build_run_record
from agent_core.tools import load_mcp_servers, scan_skills, valid_memory_name

logger = logging.getLogger(__name__)

# ---- 进程内共享状态（单进程服务，模块级缓存即可） ----

_CONFIG: AgentConfig | None = None
_KERNEL: AgentKernel | None = None
_PERSONAS: dict[str, Persona] = {}
# session_id -> {"session": AgentSession, "cwd": str, "persona": str}
_SESSIONS: dict[str, dict] = {}
# 服务级默认人设（--persona 参数）
_DEFAULT_PERSONA = "executor"
# run_id -> 异步运行条目（status/result/cancel_event/task...）
_ASYNC_RUNS: dict[str, dict] = {}
# oneshot 用量模块级累加器（与 session 用量分开统计）
_ONESHOT_TOTALS: dict = {"runs": 0, "errors": 0, "total_cost_usd": 0.0, "total_turns": 0}
# doctor 自检结果缓存（main() 启动时填充；/health 惰性兜底）
_HEALTH_CHECKS: list | None = None
# 指标采集：最近 N 次执行的延迟分布（用于 /agent/metrics）
_METRICS: dict = {
    "durations_ms": [],  # 最近 100 次执行延迟
    "error_types": {},   # 错误类型计数
    "total_corrections": 0,  # 自纠错触发次数
}
_MAX_METRICS_DURATIONS = 100  # 保留最近 N 次延迟数据
# Rate Limiting：{client_id: [timestamp, ...]}
_RATE_LIMIT_STORE: dict[str, list[float]] = {}
# 运行历史存储（惰性初始化）
_HISTORY_STORE: RunHistoryStore | None = None


# ---- 请求体模型 ----

class RunReq(BaseModel):
    """单发执行请求。"""
    prompt: str = Field(..., description="完整任务说明（必填）")
    persona: str | None = Field(None, description="人设名（如 executor）；缺省用服务默认人设")
    context_memory: str | None = Field(None, description="附加上下文记忆/参考资料，拼在任务说明之前")
    cwd: str | None = Field(None, description="任务工作目录（绝对路径）；缺省在 work_dir 下自动创建")
    model: str | None = Field(None, description="模型覆盖（如 qwen3.8-flash）；缺省用 persona/配置默认")
    planning_enabled: bool | None = Field(None, description="true 时用 Plan-Execute 引擎（异步路径）；计划步骤实时产出 plan_update 事件")


class SessionCreateReq(BaseModel):
    """创建多轮会话请求。"""
    persona: str | None = Field(None, description="人设名；缺省用服务默认人设")
    cwd: str | None = Field(None, description="会话工作目录（绝对路径）；缺省在 work_dir 下自动创建")


class SessionMessageReq(BaseModel):
    """会话消息请求。"""
    prompt: str = Field(..., description="本轮消息内容（必填）")
    timeout: float | None = Field(None, description="本轮墙钟超时（秒）；缺省不限。超时转 error 事件结束本轮")


class MemoryWriteReq(BaseModel):
    """核心记忆写入请求。"""
    content: str = Field(..., description="记忆正文（Markdown）；覆盖写入同名记忆")


# ---- 通用辅助 ----

def _clamp_sse_strings(value: Any, limit: int) -> Any:
    """递归限幅 payload 内的字符串字段（对齐 runtime truncate_summary 的
    截断标记做法）。序列化产物本身绝不截断——旧实现先 json.dumps 再切
    字符串，超长帧会让客户端 JSON.parse 直接失败。"""
    if isinstance(value, str):
        if len(value) > limit:
            return value[:limit] + "...(truncated)"
        return value
    if isinstance(value, dict):
        return {k: _clamp_sse_strings(v, limit) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_clamp_sse_strings(v, limit) for v in value]
    return value


def _valid_memory_name(name: str) -> bool:
    """记忆名校验：委托 tools.valid_memory_name（与 cli 共用同一实现，
    含 Windows 保留设备名黑名单）。"""
    return valid_memory_name(name)


def _error(status_code: int, code: str, message: str,
           detail: Any = None) -> JSONResponse:
    """统一错误响应形状：{"code", "message", "detail"}。"""
    return JSONResponse(status_code=status_code,
                        content={"code": code, "message": message, "detail": detail})


def _accumulate_oneshot(result_dict: dict, prompt: str = "",
                        persona: str = "", run_id: str | None = None) -> None:
    """oneshot 用量累加（/agent/status 的 oneshot_usage 数据源）+ 指标采集 + 历史持久化。"""
    _ONESHOT_TOTALS["runs"] += 1
    if result_dict.get("is_error") or result_dict.get("cancelled"):
        _ONESHOT_TOTALS["errors"] += 1
        # 错误类型统计
        et = result_dict.get("error_type") or "unknown"
        _METRICS["error_types"][et] = _METRICS["error_types"].get(et, 0) + 1
    try:
        _ONESHOT_TOTALS["total_cost_usd"] += float(result_dict.get("cost_usd") or 0.0)
    except (TypeError, ValueError):
        pass
    _ONESHOT_TOTALS["total_turns"] += result_dict.get("turns") or 0
    # 延迟分布
    dur = result_dict.get("duration_ms") or 0
    if dur > 0:
        _METRICS["durations_ms"].append(dur)
        if len(_METRICS["durations_ms"]) > _MAX_METRICS_DURATIONS:
            _METRICS["durations_ms"] = _METRICS["durations_ms"][-_MAX_METRICS_DURATIONS:]
    # 自纠错统计
    meta = result_dict.get("meta") or {}
    if meta.get("correction"):
        _METRICS["total_corrections"] += 1
    # 运行历史持久化
    try:
        store = _get_history_store()
        config = get_config()
        record = build_run_record(prompt, persona, config.model or "",
                                  result_dict, run_id=run_id)
        store.append(record)
    except Exception as e:
        logger.warning("运行历史持久化失败: %s", e)


def _get_history_store() -> RunHistoryStore:
    """惰性初始化运行历史存储。"""
    global _HISTORY_STORE
    if _HISTORY_STORE is None:
        config = get_config()
        history_dir = config.run_history_dir or config.work_dir
        _HISTORY_STORE = RunHistoryStore(history_dir, config.run_history_max_records)
    return _HISTORY_STORE


def _format_usage_summary(result_dict: dict) -> str:
    """把执行结果格式化为可读的用量摘要。"""
    is_error = result_dict.get("is_error")
    error_type = result_dict.get("error_type") or ""
    cost_usd = float(result_dict.get("cost_usd") or 0.0)
    duration_ms = int(result_dict.get("duration_ms") or 0)
    turns = int(result_dict.get("turns") or 0)

    # 耗时格式化
    if duration_ms >= 60000:
        mins, secs = divmod(duration_ms // 1000, 60)
        duration_str = f"{mins} 分 {secs} 秒"
    else:
        duration_str = f"{duration_ms // 1000} 秒"

    if is_error:
        return f"执行失败（错误类型: {error_type}），耗时 {duration_str}"

    # 成本转人民币（近似汇率 7.2）
    cost_cny = cost_usd * 7.2
    if cost_cny >= 1:
        cost_str = f"¥{cost_cny:.2f}"
    else:
        cost_str = f"¥{cost_cny:.4f}" if cost_cny > 0 else "¥0"

    return f"本次花费约 {cost_str}，耗时 {duration_str}，共 {turns} 轮对话"


# ---- 配置 / 内核 / 人设 装配 ----

def get_config() -> AgentConfig:
    """惰性装配并缓存 AgentConfig。"""
    global _CONFIG
    if _CONFIG is None:
        _CONFIG = load_agent_config()
    return _CONFIG


def get_kernel() -> AgentKernel:
    """惰性创建并缓存 AgentKernel。"""
    global _KERNEL
    if _KERNEL is None:
        _KERNEL = AgentKernel(get_config())
    return _KERNEL


def _build_orchestrator_persona(config: AgentConfig) -> Persona:
    """orchestrator（大脑）人设兜底：SKILL.md 正文 + 子 agent 注册。"""
    skill_md = (Path(config.project_root) / "workspace" / "agent" / "skills"
                / "orchestrator" / "SKILL.md")
    body = "你是 orchestrator，多 agent 编排框架的大脑。"
    if skill_md.is_file():
        body += "\n\n" + strip_frontmatter(skill_md.read_text(encoding="utf-8"))
    return Persona(name="orchestrator",
                   description="编排大脑：拆解目标、派发子 agent、汇总裁决",
                   system_prompt=body,
                   sub_agents=parse_subagents(config.persona_dir))


def resolve_persona(name: str) -> Persona:
    """按名称解析并缓存人设；orchestrator 无定义文件时回退大脑人设，
    其余未知名给最小人设兜底并 log 警告列出可用人设名。"""
    if name in _PERSONAS:
        return _PERSONAS[name]
    config = get_config()
    try:
        persona = load_persona_by_name(name, config.persona_dir)
    except FileNotFoundError:
        if name == "orchestrator":
            persona = _build_orchestrator_persona(config)
        else:
            available = list_persona_names(config.persona_dir)
            logger.warning("[persona] 未找到人设定义 '%s'（目录：%s）；可用人设：%s；已回退最小人设。",
                           name, config.persona_dir, ", ".join(available) or "无")
            persona = Persona(name=name,
                              description=f"{name}（未找到定义文件，使用最小人设）",
                              system_prompt=f"你是 {name}。请按用户指令完成任务。")
    _PERSONAS[name] = persona
    return persona


def _prepare_cwd(explicit: str | None, tag: str) -> str:
    """确定任务工作目录：显式指定则用之（并确保存在），否则在内核 work_dir
    下开一个带 tag 的独立子目录，避免多次调用的产物互相污染。"""
    config = get_config()
    if explicit:
        p = Path(explicit)
        p.mkdir(parents=True, exist_ok=True)
        return str(p)
    base = Path(config.work_dir or (config.project_root or ".") + "/.agent_work")
    p = base / f"{tag}-{uuid.uuid4().hex[:8]}"
    p.mkdir(parents=True, exist_ok=True)
    return str(p)


# ---- 运行事件持久化（Agent 面板数据源：实时流落 JSONL 供回放/预览） ----

def _runs_dir() -> Path:
    """运行事件落盘目录：<work_dir>/runs/。"""
    config = get_config()
    d = Path(config.work_dir or (config.project_root or ".") + "/.agent_work") / "runs"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _run_meta_path(run_id: str) -> Path:
    return _runs_dir() / f"{run_id}.meta.json"


def _write_run_meta(run_id: str, **updates: Any) -> None:
    """写/更新 run 元数据 sidecar（cwd/prompt/status 等，供回放与产物预览定位）。"""
    p = _run_meta_path(run_id)
    meta: dict = {}
    if p.exists():
        try:
            meta = json.loads(p.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            meta = {}
    meta.update(updates)
    meta["run_id"] = run_id  # 权威字段：回放/预览端点按此定位
    p.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")


def _make_event_sink(run_id: str):
    """事件 sink：把 (run_id, {seq,ts,type,data}) 逐条追加到 runs/<id>.jsonl。"""
    path = _runs_dir() / f"{run_id}.jsonl"

    def sink(rid: str, event: dict) -> None:
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(event, ensure_ascii=False, default=str) + "\n")

    return sink


async def _run_session_turn(session: Any, prompt: str,
                            timeout: float | None = None) -> dict:
    """消费一轮会话事件，聚合为可 JSON 化的结果 dict。

    timeout 非空时透传给 AgentSession.send（墙钟超时转 error 事件）。
    """
    texts: list[str] = []
    info: dict = {}
    warnings: list = []
    if timeout is not None:
        events = session.send(prompt, timeout)
    else:
        events = session.send(prompt)
    async for ev in events:
        if ev.type == "text_delta":
            texts.append(ev.data or "")
        elif ev.type == "result":
            info = ev.data if isinstance(ev.data, dict) else {}
        elif ev.type == "budget_warning":
            warnings.append(ev.data if isinstance(ev.data, dict) else str(ev.data))
        elif ev.type == "error":
            info = {"is_error": True, "error": ev.data}
    output = info.get("result") or "".join(texts) or "(无输出)"
    return {
        "output": output,
        "turns": info.get("num_turns", 0) or 0,
        "is_error": bool(info.get("is_error") or info.get("error")),
        "usage": info.get("usage"),
        "cost_usd": info.get("cost_usd"),
        "duration_ms": info.get("duration_ms", 0) or 0,
        "session_id": session.session_id,
        "error": info.get("error"),
        "budget_warnings": warnings,
    }


# ---- API Key 认证中间件（可选） ----

# 环回来源白名单：与 runtime/auth.py 同语义；"testclient" 为测试请求默认来源
_LOOPBACK_HOSTS = {"127.0.0.1", "::1", "localhost", "testclient"}


def _allow_unauthenticated() -> bool:
    """显式开放无鉴权访问：AGENT_ALLOW_UNAUTHENTICATED=1（ORCH_ 前缀兼容）。"""
    truthy = ("1", "true", "True", "yes", "on")
    return (os.environ.get("AGENT_ALLOW_UNAUTHENTICATED", "") in truthy
            or os.environ.get("ORCH_ALLOW_UNAUTHENTICATED", "") in truthy)


def _scope_host(scope: dict) -> str | None:
    """从 ASGI scope 取请求来源 host；client 不可得时返回 None。"""
    client = scope.get("client")
    return client[0] if client else None


class _ApiKeyMiddleware:
    """极简 ASGI 中间件：设置 AGENT_API_KEY 后校验 X-API-Key 头，/health 放行。

    未设置 key（loopback_only=True）：默认仅放行环回地址请求，非环回拒绝（403），
    与 brain_api / worker_api 的 auth.py 行为一致；
    AGENT_ALLOW_UNAUTHENTICATED=1（或 ORCH_ 前缀）可显式恢复全开放。
    """

    def __init__(self, app, api_key: str, loopback_only: bool = False):
        self.app = app
        self.api_key = api_key
        self.loopback_only = loopback_only

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        path = scope.get("path", "")
        if path == "/health":
            await self.app(scope, receive, send)
            return
        if self.loopback_only:
            # 未配置 key：环回豁免；非环回默认拒绝
            if _scope_host(scope) in _LOOPBACK_HOSTS:
                await self.app(scope, receive, send)
                return
            from starlette.responses import JSONResponse
            resp = JSONResponse(status_code=403, content={
                "detail": "API key not configured; only loopback access is allowed. "
                          "Set AGENT_API_KEY, or AGENT_ALLOW_UNAUTHENTICATED=1 to override."})
            await resp(scope, receive, send)
            return
        headers = dict(scope.get("headers") or [])
        key = headers.get(b"x-api-key", b"").decode()
        if key != self.api_key:
            from starlette.responses import JSONResponse
            resp = JSONResponse(status_code=401,
                                content={"detail": "invalid or missing API key"})
            await resp(scope, receive, send)
            return
        await self.app(scope, receive, send)


class _RateLimitMiddleware:
    """滑动窗口限流中间件：按客户端 IP/API Key 限制请求频率。

    超限返回 429 + Retry-After header。/health 端点免检。
    用模块级 _RATE_LIMIT_STORE 记录每个客户端的请求时间戳列表。
    """

    def __init__(self, app, max_per_minute: int = 60):
        self.app = app
        self.max_per_minute = max(1, max_per_minute)

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        path = scope.get("path", "")
        if path == "/health":
            await self.app(scope, receive, send)
            return
        # 客户端标识：API Key 优先，否则 IP
        headers = dict(scope.get("headers") or [])
        api_key = headers.get(b"x-api-key", b"").decode()
        client = scope.get("client") or ("unknown", 0)
        client_id = api_key if api_key else client[0]
        now = time.time()
        window_start = now - 60.0
        timestamps = _RATE_LIMIT_STORE.get(client_id, [])
        # 清理过期时间戳
        timestamps = [t for t in timestamps if t > window_start]
        if len(timestamps) >= self.max_per_minute:
            from starlette.responses import JSONResponse
            retry_after = int(60 - (now - timestamps[0])) + 1
            resp = JSONResponse(
                status_code=429,
                content={"detail": f"Rate limit exceeded ({self.max_per_minute}/min)"},
                headers={"Retry-After": str(retry_after)},
            )
            await resp(scope, receive, send)
            return
        timestamps.append(now)
        _RATE_LIMIT_STORE[client_id] = timestamps
        await self.app(scope, receive, send)


# ---- 应用工厂 ----

def create_app(default_persona: str = "executor") -> FastAPI:
    """装配 FastAPI 应用。default_persona 为未显式指定人设时的默认值。"""
    global _DEFAULT_PERSONA
    _DEFAULT_PERSONA = default_persona

    app = FastAPI(title="agent_core service", version="1.0.0",
                  description="单机 Agent HTTP 服务（无 brain 注册/心跳）")
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    api_key = os.environ.get("AGENT_API_KEY", "")
    if api_key:
        app.add_middleware(_ApiKeyMiddleware, api_key=api_key)
    elif _allow_unauthenticated():
        import logging
        logging.getLogger(__name__).warning(
            "鉴权未启用：AGENT_ALLOW_UNAUTHENTICATED=1 已显式开放无鉴权访问，"
            "仅限开发/受控环境使用！")
    else:
        import logging
        logging.getLogger(__name__).warning(
            "⚠ 鉴权未启用（未设置 AGENT_API_KEY）：默认仅放行环回地址（127.0.0.1/::1），"
            "非环回请求将被拒绝（403）。跨主机访问请设置 AGENT_API_KEY；"
            "如需恢复旧的全开放模式，请显式设置 AGENT_ALLOW_UNAUTHENTICATED=1。")
        app.add_middleware(_ApiKeyMiddleware, api_key="", loopback_only=True)

    # Rate Limiting 中间件（按配置启用）
    _cfg = get_config()
    if getattr(_cfg, "rate_limit_per_minute", 0) > 0:
        app.add_middleware(_RateLimitMiddleware,
                           max_per_minute=_cfg.rate_limit_per_minute)

    @app.exception_handler(HTTPException)
    async def _http_exception_handler(request, exc: HTTPException):
        """把 HTTPException 也收敛为统一错误形状。"""
        return JSONResponse(status_code=exc.status_code,
                            content={"code": f"http_{exc.status_code}",
                                     "message": str(exc.detail), "detail": None})

    @app.get("/health")
    def health() -> dict:
        """健康检查（含 doctor 自检 checks 汇总；自检失败不影响存活判定）。"""
        global _HEALTH_CHECKS
        if _HEALTH_CHECKS is None:
            try:
                _HEALTH_CHECKS = run_checks(get_config(), _DEFAULT_PERSONA)
            except Exception:  # noqa: BLE001 - 自检崩溃不影响存活判定
                _HEALTH_CHECKS = []
        try:
            summary = summarize(_HEALTH_CHECKS)
        except Exception:  # noqa: BLE001
            summary = {"total": 0, "ok": 0, "warn": 0, "fail": 0, "items": []}
        return {"status": "ok", "service": "agent_core", "checks": summary}

    # ---- SSE 流式辅助 ----

    def _event_payload(ev: Any) -> dict:
        """把 AgentEvent 转为可 JSON 化的 SSE data payload。"""
        if ev.type == "text_delta":
            return {"text": ev.data if isinstance(ev.data, str) else str(ev.data)}
        if ev.type == "tool_use":
            d = ev.data if isinstance(ev.data, dict) else {"name": ev.data}
            return {"name": d.get("name"), "input": d.get("input")}
        if ev.type == "tool_result":
            return {"content": ev.data}
        if ev.type == "budget_warning":
            return dict(ev.data) if isinstance(ev.data, dict) else {"detail": str(ev.data)}
        if ev.type == "result":
            return dict(ev.data) if isinstance(ev.data, dict) else {"detail": str(ev.data)}
        if ev.type == "error":
            return {"message": str(ev.data)}
        return {"data": ev.data}

    def _sse_format(event_type: str, payload: Any, limit: int = 2000) -> str:
        """单条 SSE 帧：event + data（JSON）。

        序列化前先对 payload 内长字符串字段限幅（截断加 "...(truncated)"
        标记），保证超长 data 仍是合法 JSON。"""
        data = json.dumps(_clamp_sse_strings(payload, limit),
                          ensure_ascii=False, default=str)
        return f"event: {event_type}\ndata: {data}\n\n"

    async def _sse_stream(events: AsyncIterator[AgentEvent]):
        """消费 AgentEvent 流并产出 SSE 帧（异常收敛为 error 帧）。"""
        try:
            async for ev in events:
                yield _sse_format(ev.type, _event_payload(ev))
        except Exception as e:  # noqa: BLE001 - 转发层异常也收敛为 error 帧
            yield _sse_format("error", {"message": f"流式转发异常：{e}"})

    def _streaming_response(events) -> StreamingResponse:
        return StreamingResponse(
            _sse_stream(events), media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    # ---- 单发：同步 / 流式 / 异步 ----

    @app.post("/agent/run")
    async def agent_run(req: RunReq) -> dict:
        """单发执行：临时工作目录跑一轮，返回 KernelResult JSON。"""
        persona = resolve_persona(req.persona or _DEFAULT_PERSONA)
        cwd = _prepare_cwd(req.cwd, tag="run")
        kernel = get_kernel()
        # run_oneshot 是同步阻塞（内部自起事件循环），放线程池避免卡住服务 loop
        result = await asyncio.to_thread(
            kernel.run_oneshot, req.prompt, persona, cwd, req.context_memory or ""
        )
        d = asdict(result)
        _accumulate_oneshot(d, prompt=req.prompt, persona=persona.name)
        d["usage_summary"] = _format_usage_summary(d)
        return d

    @app.post("/agent/run/stream")
    async def agent_run_stream(req: RunReq):
        """单发流式执行（SSE）。事件名：thinking/thinking_delta/text_delta/
        tool_use/tool_result/plan_update/budget_warning/result/error；
        data 为 JSON，payload 超长截断（2000 字符）。
        全量事件落 <work_dir>/runs/<run_id>.jsonl 供面板回放；结束后写历史。"""
        persona = resolve_persona(req.persona or _DEFAULT_PERSONA)
        cwd = _prepare_cwd(req.cwd, tag="run")
        kernel = get_kernel()
        run_id = f"run-{uuid.uuid4().hex[:12]}"
        _write_run_meta(run_id, cwd=cwd,
                        prompt=req.prompt[:2000], persona=persona.name,
                        model=req.model or "", status="running",
                        started_at=round(time.time(), 3))
        sink = _make_event_sink(run_id)
        events = kernel.stream(req.prompt, persona, cwd, req.context_memory or "",
                               run_id=run_id, event_sink=sink)

        async def _events_with_bookkeeping():
            final: dict | None = None
            try:
                async for ev in events:
                    if ev.type == "result" and isinstance(ev.data, dict):
                        final = dict(ev.data)
                    yield ev
            except Exception as e:  # noqa: BLE001
                _write_run_meta(run_id, status="failed",
                                error=f"流式转发异常：{e}")
                yield AgentEvent("error", f"流式转发异常：{e}")
            finally:
                # 客户端提前断连（生成器 aclose）也落终态，不留 running 残留
                if final is not None:
                    _write_run_meta(run_id, status=("failed" if final.get("is_error")
                                                    else "completed"))
                    # 修复"流式 run 不入历史"：同一 run_id 聚合入 history
                    result_dict = {"is_error": bool(final.get("is_error")),
                                   "cancelled": False,
                                   "error_type": final.get("error_type") or "",
                                   "cost_usd": final.get("cost_usd"),
                                   "turns": final.get("num_turns") or 0,
                                   "duration_ms": final.get("duration_ms") or 0,
                                   "usage": final.get("usage"),
                                   "output": final.get("result"),
                                   "meta": {}}
                    _accumulate_oneshot(result_dict, prompt=req.prompt,
                                        persona=persona.name, run_id=run_id)
                else:
                    _write_run_meta(run_id, status="interrupted")

        return _streaming_response(_events_with_bookkeeping())

    @app.post("/agent/run/async", status_code=202)
    async def agent_run_async(req: RunReq):
        """异步单发：立即返回 202 + {run_id}，后台任务执行 run_oneshot。

        用 GET /agent/run/{run_id} 查询状态与结果；DELETE 触发协作式取消
        （cancel_event 在消息边界生效，非强杀）。
        """
        persona = resolve_persona(req.persona or _DEFAULT_PERSONA)
        cwd = _prepare_cwd(req.cwd, tag="run")
        if req.planning_enabled:
            # 请求级规划开关：replace 出独立 config，避免污染共享内核
            import dataclasses
            run_cfg = dataclasses.replace(get_config(), planning_enabled=True)
            kernel = AgentKernel(run_cfg)
        else:
            kernel = get_kernel()
        run_id = uuid.uuid4().hex[:12]
        cancel_event = threading.Event()
        entry: dict = {
            "run_id": run_id, "status": "running", "prompt": req.prompt,
            "persona": persona.name, "cwd": cwd, "cancel_event": cancel_event,
            "result": None, "error": None, "task": None,
            "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        _ASYNC_RUNS[run_id] = entry
        _write_run_meta(run_id, cwd=cwd,
                        prompt=req.prompt[:2000], persona=persona.name,
                        model=req.model or "", status="running",
                        started_at=round(time.time(), 3))
        sink = _make_event_sink(run_id)

        async def _worker():
            try:
                result = await asyncio.to_thread(
                    kernel.run_oneshot, req.prompt, persona, cwd,
                    req.context_memory or "", cancel_event,
                    run_id=run_id, event_sink=sink)
                d = asdict(result)
                entry["result"] = d
                entry["status"] = ("cancelled" if result.cancelled
                                   else "failed" if result.is_error else "completed")
                _accumulate_oneshot(d, prompt=req.prompt, persona=persona.name,
                                    run_id=run_id)
                _write_run_meta(run_id, status=entry["status"])
            except Exception as e:  # noqa: BLE001 - 后台任务异常收敛进条目
                entry["status"] = "failed"
                error_type, message, hint = humanize_sdk_error(e)
                entry["error"] = {"code": error_type, "message": message,
                                  "hint": hint, "raw": str(e)}

        entry["task"] = asyncio.create_task(_worker())
        return JSONResponse(status_code=202,
                            content={"run_id": run_id, "status": "running"})

    @app.get("/agent/run/{run_id}")
    async def agent_run_get(run_id: str):
        """查询异步运行：status ∈ running/completed/failed/cancelled。"""
        entry = _ASYNC_RUNS.get(run_id)
        if entry is None:
            return _error(404, "not_found", f"run not found: {run_id}")
        resp: dict = {
            "run_id": run_id, "status": entry["status"],
            "persona": entry["persona"], "prompt": entry["prompt"],
            "cwd": entry["cwd"], "created_at": entry["created_at"],
        }
        if entry.get("result") is not None:
            resp["result"] = entry["result"]
        if entry.get("error") is not None:
            resp["error"] = entry["error"]
        return resp

    @app.delete("/agent/run/{run_id}")
    async def agent_run_cancel(run_id: str):
        """请求取消异步运行：置位 cancel_event，在下一个消息边界停下。"""
        entry = _ASYNC_RUNS.get(run_id)
        if entry is None:
            return _error(404, "not_found", f"run not found: {run_id}")
        entry["cancel_event"].set()
        return {"run_id": run_id, "status": entry["status"],
                "cancel_requested": True}

    # ---- 运行事件回放 / 产物只读 / 面板（Agent 面板数据源） ----

    @app.get("/agent/runs")
    def list_runs(limit: int = 50) -> dict:
        """列出运行（runs/<id>.meta.json，按 started_at 新→旧）。"""
        items = []
        for meta_path in sorted(_runs_dir().glob("*.meta.json"),
                                key=lambda p: p.stat().st_mtime, reverse=True)[:max(0, limit)]:
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
                items.append(meta)
            except Exception:  # noqa: BLE001 - 坏文件跳过
                continue
        return {"runs": items}

    @app.get("/agent/run/{run_id}/events")
    def replay_run_events(run_id: str, limit: int = 5000) -> list:
        """回放一次 run 的全量事件（按 seq 升序，供面板回放模式）。"""
        events_path = _runs_dir() / f"{run_id}.jsonl"
        if not events_path.exists():
            raise HTTPException(status_code=404,
                                detail=f"run 事件不存在: {run_id}")
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

    @app.get("/agent/artifact/{run_id}")
    def read_run_artifact(run_id: str, path: str):
        """只读回传该 run 工作目录下的产物文件（防路径穿越；二进制 base64）。"""
        meta_path = _run_meta_path(run_id)
        if not meta_path.exists():
            raise HTTPException(status_code=404, detail=f"run not found: {run_id}")
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except ValueError:
            raise HTTPException(status_code=404, detail="run meta corrupted")
        cwd = meta.get("cwd") or ""
        if not cwd or ".." in path or path.startswith(("/", "\\")) or "\\" in path:
            raise HTTPException(status_code=400, detail="invalid path")
        base = Path(cwd).resolve()
        full = (base / path).resolve()
        try:
            full.relative_to(base)
        except ValueError:
            raise HTTPException(status_code=400, detail="invalid path")
        if not full.is_file():
            raise HTTPException(status_code=404, detail="artifact not found")
        size = full.stat().st_size
        data = full.read_bytes()
        try:
            return {"path": path, "content": data.decode("utf-8"),
                    "encoding": "utf-8", "size": size}
        except UnicodeDecodeError:
            import base64
            return {"path": path, "content": base64.b64encode(data).decode("ascii"),
                    "encoding": "base64", "size": size}

    @app.get("/panel")
    def serve_panel():
        """Agent 面板（单文件深色 UI，由本服务直接托管）。"""
        from fastapi.responses import FileResponse
        panel = Path(__file__).resolve().parent / "panel" / "index.html"
        if not panel.exists():
            raise HTTPException(status_code=404, detail="panel 未安装")
        return FileResponse(panel, media_type="text/html")

    @app.get("/")
    def root_redirect():
        """根路径重定向到面板。"""
        from fastapi.responses import RedirectResponse
        return RedirectResponse(url="/panel")

    # ---- 多轮会话 ----

    @app.post("/agent/session")
    async def agent_session_create(req: SessionCreateReq) -> dict:
        """创建多轮会话并连接底层客户端，返回本地 session_id。"""
        persona_name = req.persona or _DEFAULT_PERSONA
        persona = resolve_persona(persona_name)
        cwd = _prepare_cwd(req.cwd, tag="session")
        kernel = get_kernel()
        session = kernel.open_session(persona, cwd)
        try:
            await session.connect()
        except Exception as e:  # noqa: BLE001 - 连接失败给出可读错误
            raise HTTPException(status_code=500, detail=f"会话连接失败：{e}")
        sid = uuid.uuid4().hex[:12]
        _SESSIONS[sid] = {"session": session, "cwd": cwd, "persona": persona_name}
        return {"session_id": sid, "persona": persona_name, "cwd": cwd}

    @app.post("/agent/session/{session_id}/message")
    async def agent_session_message(session_id: str, req: SessionMessageReq) -> dict:
        """向已存在会话发一轮消息，聚合完整响应后返回。"""
        entry = _SESSIONS.get(session_id)
        if entry is None:
            raise HTTPException(status_code=404, detail="session not found")
        return await _run_session_turn(entry["session"], req.prompt, req.timeout)

    @app.post("/agent/session/{session_id}/message/stream")
    async def agent_session_message_stream(session_id: str, req: SessionMessageReq):
        """向已存在会话发一轮消息（SSE 流式转发 AgentSession 事件）。"""
        entry = _SESSIONS.get(session_id)
        if entry is None:
            return _error(404, "not_found", "session not found", session_id)
        session = entry["session"]
        if req.timeout is not None:
            events = session.send(req.prompt, req.timeout)
        else:
            events = session.send(req.prompt)
        return _streaming_response(events)

    @app.post("/agent/session/{session_id}/interrupt")
    async def agent_session_interrupt(session_id: str):
        """中断会话当前轮（协作式，在 turn 边界停下）。"""
        entry = _SESSIONS.get(session_id)
        if entry is None:
            return _error(404, "not_found", "session not found", session_id)
        try:
            await entry["session"].interrupt()
        except Exception as e:  # noqa: BLE001 - 中断失败返回错误详情
            return _error(500, "interrupt_failed", f"中断失败：{e}")
        return {"status": "interrupt_requested", "session_id": session_id}

    @app.get("/agent/sessions")
    def agent_sessions_list() -> dict:
        """列出活跃会话元数据（本地 id / 人设 / 工作目录 / 累计用量）。"""
        out = []
        for sid, entry in _SESSIONS.items():
            s = entry["session"]
            out.append({
                "session_id": sid,
                "persona": entry["persona"],
                "cwd": entry["cwd"],
                "sdk_session_id": getattr(s, "session_id", None),
                "total_turns": getattr(s, "total_turns", 0) or 0,
                "total_cost_usd": round(getattr(s, "total_cost", 0.0) or 0.0, 6),
            })
        return {"sessions": out, "count": len(out)}

    @app.delete("/agent/session/{session_id}")
    async def agent_session_close(session_id: str) -> dict:
        """关闭会话并释放底层连接。"""
        entry = _SESSIONS.pop(session_id, None)
        if entry is None:
            raise HTTPException(status_code=404, detail="session not found")
        try:
            await entry["session"].close()
        except Exception:  # noqa: BLE001 - 关闭失败不影响移除
            pass
        return {"status": "closed", "session_id": session_id}

    @app.get("/agent/tools")
    def agent_tools(persona: str | None = None) -> dict:
        """列出指定（或默认）人设的可用工具、技能、MCP 服务与记忆工具。"""
        p = resolve_persona(persona or _DEFAULT_PERSONA)
        config = get_config()
        skills = [s["name"] for s in scan_skills(config.project_root)]
        mcp = list((load_mcp_servers(config.mcp_config_path) or {}).keys())
        return {
            "persona": p.name,
            "tools": p.tools if p.tools else "all",
            "skills": skills,
            "mcp_servers": mcp,
            "memory_tools": ["search_experiences", "read_core_memory",
                             "write_core_memory", "write_task_context"],
        }

    # ---- 核心记忆 HTTP 端点（复用 tools.py 记忆后端读写） ----

    def _memory_backend() -> dict:
        """按 config.memory_backend 构造记忆后端（local/remote 函数集）。"""
        from agent_core.tools import _make_local_backend, _make_remote_backend
        config = get_config()
        if config.memory_backend == "remote":
            return _make_remote_backend(config)
        return _make_local_backend(config)

    @app.get("/agent/memory/core")
    def memory_core_list() -> dict:
        """列出核心记忆（name/size/updated_at；remote 后端仅列本地目录）。"""
        config = get_config()
        d = Path(config.memory_core_dir)
        items = []
        if d.is_dir():
            for p in sorted(d.glob("*.md")):
                try:
                    stat = p.stat()
                    items.append({
                        "name": p.stem,
                        "size": stat.st_size,
                        "updated_at": time.strftime(
                            "%Y-%m-%d %H:%M:%S", time.localtime(stat.st_mtime)),
                    })
                except OSError:  # noqa: BLE001 - 单条不可读不拖垮列表
                    continue
        return {"memories": items, "count": len(items),
                "backend": config.memory_backend, "dir": str(d)}

    @app.get("/agent/memory/core/{name}")
    async def memory_core_get(name: str):
        """读取一条核心记忆；不存在返回 404（统一错误形状）。"""
        if not _valid_memory_name(name):
            return _error(400, "invalid_name",
                          f"非法记忆名：{name!r}（仅字母/数字/下划线/连字符/中文，且不得为 Windows 保留设备名）")
        try:
            data = await _memory_backend()["read_core_memory"](name)
        except Exception as e:  # noqa: BLE001 - 后端异常收敛为 500
            return _error(500, "memory_read_failed", f"读取记忆失败：{e}")
        content = data.get("content") if isinstance(data, dict) else None
        if not content:
            return _error(404, "not_found", f"记忆 '{name}' 不存在或为空")
        return {"name": name, "content": content}

    @app.put("/agent/memory/core/{name}")
    async def memory_core_put(name: str, req: MemoryWriteReq):
        """写入一条核心记忆（覆盖写）。local 后端回显落盘路径。"""
        if not _valid_memory_name(name):
            return _error(400, "invalid_name",
                          f"非法记忆名：{name!r}（仅字母/数字/下划线/连字符/中文，且不得为 Windows 保留设备名）")
        # local 后端落盘前确保目录存在（FileMemoryStore 不自动建目录）
        if get_config().memory_backend != "remote":
            try:
                Path(get_config().memory_core_dir).mkdir(parents=True,
                                                         exist_ok=True)
            except OSError:  # noqa: BLE001 - 建目录失败交给写盘异常收敛
                pass
        try:
            await _memory_backend()["write_core_memory"](name, req.content)
        except Exception as e:  # noqa: BLE001
            return _error(500, "memory_write_failed", f"写入记忆失败：{e}")
        path = None
        if get_config().memory_backend == "local":
            path = str(Path(get_config().memory_core_dir) / f"{name}.md")
        return {"status": "written", "name": name, "path": path}

    @app.get("/agent/status")
    def agent_status() -> dict:
        """内核状态：会话用量与 oneshot 用量分开返回。"""
        config = get_config()
        total_cost = 0.0
        total_turns = 0
        for entry in _SESSIONS.values():
            s = entry["session"]
            total_cost += getattr(s, "total_cost", 0.0) or 0.0
            total_turns += getattr(s, "total_turns", 0) or 0
        return {
            "active_sessions": len(_SESSIONS),
            # 兼容旧字段（会话口径）
            "total_cost_usd": round(total_cost, 6),
            "total_turns": total_turns,
            "session_usage": {
                "sessions": len(_SESSIONS),
                "total_cost_usd": round(total_cost, 6),
                "total_turns": total_turns,
            },
            "oneshot_usage": {
                "runs": _ONESHOT_TOTALS["runs"],
                "errors": _ONESHOT_TOTALS["errors"],
                "total_cost_usd": round(_ONESHOT_TOTALS["total_cost_usd"], 6),
                "total_turns": _ONESHOT_TOTALS["total_turns"],
            },
            "default_persona": _DEFAULT_PERSONA,
            "model": config.model,
            "memory_backend": config.memory_backend,
        }

    @app.get("/agent/metrics")
    def agent_metrics() -> dict:
        """可观测性指标：延迟分布、错误分类、纠错统计。

        返回 JSON 格式（便于前端展示）；同时支持 Prometheus text 格式
        （Accept: text/plain 时）。
        """
        durations = sorted(_METRICS["durations_ms"])
        n = len(durations)
        p50 = durations[n // 2] if n > 0 else 0
        p95 = durations[int(n * 0.95)] if n > 0 else 0
        p99 = durations[int(n * 0.99)] if n > 0 else 0
        return {
            "runs_total": _ONESHOT_TOTALS["runs"],
            "errors_total": _ONESHOT_TOTALS["errors"],
            "cost_usd_total": round(_ONESHOT_TOTALS["total_cost_usd"], 6),
            "duration_ms": {"p50": p50, "p95": p95, "p99": p99, "count": n},
            "error_types": dict(_METRICS["error_types"]),
            "corrections_total": _METRICS["total_corrections"],
            "active_sessions": len(_SESSIONS),
        }

    # ---- 运行历史 / 用量统计 / Trace 查询 ----

    @app.get("/agent/history")
    def agent_history(limit: int = 50, offset: int = 0,
                      status: str = "all") -> dict:
        """查询运行历史：分页 + 状态过滤。"""
        if status not in ("all", "success", "error"):
            return _error(400, "invalid_status",
                          "status 参数无效，可选: all | success | error")
        store = _get_history_store()
        records, total = store.query(limit=limit, offset=offset, status=status)
        # 移除 trace 字段（太大，不在列表接口返回）
        for r in records:
            r.pop("trace", None)
        return {
            "records": records,
            "total": total,
            "limit": limit,
            "offset": offset,
        }

    @app.get("/agent/stats")
    def agent_stats(period: str = "daily", days: int = 7) -> dict:
        """用量趋势统计：按天/小时聚合。"""
        from datetime import datetime, timedelta, timezone
        if period not in ("daily", "hourly"):
            return _error(400, "invalid_period", "period 参数无效，可选: daily | hourly")
        days = max(1, min(days, 90))  # 限制 1-90 天
        since = datetime.now(timezone.utc) - timedelta(days=days)
        store = _get_history_store()
        records = store.get_records_since(since)

        # 按时间聚合
        buckets: dict[str, dict] = {}
        for r in records:
            ts = r.get("timestamp", "")
            if not ts:
                continue
            try:
                dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            except ValueError:
                continue
            if period == "daily":
                key = dt.strftime("%Y-%m-%d")
            else:  # hourly
                key = dt.strftime("%Y-%m-%d %H:00")
            if key not in buckets:
                buckets[key] = {"runs": 0, "errors": 0, "cost_usd": 0.0,
                                "total_duration_ms": 0}
            b = buckets[key]
            b["runs"] += 1
            if r.get("is_error"):
                b["errors"] += 1
            b["cost_usd"] += r.get("cost_usd") or 0.0
            b["total_duration_ms"] += r.get("duration_ms") or 0

        # 格式化输出
        bucket_list = []
        for key in sorted(buckets.keys()):
            b = buckets[key]
            runs = b["runs"]
            bucket_list.append({
                "date" if period == "daily" else "hour": key,
                "runs": runs,
                "errors": b["errors"],
                "cost_usd": round(b["cost_usd"], 6),
                "avg_duration_ms": b["total_duration_ms"] // runs if runs else 0,
                "success_rate": round((runs - b["errors"]) / runs, 2) if runs else 0,
            })

        # 汇总
        total_runs = sum(b["runs"] for b in bucket_list)
        total_cost = sum(b["cost_usd"] for b in bucket_list)
        total_errors = sum(b["errors"] for b in bucket_list)
        return {
            "period": period,
            "days": days,
            "buckets": bucket_list,
            "summary": {
                "total_runs": total_runs,
                "total_cost_usd": round(total_cost, 6),
                "avg_cost_per_run": round(total_cost / total_runs, 6) if total_runs else 0,
                "overall_success_rate": round((total_runs - total_errors) / total_runs, 2)
                    if total_runs else 0,
            },
        }

    @app.get("/agent/trace/{run_id}")
    def agent_trace(run_id: str) -> dict:
        """查询单次执行的 trace 详情。

        面板的 run_id（runs/<id>）与历史库 run_id 现已统一；仍兼容
        仅存在于历史库的旧记录（先查历史，miss 再回退读事件流末帧结果）。
        """
        store = _get_history_store()
        record = store.get_by_run_id(run_id)
        if record is None:
            # 面板 run：从事件 JSONL 的 result 帧还原统计（无 trace 树）
            events_path = _runs_dir() / f"{run_id}.jsonl"
            if events_path.exists():
                last_result = None
                with open(events_path, encoding="utf-8") as f:
                    for line in f:
                        try:
                            e = json.loads(line)
                        except ValueError:
                            continue
                        if e.get("type") == "result":
                            last_result = e.get("data")
                if last_result is not None:
                    return {"run_id": run_id, "trace": None,
                            "summary": {k: last_result.get(k) for k in
                                        ("num_turns", "cost_usd", "duration_ms",
                                         "is_error")},
                            "note": "该 run 未落 trace 树（流式路径），以上为终态统计"}
            return _error(404, "not_found", f"运行记录不存在: {run_id}")
        trace = record.get("trace")
        if trace is None:
            return {"trace": None, "message": "该次执行未记录 trace"}
        return {"run_id": run_id, "trace": trace}

    return app


# 模块级 app：供 `uvicorn agent_core.service:app` 直接引用
app = create_app(os.environ.get("AGENT_PERSONA", "executor"))


def main(argv: list[str] | None = None) -> int:
    """命令行入口：python -m agent_core.service --port 8000 --persona executor。

    启动前跑一遍 doctor 自检并 log 告警/失败项（不阻断启动），结果缓存供
    /health 的 checks 汇总直接复用。
    """
    global _HEALTH_CHECKS
    parser = argparse.ArgumentParser(description="单机 Agent HTTP 服务")
    parser.add_argument("--host", default=os.environ.get("AGENT_HOST", "127.0.0.1"),
                        help="监听地址（默认 127.0.0.1）")
    parser.add_argument("--port", type=int,
                        default=int(os.environ.get("AGENT_PORT", "8000")),
                        help="监听端口（默认 8000）")
    parser.add_argument("--persona",
                        default=os.environ.get("AGENT_PERSONA", "executor"),
                        help="默认人设（默认 executor）")
    parser.add_argument("--reload", action="store_true", help="开发热重载")
    args = parser.parse_args(argv)

    # 启动自检：只读检查，失败/告警只 log 不阻断
    try:
        checks = run_checks(get_config(), default_persona=args.persona)
        _HEALTH_CHECKS = checks
        for c in checks:
            if c.status == "fail":
                logger.error("doctor[%s] %s", c.name, c.detail)
            elif c.status == "warn":
                logger.warning("doctor[%s] %s", c.name, c.detail)
    except Exception as e:  # noqa: BLE001 - 自检失败不阻断启动
        logger.warning("doctor 自检异常（忽略）： %s", e)

    import uvicorn

    # 指定 persona 时重建 app（模块级 app 已按 AGENT_PERSONA 装配一次）
    target = create_app(args.persona)
    uvicorn.run(target, host=args.host, port=args.port, reload=args.reload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
