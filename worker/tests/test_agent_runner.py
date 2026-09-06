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
