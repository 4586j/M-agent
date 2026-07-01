"""大脑↔runtime 桥：CLI 入口让主会话派发 executor。

大脑主会话是 prompt 语义的 skill，无法直接 import Python。本脚本提供
可执行入口，主会话通过 Bash 调用：

    python -m workspace.agent.orchestrator.runtime.dispatch_cli "<prompt>"

脚本读取 ORCH_MODE 决定走 LocalDispatcher（V1 单机，回退主会话 Agent 工具）
还是 RemoteDispatcher（V2 跨主机，HTTP 派发到 worker）。结果输出为 JSON
到 stdout，主会话据此判定。

RemoteDispatcher 的 HTTP 客户端用 requests 真实调用 worker。
"""
import json
import os
import sys

import requests

from config import load_config
from dispatcher import DispatchResult, LocalDispatcher, RemoteDispatcher
from task_store import TaskStore
from worker_registry import WorkerRegistry


def _http_call(method: str, url: str, **kwargs) -> dict:
    """真实 HTTP 客户端，签名与 dispatcher 期望一致。"""
    resp = requests.request(method, url, timeout=30, **kwargs)
    try:
        body = resp.json()
    except ValueError:
        body = {"raw": resp.text}
    return {"status_code": resp.status_code, "json": body}


def build_dispatcher(cfg):
    store = TaskStore(cfg.task_store_dir)
    if cfg.mode == "remote":
        registry = WorkerRegistry(heartbeat_timeout=cfg.heartbeat_timeout)
        return RemoteDispatcher(task_store=store, registry=registry, http_call=_http_call)
    return LocalDispatcher(task_store=store)


def dispatch(prompt: str, role: str = "executor",
             context_memory: str | None = None, model: str | None = None) -> DispatchResult:
    cfg = load_config()
    d = build_dispatcher(cfg)
    return d.dispatch(role=role, prompt=prompt, context_memory=context_memory, model=model)


def main() -> int:
    prompt = sys.argv[1] if len(sys.argv) > 1 else ""
    role = os.environ.get("ORCH_ROLE", "executor")
    ctx = os.environ.get("ORCH_CONTEXT_MEMORY") or None
    model = os.environ.get("ORCH_MODEL") or None
    try:
        res = dispatch(prompt, role=role, context_memory=ctx, model=model)
    except RuntimeError as e:
        print(json.dumps({"status": "failed", "error": str(e)}, ensure_ascii=False))
        return 1
    print(json.dumps({
        "task_id": res.task_id, "status": res.status,
        "output": res.output, "artifacts": res.artifacts, "error": res.error,
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
