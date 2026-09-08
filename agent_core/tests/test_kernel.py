"""测试 agent_core.kernel：AgentKernel / AgentSession / 事件翻译。

全程用 monkeypatch 注入假 claude_agent_sdk，不触网、不依赖真实 SDK 行为。
假消息类用真实类名（TextBlock/AssistantMessage/ResultMessage...），
使 kernel 里 ``type(msg).__name__`` 的弱耦合判断自然命中。
"""
import asyncio
import sys
import threading
import types
from pathlib import Path

import pytest

from agent_core.config import load_agent_config
from agent_core.kernel import (AgentEvent, AgentKernel, AgentSession,
                               KernelResult, _msg_to_events)
from agent_core.persona import Persona


# ---- 假 SDK 消息类型（类名必须与 kernel 判断一致）----

class TextBlock:
    def __init__(self, text):
        self.text = text


class ToolUseBlock:
    def __init__(self, name, input=None):
        self.name = name
        self.input = input if input is not None else {}


class ToolResultBlock:
    def __init__(self, content):
        self.content = content


class AssistantMessage:
    def __init__(self, content):
        self.content = content


class UserMessage:
    def __init__(self, content):
        self.content = content


class ResultMessage:
    def __init__(self, result="", num_turns=1, is_error=False, usage=None,
                 total_cost_usd=None, duration_ms=100,
                 session_id="test-session-123"):
        self.result = result
        self.num_turns = num_turns
        self.is_error = is_error
        self.usage = usage
        self.total_cost_usd = total_cost_usd
        self.duration_ms = duration_ms
        self.session_id = session_id


def _install_fake_sdk(monkeypatch, query_messages, delay=0.0,
                      client_messages=None, raise_on_query=False):
    """注入假 claude_agent_sdk 模块，返回捕获容器。

    - query(prompt, options)：异步生成器，逐条产出 query_messages；
      delay>0 时每条前 sleep（用于超时测试）；raise_on_query 时首抛异常。
    - ClaudeAgentOptions(**kwargs)：捕获 kwargs 供断言，允许后续属性赋值。
    - ClaudeSDKClient(options)：connect/disconnect/interrupt/query 全 mock。
    """
    captured = {"options_kwargs": None, "prompt": None, "client": None}

    class ClaudeAgentOptions:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)
            captured["options_kwargs"] = kwargs

    def query(prompt, options):
        captured["prompt"] = prompt

        async def gen():
            if raise_on_query:
                raise RuntimeError("boom")
            for msg in query_messages:
                if delay:
                    await asyncio.sleep(delay)
                yield msg
        return gen()

    class ClaudeSDKClient:
        def __init__(self, options=None):
            self.options = options
            self.connected = False
            self.disconnected = False
            self.interrupted = False
            captured["client"] = self

        async def connect(self):
            self.connected = True

        async def disconnect(self):
            self.disconnected = True
            self.connected = False

        async def interrupt(self):
            self.interrupted = True

        def query(self, prompt=None, session_id=None):
            captured["client_prompt"] = prompt
            msgs = client_messages if client_messages is not None else query_messages

            async def gen():
                for m in msgs:
                    yield m
            return gen()

    fake = types.ModuleType("claude_agent_sdk")
    fake.ClaudeAgentOptions = ClaudeAgentOptions
    fake.query = query
    fake.ClaudeSDKClient = ClaudeSDKClient
    monkeypatch.setitem(sys.modules, "claude_agent_sdk", fake)
    return captured


def _kernel_config(project_root, tmp_path, **overrides):
    """构造隔离配置：project_root 用真实仓库（供 scan_skills / runtime import），
    记忆与经验目录指向 tmp_path，避免污染真实数据。"""
    return load_agent_config(
        project_root=project_root,
        memory_backend="local",
        memory_core_dir=str(tmp_path / "mem" / "core"),
        memory_context_dir=str(tmp_path / "mem" / "context"),
        experience_store_dir=str(tmp_path / "exp"),
        mcp_config_path="",
        **overrides,
    )


# ---- AgentKernel.__init__ ----

def test_kernel_init_stores_config(project_root, tmp_path):
    cfg = _kernel_config(project_root, tmp_path)
    kernel = AgentKernel(cfg)
    assert kernel.config is cfg


# ---- _build_options ----

def test_build_options_constructs_valid_options(monkeypatch, project_root, tmp_path):
    captured = _install_fake_sdk(monkeypatch, [])
    cfg = _kernel_config(project_root, tmp_path, max_turns=7,
                         max_thinking_tokens=4096, task_budget_usd=0.5,
                         model="sonnet")
    kernel = AgentKernel(cfg)
    persona = Persona(name="executor", system_prompt="人设正文")
    options = kernel._build_options(persona, str(tmp_path))
    kw = captured["options_kwargs"]
    assert options is not None
    assert kw["cwd"] == str(tmp_path)
    assert "运行环境" in kw["system_prompt"]       # 环境块被拼进 system prompt
    assert "人设正文" in kw["system_prompt"]        # persona 正文被拼进
    assert kw["permission_mode"] == cfg.permission_mode
    assert kw["max_turns"] == 7
    assert kw["model"] == "sonnet"
    assert kw["max_thinking_tokens"] == 4096
    assert kw["max_budget_usd"] == 0.5


def test_build_options_persona_model_overrides_config(monkeypatch, project_root, tmp_path):
    captured = _install_fake_sdk(monkeypatch, [])
    cfg = _kernel_config(project_root, tmp_path, model="sonnet")
    kernel = AgentKernel(cfg)
    persona = Persona(name="sg", system_prompt="x", model="haiku",
                      tools=["Read", "Grep"])
    options = kernel._build_options(persona, str(tmp_path))
    assert captured["options_kwargs"]["model"] == "haiku"  # persona 优先
    assert options.allowed_tools == ["Read", "Grep"]        # 收窄工具集


def test_build_options_omits_disabled_cost_controls(monkeypatch, project_root, tmp_path):
    captured = _install_fake_sdk(monkeypatch, [])
    cfg = _kernel_config(project_root, tmp_path,
                         max_thinking_tokens=None, task_budget_usd=None)
    kernel = AgentKernel(cfg)
    kernel._build_options(Persona(name="x", system_prompt="p"), str(tmp_path))
    kw = captured["options_kwargs"]
    assert "max_thinking_tokens" not in kw
    assert "max_budget_usd" not in kw


def test_build_options_resume_and_subagents(monkeypatch, project_root, tmp_path):
    captured = _install_fake_sdk(monkeypatch, [])
    cfg = _kernel_config(project_root, tmp_path)
    kernel = AgentKernel(cfg)
    persona = Persona(name="brain", system_prompt="p",
                      sub_agents={"executor": {"description": "d"}})
    kernel._build_options(persona, str(tmp_path), resume_session_id="sess-9")
    kw = captured["options_kwargs"]
    assert kw["resume"] == "sess-9"
    assert kw["agents"] == {"executor": {"description": "d"}}


# ---- run_oneshot ----

def test_run_oneshot_success(monkeypatch, project_root, tmp_path):
    msgs = [
        AssistantMessage([TextBlock("部分输出")]),
        ResultMessage(result="最终结果", num_turns=3, is_error=False,
                      usage={"input_tokens": 10}, total_cost_usd=0.01,
                      duration_ms=1234, session_id="sess-1"),
    ]
    captured = _install_fake_sdk(monkeypatch, msgs)
    cfg = _kernel_config(project_root, tmp_path)
    kernel = AgentKernel(cfg)
    cwd = tmp_path / "work"
    cwd.mkdir()
    res = kernel.run_oneshot("做任务", Persona(name="executor", system_prompt="p"),
                             str(cwd))
    assert isinstance(res, KernelResult)
    assert res.output == "最终结果"
    assert res.turns == 3
    assert res.is_error is False
    assert res.usage == {"input_tokens": 10}
    assert res.cost_usd == 0.01
    assert res.duration_ms == 1234
    assert res.session_id == "sess-1"
    assert res.cancelled is False
    # 用户消息前缀含任务工作目录，且原始 prompt 被追加在 [任务] 段
    assert str(Path(cwd).resolve()) in captured["prompt"]
    assert "做任务" in captured["prompt"]


def test_run_oneshot_collects_artifacts(monkeypatch, project_root, tmp_path):
    _install_fake_sdk(monkeypatch, [ResultMessage(result="ok")])
    cfg = _kernel_config(project_root, tmp_path)
    kernel = AgentKernel(cfg)
    cwd = tmp_path / "work"
    cwd.mkdir()
    (cwd / "out.txt").write_text("产物", encoding="utf-8")
    sub = cwd / "nested"
    sub.mkdir()
    (sub / "deep.md").write_text("深层产物", encoding="utf-8")
    res = kernel.run_oneshot("p", Persona(name="x", system_prompt="s"), str(cwd))
    assert set(res.artifacts) >= {"out.txt", str(Path("nested") / "deep.md")}


def test_run_oneshot_fallback_to_assistant_text(monkeypatch, project_root, tmp_path):
    """ResultMessage.result 为空时，输出回退为拼接的 assistant 文本。"""
    msgs = [AssistantMessage([TextBlock("A"), TextBlock("B")]),
            ResultMessage(result="")]
    _install_fake_sdk(monkeypatch, msgs)
    cfg = _kernel_config(project_root, tmp_path)
    kernel = AgentKernel(cfg)
    res = kernel.run_oneshot("p", Persona(name="x", system_prompt="s"), str(tmp_path))
    assert res.output == "A\nB"


def test_run_oneshot_no_output_placeholder(monkeypatch, project_root, tmp_path):
    _install_fake_sdk(monkeypatch, [ResultMessage(result="")])
    cfg = _kernel_config(project_root, tmp_path)
    kernel = AgentKernel(cfg)
    res = kernel.run_oneshot("p", Persona(name="x", system_prompt="s"), str(tmp_path))
    assert res.output == "(无输出)"


def test_run_oneshot_error_no_artifacts(monkeypatch, project_root, tmp_path):
    _install_fake_sdk(monkeypatch,
                      [ResultMessage(result="炸了", is_error=True, num_turns=2)])
    cfg = _kernel_config(project_root, tmp_path)
    kernel = AgentKernel(cfg)
    cwd = tmp_path / "work"
    cwd.mkdir()
    (cwd / "x.txt").write_text("x", encoding="utf-8")
    res = kernel.run_oneshot("p", Persona(name="x", system_prompt="s"), str(cwd))
    assert res.is_error is True
    assert res.output == "炸了"
    assert res.artifacts == []  # 报错时不收集产物


def test_run_oneshot_timeout(monkeypatch, project_root, tmp_path):
    _install_fake_sdk(monkeypatch, [ResultMessage(result="late")], delay=3.0)
    cfg = _kernel_config(project_root, tmp_path, task_timeout=1)
    kernel = AgentKernel(cfg)
    res = kernel.run_oneshot("p", Persona(name="x", system_prompt="s"), str(tmp_path))
    assert res.is_error is True
    assert "超时" in res.output


def test_run_oneshot_cancelled(monkeypatch, project_root, tmp_path):
    _install_fake_sdk(monkeypatch, [ResultMessage(result="partial", num_turns=1)])
    cfg = _kernel_config(project_root, tmp_path)
    kernel = AgentKernel(cfg)
    ev = threading.Event()
    ev.set()  # 预先置位：第一条消息边界即应退出
    res = kernel.run_oneshot("p", Persona(name="x", system_prompt="s"),
                             str(tmp_path), cancel_event=ev)
    assert res.cancelled is True
    assert res.output == "partial"


def test_run_oneshot_sdk_exception_converged(monkeypatch, project_root, tmp_path):
    """query 抛异常时统一收敛为 is_error 结果，不外泄异常。"""
    _install_fake_sdk(monkeypatch, [], raise_on_query=True)
    cfg = _kernel_config(project_root, tmp_path)
    kernel = AgentKernel(cfg)
    res = kernel.run_oneshot("p", Persona(name="x", system_prompt="s"), str(tmp_path))
    assert res.is_error is True
    assert "boom" in res.output


# ---- _run_async ----

def test_run_async_without_loop():
    async def coro():
        await asyncio.sleep(0)
        return 42
    assert AgentKernel._run_async(coro()) == 42


def test_run_async_within_running_loop():
    """已在事件循环内时，借独立线程起新 loop 执行协程。"""
    async def inner():
        return "ok"

    async def outer():
        return AgentKernel._run_async(inner())

    assert asyncio.run(outer()) == "ok"


# ---- AgentSession ----

def test_open_session_returns_unconnected_session(project_root, tmp_path):
    cfg = _kernel_config(project_root, tmp_path)
    kernel = AgentKernel(cfg)
    session = kernel.open_session(Persona(name="executor", system_prompt="p"),
                                  str(tmp_path))
    assert isinstance(session, AgentSession)
    assert session.session_id is None
    assert session.total_cost == 0.0
    assert session.total_turns == 0


def test_agent_session_connect_send_close(monkeypatch, project_root, tmp_path):
    client_msgs = [
        AssistantMessage([TextBlock("回复")]),
        ResultMessage(result="done", num_turns=2, total_cost_usd=0.05,
                      session_id="sess-42"),
    ]
    captured = _install_fake_sdk(monkeypatch, [], client_messages=client_msgs)
    cfg = _kernel_config(project_root, tmp_path)
    kernel = AgentKernel(cfg)
    session = kernel.open_session(Persona(name="executor", system_prompt="p"),
                                  str(tmp_path))

    async def scenario():
        await session.connect()
        assert captured["client"].connected is True
        events = [ev async for ev in session.send("你好")]
        await session.close()
        return events

    events = asyncio.run(scenario())
    types_ = [e.type for e in events]
    assert "text_delta" in types_
    assert "result" in types_
    # 累计状态更新
    assert session.session_id == "sess-42"
    assert session.total_turns == 2
    assert session.total_cost == pytest.approx(0.05)
    # close 后底层客户端断开
    assert captured["client"].disconnected is True
    assert captured["client"].connected is False


def test_agent_session_send_without_connect_raises(project_root, tmp_path):
    cfg = _kernel_config(project_root, tmp_path)
    kernel = AgentKernel(cfg)
    session = kernel.open_session(Persona(name="x", system_prompt="s"), str(tmp_path))

    async def scenario():
        with pytest.raises(RuntimeError):
            async for _ in session.send("x"):
                pass

    asyncio.run(scenario())


def test_agent_session_interrupt(monkeypatch, project_root, tmp_path):
    captured = _install_fake_sdk(monkeypatch, [], client_messages=[])
    cfg = _kernel_config(project_root, tmp_path)
    kernel = AgentKernel(cfg)
    session = kernel.open_session(Persona(name="x", system_prompt="s"), str(tmp_path))

    async def scenario():
        await session.connect()
        await session.interrupt()

    asyncio.run(scenario())
    assert captured["client"].interrupted is True


# ---- stream ----

def test_stream_yields_event_sequence(monkeypatch, project_root, tmp_path):
    msgs = [
        AssistantMessage([TextBlock("思考中"), ToolUseBlock("Read", {"path": "x"})]),
        UserMessage([ToolResultBlock("文件内容")]),
        ResultMessage(result="完成", num_turns=1, session_id="s1"),
    ]
    _install_fake_sdk(monkeypatch, msgs)
    cfg = _kernel_config(project_root, tmp_path)
    kernel = AgentKernel(cfg)

    async def collect():
        return [ev async for ev in
                kernel.stream("任务", Persona(name="x", system_prompt="s"), str(tmp_path))]

    events = asyncio.run(collect())
    assert [e.type for e in events] == ["text_delta", "tool_use",
                                        "tool_result", "result"]
    assert events[0].data == "思考中"
    assert events[1].data["name"] == "Read"
    assert events[1].data["input"] == {"path": "x"}
    assert events[2].data == "文件内容"
    assert events[3].data["result"] == "完成"
    assert events[3].data["session_id"] == "s1"


def test_stream_error_event(monkeypatch, project_root, tmp_path):
    _install_fake_sdk(monkeypatch, [], raise_on_query=True)
    cfg = _kernel_config(project_root, tmp_path)
    kernel = AgentKernel(cfg)

    async def collect():
        return [ev async for ev in
                kernel.stream("任务", Persona(name="x", system_prompt="s"), str(tmp_path))]

    events = asyncio.run(collect())
    assert len(events) == 1
    assert events[0].type == "error"
    assert "boom" in events[0].data


# ---- _msg_to_events（事件翻译单元）----

def test_msg_to_events_assistant_text_and_tool():
    evs = _msg_to_events(AssistantMessage([TextBlock("hi"), ToolUseBlock("Bash", {})]))
    assert [e.type for e in evs] == ["text_delta", "tool_use"]
    assert evs[0].data == "hi"
    assert evs[1].data["name"] == "Bash"


def test_msg_to_events_result():
    evs = _msg_to_events(ResultMessage(result="r", num_turns=5, is_error=True,
                                       usage={"u": 1}, total_cost_usd=0.2,
                                       duration_ms=99, session_id="sid"))
    assert len(evs) == 1
    d = evs[0].data
    assert evs[0].type == "result"
    assert d["result"] == "r"
    assert d["num_turns"] == 5
    assert d["is_error"] is True
    assert d["usage"] == {"u": 1}
    assert d["cost_usd"] == 0.2
    assert d["duration_ms"] == 99
    assert d["session_id"] == "sid"


def test_msg_to_events_unknown_message_returns_empty():
    class Whatever:
        pass
    assert _msg_to_events(Whatever()) == []


def test_agent_event_dataclass_defaults():
    ev = AgentEvent("text_delta")
    assert ev.type == "text_delta"
    assert ev.data is None
