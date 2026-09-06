import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from mcp_server import BrainClient, get_client, mcp, set_client


class FakeHttp:
    """记录调用并返回预设响应，替代真实 HTTP。"""

    def __init__(self, responses=None):
        self.calls = []
        self.responses = responses or {}

    def __call__(self, method, url, json_body=None, params=None):
        self.calls.append({"method": method, "url": url,
                           "json": json_body, "params": params})
        return self.responses.get((method, url.split("?")[0]), {"ok": True})


def test_client_sends_expected_calls():
    http = FakeHttp()
    client = BrainClient(brain_url="http://brain:7000", api_key="secret", http=http)
    out = client.call("POST", "/dispatch", json_body={"prompt": "p"})
    assert out == {"ok": True}
    assert http.calls[0]["url"] == "http://brain:7000/dispatch"
    assert http.calls[0]["json"] == {"prompt": "p"}


def test_client_unreachable_returns_error_dict():
    # 指向必然拒绝连接的端口：错误以结构化 dict 返回而非抛异常，LLM 可读
    client = BrainClient(brain_url="http://127.0.0.1:1", api_key="", timeout=2)
    out = client.call("GET", "/stats")
    assert out["status"] == "error"
    assert "brain 不可达" in out["detail"]


def test_registered_tools_cover_framework_capabilities():
    tools = asyncio.run(mcp.list_tools())
    names = {t.name for t in tools}
    expected = {
        "dispatch_task", "dispatch_task_async", "get_task_status",
        "list_recent_tasks", "cancel_task", "brain_stats",
        "list_core_memories", "read_core_memory", "write_core_memory",
        "delete_core_memory", "read_task_context", "write_task_context",
        "archive_task_context", "search_experiences",
        "procedural_reflection", "list_procedural_proposals",
        "propose_procedural_revision", "vote_procedural_proposal",
    }
    assert expected <= names  # 能力全覆盖
    assert len(names) <= 20  # 工具面收敛，防止 context 膨胀


def test_tools_route_through_client():
    from mcp_server import list_core_memories, list_recent_tasks, search_experiences
    http = FakeHttp(responses={
        ("POST", "http://brain:7000/experiences/search"): [
            {"exp_id": "exp-1", "prompt": "p", "embedding": [0.1] * 384},
            {"exp_id": "exp-2", "prompt": "q", "embedding": [0.2] * 384},
        ],
        ("GET", "http://brain:7000/memory/core"): {"memories": [{"name": "a", "content": "b"}]},
        ("GET", "http://brain:7000/tasks"): [
            {"task_id": "t1", "status": "done"},
            {"task_id": "t2", "status": "running"},
        ],
    })
    set_client(BrainClient(brain_url="http://brain:7000", http=http))
    try:
        result = search_experiences("查询")
        assert "embedding" not in result[0]  # 大向量剥离，MCP 响应保持精简
        assert result[0]["exp_id"] == "exp-1"
        list_core_memories()
        assert http.calls[-1]["url"].endswith("/memory/core")
        tasks = list_recent_tasks(status="done")
        assert [t["task_id"] for t in tasks] == ["t1"]  # 状态过滤在客户端做
    finally:
        set_client(None)
    assert get_client() is not None  # 恢复默认懒加载
