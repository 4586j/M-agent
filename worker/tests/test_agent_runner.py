import sys
import threading
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agent_runner import (FakeAgentRunner, SdkAgentRunner, TaskRequest,
                          build_env_block, scan_skills)

# 项目根 = worker/tests/ 的上两级，用于验证技能扫描对真实仓库生效
PROJECT_ROOT = str(Path(__file__).resolve().parents[2])


def test_fake_runner_done_on_simple_prompt(tmp_path):
    runner = FakeAgentRunner(work_dir=str(tmp_path))
    req = TaskRequest(task_id="task-1", prompt="分析数据", context_memory="", model=None)
    res = runner.run(req)
    assert res.task_id == "task-1"
    assert res.status == "done"
    assert res.error is None
    assert isinstance(res.artifacts, list)


def test_fake_runner_failed_on_error_keyword(tmp_path):
    runner = FakeAgentRunner(work_dir=str(tmp_path))
    req = TaskRequest(task_id="task-2", prompt="FORCE_ERROR 触发失败", context_memory="", model=None)
    res = runner.run(req)
    assert res.status == "failed"
    assert res.error is not None


def test_scan_skills_finds_real_skills():
    skills = scan_skills(PROJECT_ROOT)
    names = {s["name"] for s in skills}
    assert {"data-expert", "web-expert", "back-expert"} <= names
    # orchestrator 是大脑定义，不注入给 executor
    assert "orchestrator" not in names
    for s in skills:
        assert Path(s["path"]).is_absolute()
        assert Path(s["path"], "SKILL.md").is_file()


def test_scan_skills_empty_dir(tmp_path):
    assert scan_skills(str(tmp_path)) == []


def test_build_env_block_lists_skills_with_abs_paths(tmp_path):
    skills = scan_skills(PROJECT_ROOT)
    block = build_env_block(PROJECT_ROOT, skills)
    assert "运行环境" in block
    assert str(Path(PROJECT_ROOT).resolve()) in block  # 项目根
    assert str(tmp_path) not in block  # 任务目录不得进 system prompt（KV-cache 稳定）
    for s in skills:
        assert s["name"] in block
        assert s["path"] in block


def _install_fake_sdk(monkeypatch, results, delay=0.0):
    """注入假 claude_agent_sdk：results 为 ResultMessage 参数字典列表。
    捕获传入的 prompt 与 options 供断言。返回捕获容器。"""
    captured = {}

    class ClaudeAgentOptions:
        def __init__(self, **kw):
            captured.update(kw)

    class ResultMessage:
        def __init__(self):
            for k, v in results[captured.get("_call_index", 0)].items():
                setattr(self, k, v)

    def query(prompt, options):
        captured["prompt"] = prompt

        async def gen():
            import asyncio
            for i, _ in enumerate(results):
                captured["_call_index"] = i
                if delay:
                    await asyncio.sleep(delay)
                yield ResultMessage()
        return gen()

    fake = types.ModuleType("claude_agent_sdk")
    fake.ClaudeAgentOptions = ClaudeAgentOptions
    fake.ResultMessage = ResultMessage
    fake.query = query
    monkeypatch.setitem(sys.modules, "claude_agent_sdk", fake)
    return captured


def test_sdk_runner_injects_env_block_into_system_prompt(tmp_path, monkeypatch):
    captured = _install_fake_sdk(
        monkeypatch, [{"result": "ok", "is_error": False, "num_turns": 3}])
    runner = SdkAgentRunner(work_dir=str(tmp_path),
                            executor_def_path=str(Path(PROJECT_ROOT) / ".claude" / "agents" / "executor.md"),
                            project_root=PROJECT_ROOT)
    res = runner.run(TaskRequest(task_id="task-env", prompt="做个页面",
                                 context_memory="先读 tokens", model=None))
    assert res.status == "done"
    assert "运行环境" in captured["system_prompt"]
    assert "web-expert" in captured["system_prompt"]
    task_dir = str(tmp_path / "task-env")
    # KV-cache：任务目录是每任务变化的信息，只能进用户消息，不得进 system prompt
    assert task_dir not in captured["system_prompt"]
    assert task_dir in captured["prompt"]
    assert "先读 tokens" in captured["prompt"]
    assert captured["cwd"] == task_dir


def test_sdk_runner_is_error_maps_to_failed(tmp_path, monkeypatch):
    _install_fake_sdk(monkeypatch,
                      [{"result": "炸了", "is_error": True, "num_turns": 5}])
    runner = SdkAgentRunner(work_dir=str(tmp_path),
                            executor_def_path=str(Path(PROJECT_ROOT) / ".claude" / "agents" / "executor.md"),
                            project_root=PROJECT_ROOT)
    res = runner.run(TaskRequest(task_id="task-err", prompt="p", context_memory="", model=None))
    assert res.status == "failed"
    assert res.error is not None
    assert res.meta["num_turns"] == 5


def test_sdk_runner_cancelled_mid_session(tmp_path, monkeypatch):
    _install_fake_sdk(monkeypatch,
                      [{"result": "partial", "is_error": False, "num_turns": 1}])
    runner = SdkAgentRunner(work_dir=str(tmp_path),
                            executor_def_path=str(Path(PROJECT_ROOT) / ".claude" / "agents" / "executor.md"),
                            project_root=PROJECT_ROOT)
    ev = threading.Event()
    ev.set()  # 预先置位：第一条消息后即应退出
    res = runner.run(TaskRequest(task_id="task-cancel", prompt="p",
                                 context_memory="", model=None, cancel_event=ev))
    assert res.status == "cancelled"
    assert res.output == "partial"


def test_sdk_runner_timeout(tmp_path, monkeypatch):
    _install_fake_sdk(monkeypatch,
                      [{"result": "late", "is_error": False, "num_turns": 1}],
                      delay=2.0)
    runner = SdkAgentRunner(work_dir=str(tmp_path),
                            executor_def_path=str(Path(PROJECT_ROOT) / ".claude" / "agents" / "executor.md"),
                            project_root=PROJECT_ROOT, task_timeout=1)
    res = runner.run(TaskRequest(task_id="task-slow", prompt="p", context_memory="", model=None))
    assert res.status == "failed"
    assert "timeout" in str(res.error)


def test_sdk_runner_cost_controls_and_usage(tmp_path, monkeypatch):
    captured = _install_fake_sdk(monkeypatch, [{
        "result": "ok", "is_error": False, "num_turns": 2,
        "usage": {"input_tokens": 100, "output_tokens": 50,
                  "cache_read_input_tokens": 800,
                  "cache_creation_input_tokens": 200},
        "total_cost_usd": 0.012, "duration_ms": 4321,
    }])
    runner = SdkAgentRunner(work_dir=str(tmp_path),
                            executor_def_path=str(Path(PROJECT_ROOT) / ".claude" / "agents" / "executor.md"),
                            project_root=PROJECT_ROOT,
                            max_turns=7, max_thinking_tokens=4096, task_budget_usd=0.5)
    res = runner.run(TaskRequest(task_id="task-cost", prompt="p",
                                 context_memory="", model=None))
    assert res.status == "done"
    # 成本控制项透传给 SDK options
    assert captured["max_turns"] == 7
    assert captured["max_thinking_tokens"] == 4096
    assert captured["max_budget_usd"] == 0.5
    # 用量与缓存指标进 meta（缓存命中率可观测的前提）
    assert res.meta["usage"]["cache_read_input_tokens"] == 800
    assert res.meta["usage"]["cache_creation_input_tokens"] == 200
    assert res.meta["cost_usd"] == 0.012
    assert res.meta["duration_ms"] == 4321


def test_sdk_runner_cost_controls_disabled(tmp_path, monkeypatch):
    captured = _install_fake_sdk(monkeypatch,
                                 [{"result": "ok", "is_error": False, "num_turns": 1}])
    runner = SdkAgentRunner(work_dir=str(tmp_path),
                            executor_def_path=str(Path(PROJECT_ROOT) / ".claude" / "agents" / "executor.md"),
                            project_root=PROJECT_ROOT,
                            max_thinking_tokens=0, task_budget_usd=0)
    runner.run(TaskRequest(task_id="task-nocost", prompt="p", context_memory="", model=None))
    assert "max_thinking_tokens" not in captured
    assert "max_budget_usd" not in captured


def test_load_mcp_servers(tmp_path):
    from agent_runner import load_mcp_servers
    # 配置缺失/空路径
    assert load_mcp_servers("") is None
    assert load_mcp_servers(str(tmp_path / "no.json")) is None
    # 正常配置：stdio + http 两种形状
    cfg_file = tmp_path / "mcp.json"
    cfg_file.write_text('{"mcpServers": {"fs": {"command": "npx", "args": ["-y", "x"]},'
                        ' "figma": {"type": "http", "url": "http://127.0.0.1:3845/mcp"}},'
                        ' "other": {"broken": true}}', encoding="utf-8")
    servers = load_mcp_servers(str(cfg_file))
    assert set(servers.keys()) == {"fs", "figma"}  # 缺 command/url 的条目被过滤
    assert servers["fs"]["command"] == "npx"


def test_sdk_runner_passes_mcp_servers(tmp_path, monkeypatch):
    captured = _install_fake_sdk(monkeypatch,
                                 [{"result": "ok", "is_error": False, "num_turns": 1}])
    mcp_servers = {"fs": {"command": "npx", "args": ["-y", "x"]}}
    runner = SdkAgentRunner(work_dir=str(tmp_path),
                            executor_def_path=str(Path(PROJECT_ROOT) / ".claude" / "agents" / "executor.md"),
                            project_root=PROJECT_ROOT, mcp_servers=mcp_servers)
    runner.run(TaskRequest(task_id="task-mcp", prompt="p", context_memory="", model=None))
    assert captured["mcp_servers"] == mcp_servers  # MCP 工具挂载进 SDK 会话
