"""大脑 MCP 配套服务：把编排框架能力以 MCP 标准暴露给任意客户端。

架构定位：与 dispatch_cli / Web 面板并列，MCP Server 是 brain_api 的又一个
客户端（经 HTTP 调用），自身无状态。任何 MCP 客户端（Claude Code、ZCode、
其他 agent 运行时）注册本服务后，即可把「派发任务/查任务/读写记忆/检索
经验/程序记忆演化」当作标准工具调用。

启动：python mcp_server.py   （stdio 传输，环境变量 ORCH_BRAIN_URL / ORCH_API_KEY）
工具面刻意收敛在 18 个以内（避免 GitHub MCP 90+ 工具烧 46k token 的反面教材）。
"""
import os
from typing import Any

import requests
from mcp.server.fastmcp import FastMCP

mcp = FastMCP(
    "orchestrator",
    instructions="多 agent 编排框架的大脑服务。派发任务请用 dispatch_task；"
                 "续做历史任务传 context_task_ids 引用其上下文记忆；"
                 "修改技能定义必须走程序记忆演化提案，不要直接改文件。",
)


class BrainClient:
    """brain_api HTTP 客户端。http 可注入便于测试。"""

    def __init__(self, brain_url: str | None = None, api_key: str | None = None,
                 http=None, timeout: int = 60):
        self.brain_url = (brain_url or os.environ.get("ORCH_BRAIN_URL",
                                                      "http://127.0.0.1:7000")).rstrip("/")
        self.api_key = api_key if api_key is not None else os.environ.get("ORCH_API_KEY", "")
        self.timeout = timeout
        self._http = http  # (method, url, json_body, params) -> dict，测试注入

    def call(self, method: str, path: str, json_body: dict | None = None,
             params: dict | None = None) -> Any:
        if self._http is not None:
            return self._http(method, self.brain_url + path, json_body, params)
        headers = {"X-API-Key": self.api_key} if self.api_key else {}
        try:
            resp = requests.request(method, self.brain_url + path, json=json_body,
                                    params=params, timeout=self.timeout,
                                    headers=headers)
        except Exception as e:
            return {"status": "error", "detail": f"brain 不可达: {e}"}
        try:
            body = resp.json()
        except ValueError:
            body = {"raw": resp.text}
        if resp.status_code >= 400:
            return {"status": "error", "http_status": resp.status_code, "detail": body}
        return body


_client: BrainClient | None = None


def get_client() -> BrainClient:
    global _client
    if _client is None:
        _client = BrainClient()
    return _client


def set_client(client: BrainClient | None) -> None:
    """测试注入。"""
    global _client
    _client = client


def _strip_embedding(rec: dict) -> dict:
    """经验记录里的 embedding 向量对客户端无用且巨大，剥离后再返回。"""
    return {k: v for k, v in rec.items() if k != "embedding"} if isinstance(rec, dict) else rec


# ---- 派发与任务 ----

@mcp.tool()
def dispatch_task(prompt: str, role: str = "executor", model: str | None = None,
                  context_memory: str | None = None,
                  context_task_ids: list[str] | None = None) -> Any:
    """同步派发一个任务给编排框架执行，返回最终结果（output/artifacts/verdict/用量）。

    prompt 是给 executor 的完整任务说明（必要信息最小集，token 纪律：大段背景
    放 context_memory 或用 context_task_ids 引用历史任务，不要整段粘贴）。
    role: executor（执行）/ security-guard（只读检验）。model: 覆盖模型（如
    sonnet/haiku），不传走默认路由。context_task_ids: 引用历史任务的上下文
    记忆笔记并入本次派发（续做任务时用它）。
    """
    body: dict = {"prompt": prompt, "role": role}
    if model:
        body["model"] = model
    if context_memory:
        body["context_memory"] = context_memory
    if context_task_ids:
        body["context_task_ids"] = context_task_ids
    return get_client().call("POST", "/dispatch", json_body=body)


@mcp.tool()
def dispatch_task_async(prompt: str, role: str = "executor", model: str | None = None,
                        context_memory: str | None = None,
                        context_task_ids: list[str] | None = None) -> Any:
    """异步派发：立即返回 task_id，结果经 get_task_status 轮询。

    参数含义同 dispatch_task。适合长任务（前端页面/后端服务开发）。
    """
    body: dict = {"prompt": prompt, "role": role}
    if model:
        body["model"] = model
    if context_memory:
        body["context_memory"] = context_memory
    if context_task_ids:
        body["context_task_ids"] = context_task_ids
    return get_client().call("POST", "/dispatch/async", json_body=body)


@mcp.tool()
def get_task_status(task_id: str) -> Any:
    """查任务详情：状态、output、artifacts、error、用量与缓存指标（meta）。"""
    return get_client().call("GET", f"/tasks/{task_id}")


@mcp.tool()
def list_recent_tasks(limit: int = 20, status: str | None = None) -> Any:
    """列出任务（新→旧）。status 可选 pending/running/done/failed/lost/cancelled。"""
    tasks = get_client().call("GET", "/tasks")
    if isinstance(tasks, dict):  # 错误响应透传
        return tasks
    if status:
        tasks = [t for t in tasks if t.get("status") == status]
    return tasks[:max(0, limit)]


@mcp.tool()
def cancel_task(task_id: str) -> Any:
    """取消 pending/running 状态的任务（传播到 worker，runner 在消息边界协作退出）。"""
    return get_client().call("POST", f"/tasks/{task_id}/cancel")


@mcp.tool()
def brain_stats() -> Any:
    """编排系统总览：worker 数、任务状态分布、经验库规模、最近任务。"""
    return get_client().call("GET", "/stats")


# ---- 记忆 ----

@mcp.tool()
def list_core_memories() -> Any:
    """列出全部核心记忆（持久、跨任务共识写入的内容）。"""
    return get_client().call("GET", "/memory/core")


@mcp.tool()
def read_core_memory(name: str) -> Any:
    """读一条核心记忆。name 为记忆名（如 proj-pref）。"""
    return get_client().call("GET", f"/memory/core/{name}")


@mcp.tool()
def write_core_memory(name: str, content: str) -> Any:
    """写核心记忆。

    注意：核心记忆是共识写入的高价值内容（应遵循大脑记忆层的门控流程，
    如三方共识投票），不要把临时过程信息写进来——那些用 write_task_context。
    """
    return get_client().call("PUT", f"/memory/core/{name}", json_body={"content": content})


@mcp.tool()
def delete_core_memory(name: str) -> Any:
    """删除一条核心记忆（过时信息清理；删除也应走共识门控）。"""
    return get_client().call("DELETE", f"/memory/core/{name}")


@mcp.tool()
def read_task_context(task_id: str) -> Any:
    """读某任务的全部上下文记忆笔记（含任务终态自动写入的 result 笔记）。

    续做任务前先读它，配合 dispatch_task 的 context_task_ids 使用。
    """
    return get_client().call("GET", f"/memory/context/{task_id}")


@mcp.tool()
def write_task_context(task_id: str, key: str, content: str) -> Any:
    """写某任务的上下文记忆笔记（中间产物路径、已试方案、临时决策等过程信息）。"""
    return get_client().call("PUT", f"/memory/context/{task_id}/{key}",
                             json_body={"content": content})


@mcp.tool()
def archive_task_context(task_id: str) -> Any:
    """任务结束归档：清理该任务全部上下文记忆笔记。"""
    return get_client().call("DELETE", f"/memory/context/{task_id}")


# ---- 经验库 ----

@mcp.tool()
def search_experiences(query: str, role: str | None = None,
                       successful_only: bool = True, limit: int = 5) -> Any:
    """按关键词+语义混合检索历史任务经验（prompt/输出摘要/安全裁决）。

    做新任务前先检索相似经验，避免重复踩坑。role 可按角色过滤。
    """
    result = get_client().call("POST", "/experiences/search", json_body={
        "query": query, "role": role,
        "successful_only": successful_only, "limit": limit,
    })
    if isinstance(result, list):
        return [_strip_embedding(r) for r in result]
    return result


# ---- 程序记忆演化（技能定义的提案-共识-应用） ----

@mcp.tool()
def procedural_reflection(window: int = 50) -> Any:
    """近期经验的确定性反思报告：失败任务清单、各角色成功率、高频命中经验。

    修改技能定义（SKILL.md/executor.md）前必读——提案要有证据。
    """
    return get_client().call("GET", "/memory/procedural/reflection",
                             params={"window": window})


@mcp.tool()
def list_procedural_proposals(status: str | None = None) -> Any:
    """列出程序记忆演化提案。status: pending/applied/rejected。"""
    params = {"status": status} if status else None
    return get_client().call("GET", "/memory/procedural/proposals", params=params)


@mcp.tool()
def propose_procedural_revision(target_path: str, content: str, reason: str,
                                evidence_exp_ids: list[str] | None = None) -> Any:
    """提交技能定义修订提案（程序记忆演化）。

    target_path 限 workspace/agent/skills/ 与 .claude/agents/ 下既有文件
    （只修订不新建）。创建时 security 规则引擎自动裁决，之后需 brain 与
    executor 各投一票（vote_procedural_proposal），三票全 pass 自动应用。
    reason 写清为什么要改，evidence_exp_ids 给出支撑证据的经验记录 id。
    """
    return get_client().call("POST", "/memory/procedural/proposals", json_body={
        "target_path": target_path, "content": content, "reason": reason,
        "evidence_exp_ids": evidence_exp_ids or [],
    })


@mcp.tool()
def vote_procedural_proposal(proposal_id: str, voter: str, verdict: str,
                             reason: str = "") -> Any:
    """给演化提案投票。voter: brain/executor（security 已自动投）；
    verdict: pass/block。三票全 pass 自动应用，任一 block 即否决。"""
    return get_client().call("POST", f"/memory/procedural/proposals/{proposal_id}/vote",
                             json_body={"voter": voter, "verdict": verdict,
                                        "reason": reason})


if __name__ == "__main__":
    mcp.run()
