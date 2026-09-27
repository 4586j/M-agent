"""测试 kernel 三级兜底与 AgentSession 会话轻量检测（output guard）。

沿用 test_kernel.py 的假 SDK 模式：按 sdk.query() 调用次数返回不同消息序列，
捕获每次的 prompt 与 options.model 供断言。不触网、不依赖真实 SDK。
"""
import asyncio
import sys
import types
from pathlib import Path

import pytest

# runtime 目录加入 sys.path，供直接断言 ExperienceStore（kernel 同样延迟导入它）
_RUNTIME = (Path(__file__).resolve().parents[2] / "workspace" / "agent"
            / "orchestrator" / "runtime")
if str(_RUNTIME) not in sys.path:
    sys.path.insert(0, str(_RUNTIME))

from agent_core.config import load_agent_config
from agent_core.kernel import AgentKernel
from agent_core.persona import Persona
from experience_store import ExperienceStore

# 退化样本：复读机输出（触发 n-gram 重复率，不触发字符级重复）
LOOP = "请问有什么可以帮您？" * 20
OK = "任务已完成：已生成报告文件 summary.md，共统计 12 条记录，结论无异常。"


class TextBlock:
    def __init__(self, text):
        self.text = text


class AssistantMessage:
    def __init__(self, content):
        self.content = content


class ResultMessage:
    def __init__(self, result="", num_turns=1, is_error=False, usage=None,
                 total_cost_usd=None, duration_ms=100,
                 session_id="test-session"):
        self.result = result
        self.num_turns = num_turns
        self.is_error = is_error
        self.usage = usage
        self.total_cost_usd = total_cost_usd
        self.duration_ms = duration_ms
        self.session_id = session_id


def _install_sequenced_sdk(monkeypatch, sequences, hang_from_round=None):
    """假 SDK：第 n 次 sdk.query() 产出 sequences[n]（超出时重复最后一次）。

    捕获 calls["prompts"] / calls["models"]（每次调用的 prompt 与 options.model）。
    hang_from_round：从该轮起生成器永久挂起（模拟墙钟超时透传场景）。
    """
    calls = {"prompts": [], "models": [], "count": 0}

    class ClaudeAgentOptions:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    def query(prompt, options):
        idx = calls["count"]
        calls["count"] += 1
        calls["prompts"].append(prompt)
        calls["models"].append(getattr(options, "model", None))
        msgs = sequences[min(idx, len(sequences) - 1)]

        async def gen():
            for m in msgs:
                if hang_from_round is not None and idx >= hang_from_round:
                    await asyncio.sleep(3600)  # 永久挂起，交由 wait_for 超时
                yield m
        return gen()

    async def _empty_stream(prompt, options):
        return
        yield  # pragma: no cover - 空异步生成器

    fake = types.ModuleType("claude_agent_sdk")
    fake.ClaudeAgentOptions = ClaudeAgentOptions
    fake.query = query
    fake.ClaudeSDKClient = object
    monkeypatch.setitem(sys.modules, "claude_agent_sdk", fake)
    return calls


def _install_session_sdk(monkeypatch, client_messages):
    """假 SDK（会话路径）：ClaudeSDKClient 按真实 0.2.x 用法 query 协程 +
    receive_response 异步生成器，产出 client_messages。"""
    captured = {"prompts": []}

    class ClaudeAgentOptions:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    class ClaudeSDKClient:
        def __init__(self, options=None):
            self.options = options
            self.connected = False

        async def connect(self):
            self.connected = True

        async def disconnect(self):
            self.connected = False

        async def query(self, prompt=None, session_id="default"):
            captured["prompts"].append(prompt)

        async def receive_response(self):
            for m in client_messages:
                yield m
                if type(m).__name__ == "ResultMessage":
                    return

    fake = types.ModuleType("claude_agent_sdk")
    fake.ClaudeAgentOptions = ClaudeAgentOptions
    fake.query = lambda prompt, options: _empty_gen()
    fake.ClaudeSDKClient = ClaudeSDKClient
    monkeypatch.setitem(sys.modules, "claude_agent_sdk", fake)
    return captured


async def _empty_gen():
    return
    yield  # pragma: no cover - 空异步生成器


def _kernel_config(project_root, tmp_path, **overrides):
    """隔离配置：记忆/经验目录指向 tmp_path，避免污染真实数据。"""
    return load_agent_config(
        project_root=project_root,
        memory_backend="local",
        memory_core_dir=str(tmp_path / "mem" / "core"),
        memory_context_dir=str(tmp_path / "mem" / "context"),
        experience_store_dir=str(tmp_path / "exp"),
        mcp_config_path="",
        **overrides,
    )


def _run(kernel, prompt=OK, cwd=None):
    return kernel.run_oneshot(prompt, Persona(name="executor", system_prompt="p"),
                              str(cwd))


# ---- run_oneshot 三级兜底 ----

def test_guard_retry_recovers(monkeypatch, project_root, tmp_path):
    """首次退化 → 重试成功：正常返回、meta 记 recovered 与尝试次数、指标累加。"""
    calls = _install_sequenced_sdk(
        monkeypatch, [[ResultMessage(result=LOOP, num_turns=1,
                                     total_cost_usd=0.01)],
                      [ResultMessage(result=OK, num_turns=2,
                                     total_cost_usd=0.02)]])
    cfg = _kernel_config(project_root, tmp_path)
    kernel = AgentKernel(cfg)
    res = _run(kernel, cwd=tmp_path)
    assert calls["count"] == 2
    assert res.is_error is False
    assert res.output == OK
    assert res.meta["degenerate_recovered"] is True
    assert res.meta["degenerate"]["detected"] is True
    assert res.meta["degenerate"]["recovered"] is True
    assert res.meta["degenerate"]["attempts"] == 1
    assert "n-gram 重复率" in res.meta["degenerate"]["reasons"][0]
    # cost/turns 跨尝试累加（_RoundAccumulator 模式）
    assert res.turns == 3
    assert res.cost_usd == pytest.approx(0.03)
    # 重试 prompt 注入了纠偏提示（用户消息前）
    assert "上一次输出异常" in calls["prompts"][1]


def test_guard_second_retry_switches_to_fallback_model(
        monkeypatch, project_root, tmp_path):
    """连续退化 + 配置 fallback_model → 第 2 次重试换模型并成功。"""
    calls = _install_sequenced_sdk(
        monkeypatch, [[ResultMessage(result=LOOP)],
                      [ResultMessage(result=LOOP)],
                      [ResultMessage(result=OK, num_turns=2)]])
    cfg = _kernel_config(project_root, tmp_path, fallback_model="big-model",
                         output_guard_max_retries=2)
    kernel = AgentKernel(cfg)
    res = _run(kernel, cwd=tmp_path)
    assert calls["count"] == 3
    assert calls["models"] == [None, None, "big-model"]  # 第 3 次换模型
    assert res.is_error is False
    assert res.output == OK
    assert res.meta["degenerate_recovered"] is True
    assert res.meta["degenerate"]["attempts"] == 2
    assert res.meta["degenerate"]["models_tried"] == ["", "big-model"]


def test_guard_no_fallback_still_retries_with_hint(
        monkeypatch, project_root, tmp_path):
    """未配置 fallback_model：第 2 次重试仍注入提示（不换模型）。"""
    calls = _install_sequenced_sdk(
        monkeypatch, [[ResultMessage(result=LOOP)],
                      [ResultMessage(result=LOOP)],
                      [ResultMessage(result=OK)]])
    cfg = _kernel_config(project_root, tmp_path, output_guard_max_retries=2)
    kernel = AgentKernel(cfg)
    res = _run(kernel, cwd=tmp_path)
    assert calls["count"] == 3
    assert calls["models"] == [None, None, None]
    assert all("上一次输出异常" in p for p in calls["prompts"][1:])
    assert res.meta["degenerate_recovered"] is True


def test_guard_exhausted_fuses(monkeypatch, project_root, tmp_path):
    """重试耗尽仍退化：熔断 is_error + error_type + meta 详情 + 经验失败口径。"""
    calls = _install_sequenced_sdk(
        monkeypatch, [[ResultMessage(result=LOOP)],
                      [ResultMessage(result=LOOP)],
                      [ResultMessage(result=LOOP)]])
    cfg = _kernel_config(project_root, tmp_path, fallback_model="big-model",
                         output_guard_max_retries=2)
    kernel = AgentKernel(cfg)
    res = _run(kernel, cwd=tmp_path)
    assert calls["count"] == 3
    assert res.is_error is True
    assert res.error_type == "degenerate_output"
    assert res.output == LOOP  # 保留最后一次原始输出
    meta = res.meta["degenerate"]
    assert meta["detected"] is True and meta["recovered"] is False
    assert meta["attempts"] == 2
    assert len(meta["reasons"]) == 3
    assert meta["models_tried"] == ["", "big-model"]
    assert res.artifacts == []  # 失败不收集产物
    # 熔断结果不按成功口径沉淀经验（is_error=True 天然走失败口径）
    recs = ExperienceStore(cfg.experience_store_dir).list_all()
    assert len(recs) == 1
    assert recs[0].success is False


def test_guard_disabled_no_detection(monkeypatch, project_root, tmp_path):
    """guard 关闭：不检测、不重试，行为与旧版完全一致。"""
    calls = _install_sequenced_sdk(
        monkeypatch, [[ResultMessage(result=LOOP)], [ResultMessage(result=OK)]])
    cfg = _kernel_config(project_root, tmp_path, output_guard_enabled=False)
    kernel = AgentKernel(cfg)
    res = _run(kernel, cwd=tmp_path)
    assert calls["count"] == 1
    assert res.output == LOOP
    assert res.is_error is False
    assert "degenerate" not in res.meta
    assert "degenerate_recovered" not in res.meta


def test_guard_normal_output_single_call(monkeypatch, project_root, tmp_path):
    """正常输出：一次调用即返回，meta 无 degenerate 字段。"""
    calls = _install_sequenced_sdk(
        monkeypatch, [[ResultMessage(result=OK, num_turns=1)]])
    kernel = AgentKernel(_kernel_config(project_root, tmp_path))
    res = _run(kernel, cwd=tmp_path)
    assert calls["count"] == 1
    assert res.is_error is False
    assert "degenerate" not in res.meta
    assert "degenerate_recovered" not in res.meta


def test_guard_tool_only_round_skipped(monkeypatch, project_root, tmp_path):
    """tool 轮无文本（占位输出 "(无输出)"）：跳过检测，不触发重试。"""
    calls = _install_sequenced_sdk(
        monkeypatch, [[ResultMessage(result="")], [ResultMessage(result=OK)]])
    kernel = AgentKernel(_kernel_config(project_root, tmp_path))
    res = _run(kernel, cwd=tmp_path)
    assert calls["count"] == 1
    assert res.output == "(无输出)"
    assert "degenerate" not in res.meta


def test_guard_warn_recorded_without_retry(monkeypatch, project_root, tmp_path):
    """warn 级（长 prompt 短输出非短答案）：只透出 degenerate_warn，不重试。"""
    calls = _install_sequenced_sdk(
        monkeypatch, [[ResultMessage(result="未找到结果")]])
    kernel = AgentKernel(_kernel_config(project_root, tmp_path))
    long_prompt = "请详细分析这份季度销售报告的趋势并给出下季度预测建议" * 5
    res = _run(kernel, prompt=long_prompt, cwd=tmp_path)
    assert calls["count"] == 1
    assert res.is_error is False
    assert "degenerate_warn" in res.meta
    assert "输出过短" in res.meta["degenerate_warn"]


# ---- 守卫行为补强（评审修复 2/4/5/6）----

def test_guard_hint_appended_at_end(monkeypatch, project_root, tmp_path):
    """纠偏提示追加到消息末尾：稳定前缀不动，保住 prompt caching。"""
    calls = _install_sequenced_sdk(
        monkeypatch, [[ResultMessage(result=LOOP)],
                      [ResultMessage(result=OK)]])
    kernel = AgentKernel(_kernel_config(project_root, tmp_path))
    task = "请统计销售数据并生成报告文件"
    _run(kernel, prompt=task, cwd=tmp_path)
    retry_prompt = calls["prompts"][1]
    # hint 必须出现在原始任务文本之后（追加尾部），而非插到稳定前缀之前
    assert retry_prompt.find("上一次输出异常") > retry_prompt.find(task)


def test_guard_budget_guard_stops_retry(monkeypatch, project_root, tmp_path):
    """重试前预算闸门：累计花费达 task_budget_usd 的 80% 则不再重试。"""
    calls = _install_sequenced_sdk(
        monkeypatch, [[ResultMessage(result=LOOP, total_cost_usd=0.02)],
                      [ResultMessage(result=OK)]])
    cfg = _kernel_config(project_root, tmp_path, task_budget_usd=0.02)
    kernel = AgentKernel(cfg)
    res = _run(kernel, cwd=tmp_path)
    assert calls["count"] == 1  # 闸门生效：未发起第二次调用
    assert res.is_error is True
    assert res.error_type == "degenerate_output"
    meta = res.meta["degenerate"]
    assert meta["budget_guard_stop"] is True
    assert meta["recovered"] is False
    assert meta["spent_usd"] == pytest.approx(0.02)
    assert meta["budget_usd"] == 0.02


def test_guard_first_detection_error_passes_through(
        monkeypatch, project_root, tmp_path):
    """首轮检测器抛异常：记 warning 后放行第一轮结果，不误吞成功输出。"""
    calls = _install_sequenced_sdk(
        monkeypatch, [[ResultMessage(result=OK)]])
    import agent_core.kernel as kernel_module

    def boom(text, prompt, **kwargs):
        raise RuntimeError("detector exploded")

    monkeypatch.setattr(kernel_module, "detect_degenerate", boom)
    kernel = AgentKernel(_kernel_config(project_root, tmp_path))
    res = _run(kernel, cwd=tmp_path)
    assert calls["count"] == 1
    assert res.is_error is False
    assert res.output == OK
    assert "degenerate" not in res.meta


def test_guard_retry_detection_error_returns_recovered(
        monkeypatch, project_root, tmp_path):
    """重试轮检测器抛异常：按 recovered 口径返回（不拦截已成功的重试）。"""
    calls = _install_sequenced_sdk(
        monkeypatch, [[ResultMessage(result=LOOP)],
                      [ResultMessage(result=OK)]])
    import agent_core.kernel as kernel_module
    real_detect = kernel_module.detect_degenerate
    state = {"n": 0}

    def flaky(text, prompt, **kwargs):
        state["n"] += 1
        if state["n"] == 1:
            return real_detect(text, prompt, **kwargs)  # 首轮：真检测出退化
        raise RuntimeError("detector exploded on retry round")

    monkeypatch.setattr(kernel_module, "detect_degenerate", flaky)
    kernel = AgentKernel(_kernel_config(project_root, tmp_path))
    res = _run(kernel, cwd=tmp_path)
    assert calls["count"] == 2
    assert res.is_error is False
    assert res.output == OK
    meta = res.meta["degenerate"]
    assert meta["recovered"] is True
    assert meta["note"] == "detection_error_pass"


def test_oneshot_timeout_passthrough_progress(monkeypatch, project_root,
                                              tmp_path):
    """墙钟超时：透传已累计 turns/cost 与最近一次退化 verdict（note=timeout）。"""
    _install_sequenced_sdk(
        monkeypatch,
        [[ResultMessage(result=LOOP, num_turns=1, total_cost_usd=0.01)],
         [ResultMessage(result=OK, num_turns=2, total_cost_usd=0.02)]],
        hang_from_round=1)  # 第二轮（重试轮）挂起直到超时
    cfg = _kernel_config(project_root, tmp_path, task_timeout=0.3)
    kernel = AgentKernel(cfg)
    res = _run(kernel, cwd=tmp_path)
    assert res.is_error is True
    assert res.error_type == "timeout"
    assert res.turns == 1  # 第一轮已累计的 turns
    assert res.cost_usd == pytest.approx(0.01)
    meta = res.meta["degenerate"]
    assert meta["note"] == "timeout"
    assert meta["recovered"] is False
    assert "n-gram 重复率" in meta["reasons"][0]


# ---- AgentSession.send 会话轻量检测 ----

def test_session_degenerate_round_marked_and_failed_experience(
        monkeypatch, project_root, tmp_path):
    """会话退化轮：result 事件附 degenerate 标记，本轮经验按失败口径沉淀。"""
    client_msgs = [AssistantMessage([TextBlock(LOOP)]),
                   ResultMessage(result=LOOP, num_turns=1,
                                 total_cost_usd=0.05, session_id="sess-dg")]
    _install_session_sdk(monkeypatch, client_msgs)
    cfg = _kernel_config(project_root, tmp_path)
    kernel = AgentKernel(cfg)
    session = kernel.open_session(Persona(name="executor", system_prompt="p"),
                                  str(tmp_path))

    async def scenario():
        await session.connect()
        events = [ev async for ev in session.send("一个足够长的任务描述" * 5)]
        await session.close()
        return events

    events = asyncio.run(scenario())
    result_ev = next(e for e in events if e.type == "result")
    assert result_ev.data["degenerate"]["detected"] is True
    assert "n-gram 重复率" in result_ev.data["degenerate"]["reason"]
    # 累计指标不受影响
    assert session.total_cost == pytest.approx(0.05)
    # 退化轮不按成功沉淀经验（失败口径 success=False）
    recs = ExperienceStore(cfg.experience_store_dir).list_all()
    assert len(recs) == 1
    assert recs[0].success is False


def test_session_normal_round_no_marker_and_success_experience(
        monkeypatch, project_root, tmp_path):
    """会话正常轮：result 事件无 degenerate 标记，经验照常按成功口径沉淀。"""
    client_msgs = [AssistantMessage([TextBlock(OK)]),
                   ResultMessage(result=OK, num_turns=2,
                                 total_cost_usd=0.01, session_id="sess-ok")]
    _install_session_sdk(monkeypatch, client_msgs)
    cfg = _kernel_config(project_root, tmp_path)
    kernel = AgentKernel(cfg)
    session = kernel.open_session(Persona(name="executor", system_prompt="p"),
                                  str(tmp_path))

    async def scenario():
        await session.connect()
        events = [ev async for ev in session.send("正常任务")]
        await session.close()
        return events

    events = asyncio.run(scenario())
    result_ev = next(e for e in events if e.type == "result")
    assert "degenerate" not in result_ev.data
    recs = ExperienceStore(cfg.experience_store_dir).list_all()
    assert len(recs) == 1
    assert recs[0].success is True


def test_session_guard_disabled_no_marker(monkeypatch, project_root, tmp_path):
    """guard 关闭：会话轮不检测、无标记。"""
    client_msgs = [AssistantMessage([TextBlock(LOOP)]),
                   ResultMessage(result=LOOP, num_turns=1, session_id="s")]
    _install_session_sdk(monkeypatch, client_msgs)
    cfg = _kernel_config(project_root, tmp_path, output_guard_enabled=False)
    kernel = AgentKernel(cfg)
    session = kernel.open_session(Persona(name="executor", system_prompt="p"),
                                  str(tmp_path))

    async def scenario():
        await session.connect()
        events = [ev async for ev in session.send("任意任务")]
        await session.close()
        return events

    events = asyncio.run(scenario())
    result_ev = next(e for e in events if e.type == "result")
    assert "degenerate" not in result_ev.data
