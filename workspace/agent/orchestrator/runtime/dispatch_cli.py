"""大脑↔runtime 桥：CLI 入口让主会话派发 executor。

大脑主会话是 prompt 语义的 skill，无法直接 import Python。本脚本提供
可执行入口，主会话通过 Bash 调用：

    python dispatch_cli.py "<prompt>"

脚本不自行持有 Dispatcher——派发与 worker 注册状态须在同一进程
（brain_api），因此本脚本转发 HTTP 到运行中的 brain_api 的 /dispatch 端点。
brain_url 由 ORCH_BRAIN_URL 指定（默认 http://127.0.0.1:7000）。
鉴权：配置了 API key 的部署（ORCH_API_KEY）要求 X-API-Key 头，本脚本
从 ORCH_API_KEY / AGENT_API_KEY 环境变量读取，存在才携带。
"""
import json
import os
import sys

import requests


def dispatch(prompt: str, role: str = "executor",
             context_memory: str | None = None, model: str | None = None,
             worker_id: str | None = None) -> dict:
    brain_url = os.environ.get("ORCH_BRAIN_URL", "http://127.0.0.1:7000")
    # 鉴权部署：brain 配置了 key 时拒绝无 X-API-Key 的请求；存在才携带
    headers = {}
    api_key = os.environ.get("ORCH_API_KEY") or os.environ.get("AGENT_API_KEY")
    if api_key:
        headers["X-API-Key"] = api_key
    body: dict = {
        "prompt": prompt, "role": role,
        "context_memory": context_memory, "model": model,
    }
    if worker_id:
        body["worker_id"] = worker_id
    resp = requests.post(f"{brain_url}/dispatch", json=body,
                         headers=headers, timeout=120)
    try:
        return resp.json()
    except ValueError:
        return {"status": "failed", "error": f"brain 返回非 JSON: {resp.text}"}


def main() -> int:
    prompt = sys.argv[1] if len(sys.argv) > 1 else ""
    role = os.environ.get("ORCH_ROLE", "executor")
    ctx = os.environ.get("ORCH_CONTEXT_MEMORY") or None
    model = os.environ.get("ORCH_MODEL") or None
    worker_id = os.environ.get("ORCH_WORKER_TARGET") or None
    res = dispatch(prompt, role=role, context_memory=ctx, model=model,
                   worker_id=worker_id)
    print(json.dumps(res, ensure_ascii=False))
    return 0 if res.get("status") == "done" else 1


if __name__ == "__main__":
    sys.exit(main())
