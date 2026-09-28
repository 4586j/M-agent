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

import agent_core.kernel as kernel_module
from agent_core.config import load_agent_config
from agent_core.kernel import (AgentEvent, AgentKernel, AgentSession,
                               KernelResult, _msg_to_events)
from agent_core.persona import Persona


# ---- 假 SDK 消息类型（类名必须与 kernel 判断一致）----

class TextBlock:
    def __init__(self, text):
        self.text = text


class ThinkingBlock:
    def __init__(self, thinking):
        self.thinking = thinking


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


class StreamEvent:
    """SDK include_partial_messages 开启时的增量流事件（event 为原始流 dict）。"""

    def __init__(self, event):
        self.event = event


def _stream_text(text):
    """构造一条文本增量 StreamEvent（content_block_delta/text_delta）。"""
    return StreamEvent({"type": "content_block_delta",
                        "delta": {"type": "text_delta", "text": text}})


def _install_fake_sdk(monkeypatch, query_messages, delay=0.0,
                      client_messages=None, raise_on_query=False,
                      client_delay=0.0):
    """注入假 claude_agent_sdk 模块，返回捕获容器。

    - query(prompt, options)：异步生成器，逐条产出 query_messages；
      delay>0 时每条前 sleep（用于超时测试）；raise_on_query 时首抛异常。
    - ClaudeAgentOptions(**kwargs)：捕获 kwargs 供断言，允许后续属性赋值。
    - ClaudeSDKClient(options)：connect/disconnect/interrupt mock；按真实 SDK 0.2.x，
      query() 为「协程」（只发送、返回 None），receive_response() 为「异步生成器」
      （产出 client_messages，遇 ResultMessage 结束本轮；client_delay>0 时
      每条前 sleep，用于会话墙钟超时测试）。
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

        # 真实 SDK 0.2.x：query() 是协程，只把消息写入 transport、返回 None（非生成器）
        async def query(self, prompt=None, session_id="default"):
            captured["client_prompt"] = prompt

        # 收流用 receive_response()：异步生成器，逐条产出，遇 ResultMessage 结束本轮
        async def receive_response(self):
            msgs = client_messages if client_messages is not None else query_messages
            for m in msgs:
                if client_delay:
                    await asyncio.sleep(client_delay)
                yield m
                if type(m).__name__ == "ResultMessage":
                    return

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


def test_build_options_subagents_gate(monkeypatch, project_root, tmp_path):
    """全局闸（Slice: 子代理开关）：config.subagents_enabled=False 时即使人设带
    sub_agents 也不映射为 options.agents；True（默认）时照常映射。"""
    persona = Persona(name="brain", system_prompt="p",
                      sub_agents={"executor": {"description": "d"}})
    # 默认（True）：映射 agents
    captured = _install_fake_sdk(monkeypatch, [])
    AgentKernel(_kernel_config(project_root, tmp_path))._build_options(
        persona, str(tmp_path))
    assert captured["options_kwargs"]["agents"] == {"executor": {"description": "d"}}
    # 关闭：不映射 agents
    captured = _install_fake_sdk(monkeypatch, [])
    AgentKernel(_kernel_config(project_root, tmp_path,
                               subagents_enabled=False))._build_options(
        persona, str(tmp_path))
    assert "agents" not in captured["options_kwargs"]


def test_build_options_defaults_to_fresh_session(monkeypatch, project_root, tmp_path):
    """会话隔离（Slice 4）：未显式续接时不传 resume，每次单发起全新会话。

    run_oneshot 走 _build_options(persona, cwd)（不带 resume_session_id），
    因此不会把上一运行的 SDK 会话历史带进来——sessions do not leak memory。
    """
    captured = _install_fake_sdk(monkeypatch, [])
    cfg = _kernel_config(project_root, tmp_path)
    kernel = AgentKernel(cfg)
    kernel._build_options(Persona(name="executor", system_prompt="p"), str(tmp_path))
    assert "resume" not in captured["options_kwargs"]


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


# ---- AGENT_STREAMING → include_partial_messages 映射 ----

def test_build_options_streaming_maps_include_partial(monkeypatch, project_root, tmp_path):
    """config.streaming=True（AGENT_STREAMING 默认）映射为 SDK 增量流选项。"""
    captured = _install_fake_sdk(monkeypatch, [])
    cfg = _kernel_config(project_root, tmp_path, streaming=True)
    kernel = AgentKernel(cfg)
    kernel._build_options(Persona(name="x", system_prompt="p"), str(tmp_path))
    assert captured["options_kwargs"]["include_partial_messages"] is True


def test_build_options_streaming_disabled_omits_option(monkeypatch, project_root, tmp_path):
    captured = _install_fake_sdk(monkeypatch, [])
    cfg = _kernel_config(project_root, tmp_path, streaming=False)
    kernel = AgentKernel(cfg)
    kernel._build_options(Persona(name="x", system_prompt="p"), str(tmp_path))
    assert "include_partial_messages" not in captured["options_kwargs"]


# ---- StreamEvent 翻译与 _StreamFilter 去重 ----

def test_msg_to_events_stream_event_text_delta():
    evs = _msg_to_events(_stream_text("增量片段"))
    assert [e.type for e in evs] == ["text_delta"]
    assert evs[0].data == "增量片段"


def test_msg_to_events_stream_event_ignores_non_text():
    # 工具入参增量与其他类型不产出事件；thinking 增量改为产出（面板需要）
    thinking = StreamEvent({"type": "content_block_delta",
                            "delta": {"type": "thinking_delta", "thinking": "x"}})
    tool_json = StreamEvent({"type": "content_block_delta",
                             "delta": {"type": "input_json_delta", "partial_json": "{}"}})
    other = StreamEvent({"type": "message_start"})
    assert [(e.type, e.data) for e in _msg_to_events(thinking)] == [
        ("thinking_delta", "x")]
    for ev in (tool_json, other):
        assert _msg_to_events(ev) == []


def test_msg_to_events_thinking_block():
    # 完整 AssistantMessage 的 ThinkingBlock 翻译为 thinking 事件
    msg = AssistantMessage([ThinkingBlock("推演中"), TextBlock("答案")])
    evs = _msg_to_events(msg)
    assert [(e.type, e.data) for e in evs] == [("thinking", "推演中"), ("text_delta", "答案")]


def test_stream_filter_dedup_thinking_partial_and_full():
    """thinking 增量与完整块去重：增量流出后完整消息的 ThinkingBlock 跳过。"""
    flt = kernel_module._StreamFilter(partial_text=True)
    flt.translate(StreamEvent({"type": "content_block_delta",
                               "delta": {"type": "thinking_delta", "thinking": "思"}}))
    full = AssistantMessage([ThinkingBlock("思考全文"), TextBlock("答")])
    evs = flt.translate(full)
    # 增量已流出 → 完整 ThinkingBlock 跳过；TextBlock 无增量前缀 → 保留
    assert [e.type for e in evs] == ["text_delta"]
    assert evs[0].data == "答"


def test_stream_filter_dedup_partial_and_full_message():
    """增量 + 完整 AssistantMessage 双份文本只留一份；ToolUseBlock 保留。"""
    flt = kernel_module._StreamFilter(partial_text=True)
    evs = flt.translate(_stream_text("增量"))
    assert [e.type for e in evs] == ["text_delta"]
    full = AssistantMessage([TextBlock("增量"), ToolUseBlock("Read", {"a": 1})])
    evs2 = flt.translate(full)
    # 完整消息的 TextBlock 被跳过（去重），ToolUseBlock 保留
    assert [e.type for e in evs2] == ["tool_use"]
    assert evs2[0].data["name"] == "Read"


def test_stream_filter_passthrough_without_partial():
    """未见增量时，完整 AssistantMessage 照常翻译（旧行为）。"""
    flt = kernel_module._StreamFilter(partial_text=True)
    evs = flt.translate(AssistantMessage([TextBlock("全文")]))
    assert [e.type for e in evs] == ["text_delta"]
    assert evs[0].data == "全文"


def test_stream_filter_disabled_when_streaming_off():
    """config.streaming=False 时不启用去重，完整消息照常产出文本事件。"""
    flt = kernel_module._StreamFilter(partial_text=False)
    evs = flt.translate(_stream_text("增量"))
    assert [e.type for e in evs] == ["text_delta"]
    evs2 = flt.translate(AssistantMessage([TextBlock("全文")]))
    assert [e.type for e in evs2] == ["text_delta"]


def test_stream_filter_resets_after_result_message():
    """中断/异常轮次只出现增量、未到完整 AssistantMessage 时，result 到达
    后去重标志必须复位，下一轮完整消息文本不被残留标志误吞。"""
    flt = kernel_module._StreamFilter(partial_text=True)
    # 第一轮：增量出现（标志置位），随后直接 result（中断场景，无完整消息）
    evs = flt.translate(_stream_text("片段"))
    assert [e.type for e in evs] == ["text_delta"]
    flt.translate(ResultMessage(result="中断结束"))
    # 第二轮：完整 AssistantMessage 应照常翻译，不再被残留标志吞掉
    evs2 = flt.translate(AssistantMessage([TextBlock("下一轮全文")]))
    assert [e.type for e in evs2] == ["text_delta"]
    assert evs2[0].data == "下一轮全文"


def test_stream_filter_reset_allows_manual_rearm():
    """reset() 可手动复位：复位后完整消息恢复透传（异常路径兜底语义）。"""
    flt = kernel_module._StreamFilter(partial_text=True)
    flt.translate(_stream_text("片段"))
    flt.translate(AssistantMessage([TextBlock("被去重")]))   # 标志复位（消费掉）
    flt.translate(_stream_text("又一轮增量"))                # 标志再次置位
    flt.reset()                                              # 流结束/异常路径统一复位
    evs = flt.translate(AssistantMessage([TextBlock("恢复透传")]))
    assert [e.type for e in evs] == ["text_delta"]
    assert evs[0].data == "恢复透传"


def test_stream_includes_partial_events(monkeypatch, project_root, tmp_path):
    """stream() 在假 SDK 产出 StreamEvent 时翻译出增量文本。"""
    msgs = [_stream_text("片段A"), _stream_text("片段B"),
            ResultMessage(result="完成")]
    _install_fake_sdk(monkeypatch, msgs)
    cfg = _kernel_config(project_root, tmp_path)  # streaming 默认 True
    kernel = AgentKernel(cfg)

    async def collect():
        return [ev async for ev in
                kernel.stream("任务", Persona(name="x", system_prompt="s"), str(tmp_path))]

    events = asyncio.run(collect())
    deltas = [e for e in events if e.type == "text_delta"]
    assert [e.data for e in deltas] == ["片段A", "片段B"]
    assert events[-1].type == "result"


def test_stream_timeout_yields_error_and_result(monkeypatch, project_root, tmp_path):
    """流式墙钟截止（P0）：超时转 error + 终结 result（is_error, timeout），
    不再无限等待卡住的后端。"""
    msgs = [AssistantMessage([TextBlock("慢")]), ResultMessage(result="late")]
    _install_fake_sdk(monkeypatch, msgs, delay=0.5)
    cfg = _kernel_config(project_root, tmp_path, task_timeout=0.2)
    kernel = AgentKernel(cfg)

    async def collect():
        return [ev async for ev in
                kernel.stream("任务", Persona(name="x", system_prompt="s"), str(tmp_path))]

    events = asyncio.run(collect())
    types = [e.type for e in events]
    assert "error" in types
    assert types[-1] == "result"
    assert events[-1].data["is_error"] is True
    assert events[-1].data["error_type"] == "timeout"


def test_stream_missing_result_marks_error(monkeypatch, project_root, tmp_path):
    """流结束未见终结 ResultMessage：补发 error + result（no_result），不当成功。"""
    msgs = [AssistantMessage([TextBlock("半截")])]  # 无 ResultMessage
    _install_fake_sdk(monkeypatch, msgs)
    cfg = _kernel_config(project_root, tmp_path)
    kernel = AgentKernel(cfg)

    async def collect():
        return [ev async for ev in
                kernel.stream("任务", Persona(name="x", system_prompt="s"), str(tmp_path))]

    events = asyncio.run(collect())
    assert events[-1].type == "result"
    assert events[-1].data["is_error"] is True
    assert events[-1].data["error_type"] == "no_result"


# ---- AgentSession 墙钟超时 ----

def test_agent_session_send_timeout_error_event(monkeypatch, project_root, tmp_path):
    """send(timeout=...) 超时转为 error 事件而非裸抛，本轮终止。"""
    client_msgs = [AssistantMessage([TextBlock("慢回复")]),
                    ResultMessage(result="late")]
    _install_fake_sdk(monkeypatch, [], client_messages=client_msgs, client_delay=0.5)
    cfg = _kernel_config(project_root, tmp_path)
    kernel = AgentKernel(cfg)
    session = kernel.open_session(Persona(name="x", system_prompt="s"), str(tmp_path))

    async def scenario():
        await session.connect()
        return [ev async for ev in session.send("你好", timeout=0.2)]

    events = asyncio.run(scenario())
    assert len(events) == 1
    assert events[0].type == "error"
    assert "超时" in events[0].data


def test_agent_session_send_no_timeout_by_default(monkeypatch, project_root, tmp_path):
    """默认不传 timeout：慢消息也照常收到（保持原行为）。"""
    client_msgs = [ResultMessage(result="done", total_cost_usd=0.01)]
    _install_fake_sdk(monkeypatch, [], client_messages=client_msgs, client_delay=0.15)
    cfg = _kernel_config(project_root, tmp_path)
    kernel = AgentKernel(cfg)
    session = kernel.open_session(Persona(name="x", system_prompt="s"), str(tmp_path))

    async def scenario():
        await session.connect()
        return [ev async for ev in session.send("你好")]

    events = asyncio.run(scenario())
    assert events[-1].type == "result"


def test_agent_session_send_timeout_zero_rejected(monkeypatch, project_root, tmp_path):
    """timeout=0/负数语义上不是"不限"：显式抛 ValueError；None 才是不限。

    校验在任何消息发送前完成，抛出后 session 仍可正常使用。
    """
    _install_fake_sdk(monkeypatch, [],
                      client_messages=[ResultMessage(result="ok")])
    cfg = _kernel_config(project_root, tmp_path)
    kernel = AgentKernel(cfg)
    session = kernel.open_session(Persona(name="x", system_prompt="s"), str(tmp_path))

    async def scenario():
        await session.connect()
        rejected = []
        for bad in (0, -1, 0.0):
            try:
                [ev async for ev in session.send("你好", timeout=bad)]
                rejected.append(False)
            except ValueError:
                rejected.append(True)
        # 非法 timeout 全部被拒后，session 仍可正常发送
        events = [ev async for ev in session.send("你好")]
        return rejected, events

    rejected, events = asyncio.run(scenario())
    assert rejected == [True, True, True]
    assert events[-1].type == "result"


def test_agent_session_send_timeout_interrupts_and_drains(
        monkeypatch, project_root, tmp_path):
    """超时后先叫停底层执行再排空残留：interrupt 置位，下一轮不被污染。

    排空消费掉本轮残留的 AssistantMessage/ResultMessage（不产出事件），
    随后新一轮 send 正常收尾（残留消息不再混入下一轮）。
    """
    client_msgs = [AssistantMessage([TextBlock("慢回复")]),
                    ResultMessage(result="late", session_id="sess-late")]
    captured = _install_fake_sdk(monkeypatch, [], client_messages=client_msgs,
                                 client_delay=0.4)
    cfg = _kernel_config(project_root, tmp_path)
    kernel = AgentKernel(cfg)
    session = kernel.open_session(Persona(name="x", system_prompt="s"), str(tmp_path))

    async def scenario():
        await session.connect()
        first = [ev async for ev in session.send("第一轮", timeout=0.15)]
        second = [ev async for ev in session.send("第二轮")]
        return first, second

    first, second = asyncio.run(scenario())
    assert len(first) == 1 and first[0].type == "error"
    # 排空前的 interrupt 已下发到底层客户端
    assert captured["client"].interrupted is True
    # 排空不产出事件；第二轮正常收尾，session 状态可继续推进
    assert second[-1].type == "result"
    assert second[-1].data["session_id"] == "sess-late"


def test_agent_session_timeout_drain_bounded(monkeypatch, project_root, tmp_path):
    """排空有单条限时保护：底层卡死时排空在限时内放弃，send 正常返回。"""
    import agent_core.kernel as kernel_module

    class HangClient:
        """第一条消息后永久挂起的底层客户端（模拟子进程卡死）。"""

        def __init__(self):
            self.interrupted = False

        async def connect(self):
            pass

        async def query(self, prompt=None, session_id="default"):
            pass

        async def interrupt(self):
            self.interrupted = True

        def receive_response(self):
            async def gen():
                yield AssistantMessage([TextBlock("一条")])
                await asyncio.sleep(3600)  # 挂起：排空限时保护必须放开
            return gen()

    _install_fake_sdk(monkeypatch, [])  # 仅为了 SDK 模块可导入
    monkeypatch.setattr(kernel_module, "_DRAIN_WAIT_SECONDS", 0.1)
    cfg = _kernel_config(project_root, tmp_path)
    kernel = AgentKernel(cfg)
    session = kernel.open_session(Persona(name="x", system_prompt="s"), str(tmp_path))
    hang = HangClient()

    async def scenario():
        await session.connect()
        session._client = hang  # 换成卡死客户端
        return [ev async for ev in session.send("你好", timeout=0.2)]

    events = asyncio.run(scenario())
    # 首条消息正常产出；随后挂起触发超时 error，排空限时放弃不卡死
    assert [e.type for e in events] == ["text_delta", "error"]
    assert hang.interrupted is True


# ---- 预算软预警 ----

def test_agent_session_budget_warnings_80_and_95(monkeypatch, project_root, tmp_path):
    """累计 cost 达预算 80%/95% 各预警一次，不改硬熔断。"""
    client_msgs = [ResultMessage(result="r", num_turns=1, total_cost_usd=0.09,
                                 session_id="sess-b1")]
    _install_fake_sdk(monkeypatch, [], client_messages=client_msgs)
    cfg = _kernel_config(project_root, tmp_path, task_budget_usd=0.1)
    kernel = AgentKernel(cfg)
    session = kernel.open_session(Persona(name="x", system_prompt="s"), str(tmp_path))

    async def scenario():
        await session.connect()
        all_types = []
        all_events = []
        for _ in range(2):
            events = [ev async for ev in session.send("你好")]
            all_events.extend(events)
            all_types.extend(e.type for e in events)
        return all_events, all_types

    all_events, all_types = asyncio.run(scenario())
    # 每轮 result 后紧跟预算预警：第一轮 0.09/0.1=90% 触发 80% 档；第二轮 0.18 触发 95% 档
    assert all_types == ["result", "budget_warning", "result", "budget_warning"]
    warn1 = all_events[1].data
    warn2 = all_events[3].data
    assert warn1["threshold"] == 0.8
    assert warn1["spent_usd"] == pytest.approx(0.09)
    assert warn1["budget_usd"] == 0.1
    assert warn2["threshold"] == 0.95
    assert warn2["spent_usd"] == pytest.approx(0.18)


def test_agent_session_no_budget_warning_without_budget(monkeypatch, project_root, tmp_path):
    client_msgs = [ResultMessage(result="r", total_cost_usd=100.0)]
    _install_fake_sdk(monkeypatch, [], client_messages=client_msgs)
    cfg = _kernel_config(project_root, tmp_path, task_budget_usd=None)
    kernel = AgentKernel(cfg)
    session = kernel.open_session(Persona(name="x", system_prompt="s"), str(tmp_path))

    async def scenario():
        await session.connect()
        return [e.type async for e in session.send("你好")]

    assert "budget_warning" not in asyncio.run(scenario())


# ---- RAG 下沉（内核统一 prompt 拼装） ----

def _fake_retrieve(prompt, config):
    return [("历史任务：写快排", "产出：O(n log n) 实现")]

def test_run_oneshot_rag_enrich(monkeypatch, project_root, tmp_path):
    captured = _install_fake_sdk(monkeypatch, [ResultMessage(result="ok")])
    monkeypatch.setattr(kernel_module, "_retrieve_experiences", _fake_retrieve)
    cfg = _kernel_config(project_root, tmp_path, rag_enabled=True)
    kernel = AgentKernel(cfg)
    res = kernel.run_oneshot("做任务", Persona(name="x", system_prompt="s"), str(tmp_path))
    assert res.output == "ok"
    assert "[历史经验参考]" in captured["prompt"]
    assert "历史任务：写快排" in captured["prompt"]
    assert "做任务" in captured["prompt"]


def test_run_oneshot_rag_disabled_prompt_unchanged(monkeypatch, project_root, tmp_path):
    captured = _install_fake_sdk(monkeypatch, [ResultMessage(result="ok")])
    monkeypatch.setattr(kernel_module, "_retrieve_experiences", _fake_retrieve)
    cfg = _kernel_config(project_root, tmp_path, rag_enabled=False)
    kernel = AgentKernel(cfg)
    kernel.run_oneshot("做任务", Persona(name="x", system_prompt="s"), str(tmp_path))
    assert "[历史经验参考]" not in captured["prompt"]


def test_agent_session_rag_enrich(monkeypatch, project_root, tmp_path):
    client_msgs = [ResultMessage(result="r", session_id="s9")]
    captured = _install_fake_sdk(monkeypatch, [], client_messages=client_msgs)
    monkeypatch.setattr(kernel_module, "_retrieve_experiences", _fake_retrieve)
    cfg = _kernel_config(project_root, tmp_path, rag_enabled=True)
    kernel = AgentKernel(cfg)
    session = kernel.open_session(Persona(name="x", system_prompt="s"), str(tmp_path))

    async def scenario():
        await session.connect()
        return [ev async for ev in session.send("你好")]

    asyncio.run(scenario())
    assert "[历史经验参考]" in captured["client_prompt"]


def test_agent_session_rag_disabled_unchanged(monkeypatch, project_root, tmp_path):
    client_msgs = [ResultMessage(result="r", session_id="s9")]
    captured = _install_fake_sdk(monkeypatch, [], client_messages=client_msgs)
    monkeypatch.setattr(kernel_module, "_retrieve_experiences", _fake_retrieve)
    cfg = _kernel_config(project_root, tmp_path, rag_enabled=False)
    kernel = AgentKernel(cfg)
    session = kernel.open_session(Persona(name="x", system_prompt="s"), str(tmp_path))

    async def scenario():
        await session.connect()
        return [ev async for ev in session.send("你好")]

    asyncio.run(scenario())
    assert "[历史经验参考]" not in captured["client_prompt"]


# ---- 会话经验自动沉淀（下沉到内核） ----

def _capture_record_experience(monkeypatch):
    calls = []

    def fake(self, prompt, persona, result, cwd):
        calls.append({"prompt": prompt, "persona": persona,
                      "result": result, "cwd": cwd})

    monkeypatch.setattr(AgentKernel, "_record_experience", fake)
    return calls


def test_agent_session_records_experience(monkeypatch, project_root, tmp_path):
    client_msgs = [ResultMessage(result="产出", num_turns=2, total_cost_usd=0.02,
                                 session_id="sess-x1")]
    _install_fake_sdk(monkeypatch, [], client_messages=client_msgs)
    cfg = _kernel_config(project_root, tmp_path)  # auto_record_experience 默认 True
    kernel = AgentKernel(cfg)
    calls = _capture_record_experience(monkeypatch)
    session = kernel.open_session(Persona(name="executor", system_prompt="s"),
                                  str(tmp_path))

    async def scenario():
        await session.connect()
        return [ev async for ev in session.send("原始提问")]

    asyncio.run(scenario())
    assert len(calls) == 1
    assert calls[0]["prompt"] == "原始提问"   # 用原始 prompt，非 RAG 拼装后的
    assert calls[0]["result"].output == "产出"
    assert calls[0]["result"].cost_usd == 0.02


def test_agent_session_experience_disabled(monkeypatch, project_root, tmp_path):
    client_msgs = [ResultMessage(result="r", session_id="s")]
    _install_fake_sdk(monkeypatch, [], client_messages=client_msgs)
    cfg = _kernel_config(project_root, tmp_path, auto_record_experience=False)
    kernel = AgentKernel(cfg)
    calls = _capture_record_experience(monkeypatch)
    session = kernel.open_session(Persona(name="x", system_prompt="s"), str(tmp_path))

    async def scenario():
        await session.connect()
        return [ev async for ev in session.send("你好")]

    asyncio.run(scenario())
    assert calls == []


def test_agent_session_no_experience_on_timeout(monkeypatch, project_root, tmp_path):
    """本轮超时（无 result 事件）不沉淀经验。"""
    client_msgs = [AssistantMessage([TextBlock("慢")]), ResultMessage(result="late")]
    _install_fake_sdk(monkeypatch, [], client_messages=client_msgs, client_delay=0.5)
    cfg = _kernel_config(project_root, tmp_path)
    kernel = AgentKernel(cfg)
    calls = _capture_record_experience(monkeypatch)
    session = kernel.open_session(Persona(name="x", system_prompt="s"), str(tmp_path))

    async def scenario():
        await session.connect()
        return [ev async for ev in session.send("你好", timeout=0.2)]

    asyncio.run(scenario())
    assert calls == []


def test_run_oneshot_record_experience_false_skips_write(monkeypatch, project_root,
                                                         tmp_path):
    """评测消融硬关：record_experience=False 时即使 auto_record_experience=True
    也不沉淀经验（真正禁用写，而非仅靠提示词）。"""
    _install_fake_sdk(monkeypatch, [ResultMessage(result="产出", session_id="s")])
    cfg = _kernel_config(project_root, tmp_path)  # auto_record_experience 默认 True
    kernel = AgentKernel(cfg)
    calls = _capture_record_experience(monkeypatch)
    res = kernel.run_oneshot("p", Persona(name="x", system_prompt="s"),
                             str(tmp_path), record_experience=False)
    assert res.is_error is False
    assert calls == []   # 未沉淀


def test_run_oneshot_record_experience_default_writes(monkeypatch, project_root,
                                                      tmp_path):
    """默认（record_experience=True）+ auto_record_experience=True → 照常沉淀。"""
    _install_fake_sdk(monkeypatch, [ResultMessage(result="产出", session_id="s")])
    cfg = _kernel_config(project_root, tmp_path)
    kernel = AgentKernel(cfg)
    calls = _capture_record_experience(monkeypatch)
    res = kernel.run_oneshot("p", Persona(name="x", system_prompt="s"), str(tmp_path))
    assert res.is_error is False
    assert len(calls) == 1


# ---- P0 真相口径：缺失终结 result / 自纠错闭环 ----

def test_run_oneshot_missing_result_marks_error(monkeypatch, project_root, tmp_path):
    """SDK 流未产出终结 ResultMessage（子进程崩溃/被杀）：绝不谎报成功。"""
    msgs = [AssistantMessage([TextBlock("半截输出")])]  # 无 ResultMessage
    _install_fake_sdk(monkeypatch, msgs)
    cfg = _kernel_config(project_root, tmp_path)
    kernel = AgentKernel(cfg)
    res = kernel.run_oneshot("p", Persona(name="x", system_prompt="s"), str(tmp_path))
    assert res.is_error is True
    assert res.error_type == "no_result"
    assert res.meta.get("no_result") is True
    assert "半截输出" in res.output  # 保留已收文本供排障


def test_run_oneshot_correction_passes_verification(monkeypatch, project_root, tmp_path):
    """正常成功轮：自纠错验证通过，meta 记 verification.status=passed。"""
    _install_fake_sdk(monkeypatch, [ResultMessage(result="一段普通文本结论")])
    cfg = _kernel_config(project_root, tmp_path)
    kernel = AgentKernel(cfg)
    res = kernel.run_oneshot("p", Persona(name="x", system_prompt="s"), str(tmp_path))
    assert res.is_error is False
    assert res.meta["correction"]["validated"] is True
    assert res.meta["verification"]["status"] == "passed"


def test_run_oneshot_correction_high_severity_fails(monkeypatch, project_root, tmp_path):
    """重试耗尽仍为高严重度结构性错误（Python 语法错）：视为必需验证失败，
    且跨修正轮累计 turns/cost（不谎报低成本）。"""
    broken = "```python\ndef foo(\n```"
    _install_fake_sdk(monkeypatch, [ResultMessage(result=broken, num_turns=1,
                                                  total_cost_usd=0.01)])
    cfg = _kernel_config(project_root, tmp_path, correction_max_retries=1)
    kernel = AgentKernel(cfg)
    res = kernel.run_oneshot("写个函数", Persona(name="x", system_prompt="s"),
                             str(tmp_path))
    assert res.is_error is True
    assert res.error_type == "validation_failed"
    assert res.meta["verification"]["status"] == "failed"
    assert res.meta["verification"]["severity"] == "high"
    # 初始轮 + 1 次修正轮 = 2 turns / 0.02 美元
    assert res.turns == 2
    assert res.cost_usd == pytest.approx(0.02)


def test_run_correction_loop_cancelled_returns_cancelled(monkeypatch, project_root,
                                                         tmp_path):
    """自纠错阶段收到取消：返回 cancelled，绝不返回之前的「成功」结果。"""
    from agent_core.trace import ExecutionTracer
    _install_fake_sdk(monkeypatch, [ResultMessage(result="x")])
    cfg = _kernel_config(project_root, tmp_path)
    kernel = AgentKernel(cfg)
    base = KernelResult(output="初值", turns=1, is_error=False, session_id="s1")
    ev = threading.Event()
    ev.set()
    res = kernel._run_correction_loop(
        base, "p", Persona(name="x", system_prompt="s"), str(tmp_path), "", ev,
        ExecutionTracer("run_oneshot"))
    assert res.cancelled is True
    assert res.is_error is False
    assert res.meta["correction"]["cancelled"] is True


def test_run_correction_loop_expired_deadline_short_circuits(monkeypatch, project_root,
                                                             tmp_path):
    """整任务截止点已到期：卡住的纠错不再开启新修正轮，标 deadline_exceeded。"""
    from agent_core.trace import ExecutionTracer
    from agent_core.kernel import _Deadline
    _install_fake_sdk(monkeypatch, [ResultMessage(result="x")])
    cfg = _kernel_config(project_root, tmp_path, correction_max_retries=3)
    kernel = AgentKernel(cfg)
    broken = "```python\ndef foo(\n```"
    base = KernelResult(output=broken, turns=1, is_error=False, session_id="s1")
    dl = _Deadline(0.0)  # 剩余 <= 0，已到期
    res = kernel._run_correction_loop(
        base, "写个函数", Persona(name="x", system_prompt="s"), str(tmp_path), "",
        None, ExecutionTracer("run_oneshot"), dl)
    assert res.meta["correction"]["deadline_exceeded"] is True
    assert res.meta["verification"]["deadline_exceeded"] is True
    # 高严重度结构性错误未能修正 -> 必需验证失败
    assert res.is_error is True
    assert res.error_type == "validation_failed"
    assert res.turns == 1  # 未开启任何修正轮（用量仍为初始轮）


def test_run_correction_loop_retry_bounded_by_deadline(monkeypatch, project_root,
                                                       tmp_path):
    """修正轮受剩余预算约束：重试卡住则 wait_for 超时，标 deadline_exceeded。"""
    from agent_core.trace import ExecutionTracer
    from agent_core.kernel import _Deadline
    broken = "```python\ndef foo(\n```"
    # 每条消息 delay=1.0，修正轮会卡住；截止点仅给 0.2s
    _install_fake_sdk(monkeypatch, [ResultMessage(result=broken)], delay=1.0)
    cfg = _kernel_config(project_root, tmp_path, correction_max_retries=2)
    kernel = AgentKernel(cfg)
    base = KernelResult(output=broken, turns=1, is_error=False, session_id="s1")
    dl = _Deadline(0.2)
    res = kernel._run_correction_loop(
        base, "写个函数", Persona(name="x", system_prompt="s"), str(tmp_path), "",
        None, ExecutionTracer("run_oneshot"), dl)
    assert res.meta["correction"]["deadline_exceeded"] is True
    assert res.is_error is True  # 仍未修正的高严重度错误
    assert res.turns == 1        # 修正轮超时未完成，不计入用量


# ---- 结果翻译钩子（run_oneshot 结果边界）----

class _FakeTranslator:
    """假翻译服务：捕获 translate 入参，返回预设 TranslationResult 或抛异常。"""

    def __init__(self, result=None, raises=None):
        self._result = result
        self._raises = raises
        self.calls: list[dict] = []

    def translate(self, text, *, target_lang=None, skip_if_target=True):
        self.calls.append({"text": text, "target_lang": target_lang})
        if self._raises is not None:
            raise self._raises
        return self._result


def test_run_oneshot_translates_result_when_enabled(monkeypatch, project_root, tmp_path):
    """开关开启：结果边界的英文产出被译为中文，原文保留进 meta。"""
    from agent_core.translator import TranslationResult
    _install_fake_sdk(monkeypatch, [ResultMessage(result="Done: 42 rows")])
    cfg = _kernel_config(project_root, tmp_path, result_translate_enabled=True,
                         translate_target_lang="zh")
    kernel = AgentKernel(cfg)
    fake = _FakeTranslator(result=TranslationResult(
        translated="完成：42 行", target_lang="zh", changed=True))
    kernel._translator = fake
    res = kernel.run_oneshot("p", Persona(name="x", system_prompt="s"), str(tmp_path))
    assert res.output == "完成：42 行"
    assert res.meta["original_output"] == "Done: 42 rows"
    assert res.meta["translated"]["changed"] is True
    assert fake.calls[0]["target_lang"] == "zh"


def test_run_oneshot_no_translate_when_disabled(monkeypatch, project_root, tmp_path):
    """默认（关）：结果原样返回，不构造/不调用翻译。"""
    from agent_core.translator import TranslationResult
    _install_fake_sdk(monkeypatch, [ResultMessage(result="Done")])
    cfg = _kernel_config(project_root, tmp_path)  # result_translate_enabled 默认 False
    kernel = AgentKernel(cfg)
    fake = _FakeTranslator(result=TranslationResult(translated="不应出现", changed=True))
    kernel._translator = fake
    res = kernel.run_oneshot("p", Persona(name="x", system_prompt="s"), str(tmp_path))
    assert res.output == "Done"
    assert "translated" not in res.meta
    assert fake.calls == []


def test_run_oneshot_skips_translate_on_error(monkeypatch, project_root, tmp_path):
    """错误结果不翻译（已人性化中文，且避免掩盖错误）。"""
    from agent_core.translator import TranslationResult
    _install_fake_sdk(monkeypatch, [ResultMessage(result="炸了", is_error=True)])
    cfg = _kernel_config(project_root, tmp_path, result_translate_enabled=True)
    kernel = AgentKernel(cfg)
    fake = _FakeTranslator(result=TranslationResult(translated="x", changed=True))
    kernel._translator = fake
    res = kernel.run_oneshot("p", Persona(name="x", system_prompt="s"), str(tmp_path))
    assert res.is_error is True
    assert res.output == "炸了"
    assert fake.calls == []


def test_run_oneshot_translate_failure_preserves_original(monkeypatch, project_root,
                                                          tmp_path):
    """翻译异常绝不影响主结果：保留原文，meta 记录降级。"""
    _install_fake_sdk(monkeypatch, [ResultMessage(result="Done")])
    cfg = _kernel_config(project_root, tmp_path, result_translate_enabled=True)
    kernel = AgentKernel(cfg)
    kernel._translator = _FakeTranslator(raises=RuntimeError("translate boom"))
    res = kernel.run_oneshot("p", Persona(name="x", system_prompt="s"), str(tmp_path))
    assert res.output == "Done"
    assert res.is_error is False
    assert res.meta["translated"]["error"] == "translate_exception"


def test_run_oneshot_translate_skipped_records_meta(monkeypatch, project_root, tmp_path):
    """已是目标语言而跳过：结果不变，meta 记录 skipped，不存原文副本。"""
    from agent_core.translator import TranslationResult
    _install_fake_sdk(monkeypatch, [ResultMessage(result="已是中文结果")])
    cfg = _kernel_config(project_root, tmp_path, result_translate_enabled=True)
    kernel = AgentKernel(cfg)
    kernel._translator = _FakeTranslator(result=TranslationResult(
        translated="已是中文结果", target_lang="zh", changed=False,
        skipped="already_target_lang"))
    res = kernel.run_oneshot("p", Persona(name="x", system_prompt="s"), str(tmp_path))
    assert res.output == "已是中文结果"
    assert res.meta["translated"]["skipped"] == "already_target_lang"
    assert "original_output" not in res.meta


def test_get_translator_lazy_constructs_and_caches(project_root, tmp_path):
    """_get_translator 惰性构造 TranslatorService 并缓存复用。"""
    from agent_core.translator import TranslatorService
    cfg = _kernel_config(project_root, tmp_path)
    kernel = AgentKernel(cfg)
    assert kernel._translator is None
    tr = kernel._get_translator()
    assert isinstance(tr, TranslatorService)
    assert kernel._get_translator() is tr  # 同一实例（缓存）
