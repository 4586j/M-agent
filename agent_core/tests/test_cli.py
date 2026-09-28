"""测试 agent_core.cli：多槽会话持久化、slash 命令、/retry、/usage、/memory。

会话连接一律用假 session / monkeypatch _open_session，不触真实 SDK。
"""
import asyncio
import json
from pathlib import Path

import pytest

import agent_core.cli as cli
from agent_core.cli import (
    CliState,
    _interactive_write_memory,
    _render_session_records,
    build_arg_parser,
    handle_command,
    load_last_session_id,
    load_session_records,
    save_session_record,
)
from agent_core.config import load_agent_config
from agent_core.tools import valid_memory_name
from agent_core.kernel import AgentEvent, AgentKernel
from agent_core.persona import Persona


# ---- 假 session / 假 UI ----

class FakeCliSession:
    """CLI 层假会话：send 产出预置事件，可关可中断。"""

    def __init__(self, sid="sess-1", events=None):
        self._sid = sid
        self._events = events or []
        self.total_turns = 0
        self.total_cost = 0.0
        self.closed = False
        self.last_prompt = None
        self.last_timeout = "__unset__"

    @property
    def session_id(self):
        return self._sid

    async def send(self, prompt, timeout=None):
        self.last_prompt = prompt
        self.last_timeout = timeout
        for ev in self._events:
            yield ev

    async def close(self):
        self.closed = True

    async def interrupt(self):
        pass


class RecordingUI:
    """记录型假 UI：收集各 hook 调用供断言。"""

    rich = False

    def __init__(self):
        self.infos = []
        self.memories = []
        self.fatals = []
        self.budget = []
        self.stats = []

    # 流式 hook
    def turn_start(self): pass

    def text(self, chunk): pass

    def tool_use(self, name, inp): pass

    def tool_result(self, content): pass

    def error(self, msg): pass

    def fatal(self, msg):
        self.fatals.append(msg)

    def interrupted(self): pass

    def budget_warning(self, data):
        self.budget.append(data)

    def turn_end(self): pass

    # 信息 hook
    def info(self, msg):
        self.infos.append(msg)

    def banner(self, state): pass

    def help(self): pass

    def tools(self, state): pass

    def plan(self, plan): pass

    def cost(self, turns, total): pass

    def memory(self, text):
        self.memories.append(text)

    def turn_stats(self, info, session):
        self.stats.append(info)


# ---- 夹具 ----

@pytest.fixture
def cfg(tmp_path):
    # .env 隔离由 conftest 的 autouse 夹具统一负责（AGENT_ENV_FILE 哨兵路径），
    # 避免真实 .env 键值写入 os.environ 污染后续 dotenv 断言。
    return load_agent_config(
        project_root=str(tmp_path),
        memory_backend="local",
        memory_core_dir=str(tmp_path / "mem" / "core"),
        memory_context_dir=str(tmp_path / "mem" / "context"),
        experience_store_dir=str(tmp_path / "exp"),
        mcp_config_path="",
    )


@pytest.fixture
def state(cfg):
    kernel = AgentKernel(cfg)
    st = CliState(kernel, Persona(name="executor", system_prompt="p"), cfg,
                  cwd=str(cfg.project_root), planning=False)
    st.session = FakeCliSession(sid=None)
    return st


def _patch_open_session(monkeypatch):
    """替换 cli._open_session：不触真实 SDK，记录 resume_id。"""
    opened = []

    async def fake_open(state, resume_id=None, ui=None):
        state.session = FakeCliSession(sid=resume_id or "fresh")
        state.session_meta = state._new_session_meta()
        opened.append(resume_id)

    monkeypatch.setattr(cli, "_open_session", fake_open)
    return opened


def _result_event(sid="sid-1", cost=0.01):
    return AgentEvent("result", {
        "result": "答", "num_turns": 2, "is_error": False,
        "usage": {"input_tokens": 10, "output_tokens": 4},
        "cost_usd": cost, "duration_ms": 50, "session_id": sid,
    })


# ---- 多槽会话持久化 ----

def test_save_and_load_session_records(tmp_path):
    d = tmp_path / ".agent_sessions"
    rec1 = {"session_id": "aaa", "persona": "executor", "created_at": "t1",
            "updated_at": "2026-01-01 10:00:00", "first_message": "第一问",
            "total_turns": 3, "total_cost": 0.05}
    rec2 = {"session_id": "bbb", "persona": "orchestrator", "created_at": "t2",
            "updated_at": "2026-01-02 09:00:00", "first_message": "第二问",
            "total_turns": 1, "total_cost": 0.01}
    p1 = save_session_record(rec1, d)
    p2 = save_session_record(rec2, d)
    assert p1.name == "aaa.json" and p2.name == "bbb.json"
    records = load_session_records(sessions_dir=d)
    assert [r["session_id"] for r in records] == ["bbb", "aaa"]  # 最近在前


def test_load_session_records_skips_broken_files(tmp_path):
    d = tmp_path / ".agent_sessions"
    d.mkdir()
    (d / "good.json").write_text(json.dumps(
        {"session_id": "good", "updated_at": "x"}), encoding="utf-8")
    (d / "bad.json").write_text("{not json", encoding="utf-8")
    records = load_session_records(sessions_dir=d)
    assert [r["session_id"] for r in records] == ["good"]


def test_legacy_agent_session_compat(tmp_path):
    """目录无记录时兼容读取旧单文件 .agent_session。"""
    (tmp_path / ".agent_session").write_text(
        json.dumps({"session_id": "legacy-9"}), encoding="utf-8")
    records = load_session_records(project_root=tmp_path)
    assert len(records) == 1
    assert records[0]["session_id"] == "legacy-9"
    assert records[0].get("legacy") is True


def test_load_last_session_id_prefers_dir_over_legacy(tmp_path):
    d = tmp_path / ".agent_sessions"
    save_session_record({"session_id": "new-1", "updated_at": "now"}, d)
    (tmp_path / ".agent_session").write_text(
        json.dumps({"session_id": "legacy-1"}), encoding="utf-8")
    assert load_last_session_id(project_root=tmp_path) == "new-1"


def test_render_session_records(tmp_path):
    assert "暂无" in _render_session_records([])
    text = _render_session_records([{
        "session_id": "abc123def", "persona": "executor",
        "updated_at": "2026-01-01", "first_message": "你好",
        "total_cost": 0.12}])
    assert "abc123def" in text
    assert "executor" in text
    assert "你好" in text
    assert "$0.1200" in text


def test_cli_state_saves_current_session_record(state, tmp_path):
    state.session = FakeCliSession(sid="curr-1")
    state.total_turns_setter = None
    state.session.total_turns = 4
    state.session.total_cost = 0.22
    state.session_meta["first_message"] = "首条消息"
    path = state.save_current_session_record()
    assert path is not None and path.exists()
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["session_id"] == "curr-1"
    assert data["total_turns"] == 4
    assert data["total_cost"] == pytest.approx(0.22)
    assert data["first_message"] == "首条消息"


def test_cli_state_skip_when_no_session_id(state):
    state.session = FakeCliSession(sid=None)
    assert state.save_current_session_record() is None


def test_record_turn_extracts_tokens(state):
    state.record_turn({"num_turns": 2, "cost_usd": 0.03,
                       "usage": {"input_tokens": 7, "output_tokens": 3}})
    rec = state.turn_records[-1]
    assert rec["turns"] == 2
    assert rec["cost"] == 0.03
    assert rec["input_tokens"] == 7
    assert rec["output_tokens"] == 3


def test_note_first_message_idempotent(state):
    state.note_first_message("第一条 很长".replace(" 很长", " " * 200))
    state.note_first_message("第二条")
    assert "第一条" in state.session_meta["first_message"]
    assert "第二条" not in state.session_meta["first_message"]


# ---- 命令：/sessions /session /switch ----

def test_cmd_sessions_lists_records(state, tmp_path):
    d = tmp_path / ".agent_sessions"
    save_session_record({"session_id": "sess-x", "persona": "executor",
                         "updated_at": "t", "first_message": "hi",
                         "total_cost": 0.1}, d)
    ui = RecordingUI()
    asyncio.run(handle_command(state, "/sessions", ui))
    assert any("sess-x" in m for m in ui.infos)


def test_cmd_session_shows_current_metadata(state):
    state.session = FakeCliSession(sid="sid-cur")
    state.session_meta["first_message"] = "元数据消息"
    ui = RecordingUI()
    asyncio.run(handle_command(state, "/session", ui))
    text = "\n".join(ui.infos)
    assert "sid-cur" in text
    assert "元数据消息" in text
    assert ".agent_sessions/sid-cur.json" in text


def test_cmd_switch_closes_current_and_resumes(state, monkeypatch, tmp_path):
    opened = _patch_open_session(monkeypatch)
    d = tmp_path / ".agent_sessions"
    save_session_record({"session_id": "target-1", "persona": "executor",
                         "updated_at": "2026-01-02", "first_message": "旧会话",
                         "total_cost": 0.3, "created_at": "c"}, d)
    current = FakeCliSession(sid="current-1")
    state.session = current
    ui = RecordingUI()
    asyncio.run(handle_command(state, "/switch 1", ui))
    assert current.closed is True            # 关闭当前
    assert opened == ["target-1"]            # resume 选中
    assert state.session.session_id == "target-1"
    assert state.session_meta["first_message"] == "旧会话"  # 元数据继承
    assert any("target-1" in m for m in ui.infos)


def test_cmd_switch_out_of_range(state, monkeypatch, tmp_path):
    _patch_open_session(monkeypatch)
    ui = RecordingUI()
    asyncio.run(handle_command(state, "/switch 5", ui))
    assert any("超出范围" in m for m in ui.infos)


def test_cmd_switch_requires_number(state, monkeypatch):
    _patch_open_session(monkeypatch)
    ui = RecordingUI()
    asyncio.run(handle_command(state, "/switch", ui))
    assert any("用法" in m for m in ui.infos)


# ---- /retry ----

def test_retry_resends_last_prompt(state, monkeypatch):
    _patch_open_session(monkeypatch)
    fake = FakeCliSession(sid="sid-r", events=[_result_event()])
    state.session = fake
    state.last_prompt = "第一问"
    ui = RecordingUI()
    asyncio.run(handle_command(state, "/retry", ui))
    assert fake.last_prompt == "第一问"
    assert state.turn_records and state.turn_records[0]["turns"] == 2
    assert ui.stats  # turn_stats 被调用


def test_retry_empty_prompts_hints(state):
    ui = RecordingUI()
    asyncio.run(handle_command(state, "/retry", ui))
    assert any("没有可重发" in m for m in ui.infos)


def test_fatal_hint_retry_cached_prompt(state, monkeypatch):
    """发送链路缓存 last_prompt；轮级异常时 fatal + /retry 提示。"""
    class BoomSession(FakeCliSession):
        async def send(self, prompt, timeout=None):
            raise RuntimeError("连接中断")
            yield  # pragma: no cover - 使其成为生成器

    state.session = BoomSession()
    ui = RecordingUI()

    async def scenario():
        await cli._send_user_message(state, "坏消息", ui)

    asyncio.run(scenario())
    assert state.last_prompt == "坏消息"
    assert ui.fatals
    assert any("/retry" in m for m in ui.infos)


# ---- /usage ----

def test_cmd_usage_appends_jsonl_once(state, tmp_path):
    state.session = FakeCliSession(sid="usage-1")
    state.record_turn({"num_turns": 1, "cost_usd": 0.01,
                       "usage": {"input_tokens": 3, "output_tokens": 2}})
    state.record_turn({"num_turns": 2, "cost_usd": 0.02, "usage": {}})
    ui = RecordingUI()
    asyncio.run(handle_command(state, "/usage", ui))
    p = tmp_path / ".agent_usage" / "usage-1.jsonl"
    assert p.exists()
    lines = p.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2
    first = json.loads(lines[0])
    assert first["input_tokens"] == 3
    # 再次执行不重复追加
    asyncio.run(handle_command(state, "/usage", ui))
    assert len(p.read_text(encoding="utf-8").strip().splitlines()) == 2


def test_cmd_usage_without_session_id(state):
    state.session = FakeCliSession(sid=None)
    ui = RecordingUI()
    asyncio.run(handle_command(state, "/usage", ui))
    assert any("session_id" in m for m in ui.infos)


# ---- /memory 增强 ----

def test_memory_show_by_name(state, tmp_path):
    core = tmp_path / "mem" / "core"
    core.mkdir(parents=True)
    (core / "coding_style.md").write_text("永远写测试", encoding="utf-8")
    ui = RecordingUI()
    asyncio.run(handle_command(state, "/memory coding_style", ui))
    assert ui.memories and ui.memories[0] == "永远写测试"


def test_memory_show_missing_lists_available(state, tmp_path):
    core = tmp_path / "mem" / "core"
    core.mkdir(parents=True)
    (core / "known.md").write_text("x", encoding="utf-8")
    ui = RecordingUI()
    asyncio.run(handle_command(state, "/memory nothere", ui))
    assert any("nothere" in m and "known" in m for m in ui.infos)


def test_memory_set_invalid_name_rejected(state):
    ui = RecordingUI()
    asyncio.run(handle_command(state, r"/memory set a\\b", ui))
    assert any("记忆名" in m for m in ui.infos)


def test_memory_set_reserved_device_name_rejected(state):
    """Windows 保留设备名（NUL/CON 等）作为记忆名一律拒绝，
    否则写入会被设备吞。"""
    for cmd in ("/memory set NUL", "/memory set con", "/memory set Com1"):
        ui = RecordingUI()
        asyncio.run(handle_command(state, cmd, ui))
        assert any("记忆名" in m for m in ui.infos), cmd


def test_interactive_write_memory(tmp_path):
    cfgx = load_agent_config(project_root=str(tmp_path),
                             memory_core_dir=str(tmp_path / "core"))
    answers = iter(["第一行", "第二行", ""])
    path = asyncio.run(_interactive_write_memory(cfgx, "my_note",
                                                 input_fn=lambda: next(answers)))
    p = Path(path)
    assert p.exists()
    assert p == tmp_path / "core" / "my_note.md"
    assert "第一行\n第二行" in p.read_text(encoding="utf-8")


def test_interactive_write_memory_cancelled_when_empty(tmp_path):
    cfgx = load_agent_config(project_root=str(tmp_path),
                             memory_core_dir=str(tmp_path / "core"))
    answers = iter(["", "忽略"])
    assert asyncio.run(_interactive_write_memory(
        cfgx, "empty", input_fn=lambda: next(answers))) is None


def test_valid_memory_name():
    assert valid_memory_name("abc")
    assert valid_memory_name("中文记忆")
    assert valid_memory_name("a_b-c")
    assert not valid_memory_name("")
    assert not valid_memory_name("a/b")
    assert not valid_memory_name("a\\b")
    assert not valid_memory_name("..")
    assert not valid_memory_name("a b")
    # Windows 保留设备名（大小写不敏感）
    for bad in ("NUL", "con", "AUX", "prn", "com1", "LPT9"):
        assert not valid_memory_name(bad), bad
    # 含保留名子串的正常名字不受影响
    assert valid_memory_name("nul_backup")
    assert valid_memory_name("console")


# ---- 参数解析 ----

def test_planning_default_reads_agent_planning_enabled(monkeypatch):
    monkeypatch.setenv("AGENT_PLANNING_ENABLED", "1")
    assert build_arg_parser().parse_args([]).planning is True
    monkeypatch.delenv("AGENT_PLANNING_ENABLED", raising=False)
    assert build_arg_parser().parse_args([]).planning is False


def test_doctor_flag_exists():
    args = build_arg_parser().parse_args(["--doctor"])
    assert args.doctor is True


# ---- P0 真相口径：headless 退出码 ----

class _OneshotUI(RecordingUI):
    """补上 oneshot_stats hook（oneshot_async 专用于纯文本统计）。"""

    def __init__(self):
        super().__init__()
        self.oneshot = []

    def oneshot_stats(self, info, session):
        self.oneshot.append(info)


def _patch_open_with_events(monkeypatch, events, sid="fresh"):
    async def fake_open(state, resume_id=None, ui=None):
        state.session = FakeCliSession(sid=sid, events=events)
        state.session_meta = state._new_session_meta()
    monkeypatch.setattr(cli, "_open_session", fake_open)


def test_oneshot_async_exit_zero_on_clean_result(monkeypatch, state):
    """正常收到终结 result 且无 error 事件：退出 0。"""
    _patch_open_with_events(monkeypatch, [_result_event()])
    code = asyncio.run(cli.oneshot_async(state, "问", resume=False, ui=_OneshotUI()))
    assert code == 0


def test_oneshot_async_exit_one_when_no_result_event(monkeypatch, state):
    """整轮无终结 result 事件（流异常提前结束）：headless 必须退出 1。"""
    _patch_open_with_events(monkeypatch, [AgentEvent("text_delta", "半截")])
    code = asyncio.run(cli.oneshot_async(state, "问", resume=False, ui=_OneshotUI()))
    assert code == 1


def test_oneshot_async_exit_one_on_error_event(monkeypatch, state):
    """流内出现 error 事件：headless 必须退出 1（即便随后有 result）。"""
    events = [AgentEvent("error", "炸了"), _result_event()]
    _patch_open_with_events(monkeypatch, events)
    code = asyncio.run(cli.oneshot_async(state, "问", resume=False, ui=_OneshotUI()))
    assert code == 1


def test_stream_turn_forwards_task_timeout(state):
    """P0：CLI 单轮发送带墙钟截止（config.task_timeout），不再无限等待。"""
    sess = FakeCliSession(sid="s", events=[_result_event()])
    state.session = sess
    state.config.task_timeout = 123
    asyncio.run(cli.stream_turn(state, "问", RecordingUI()))
    assert sess.last_timeout == 123


def test_stream_turn_no_timeout_when_nonpositive(state):
    """task_timeout 非正数视为不限（None），不触发 send 的 ValueError。"""
    sess = FakeCliSession(sid="s", events=[_result_event()])
    state.session = sess
    state.config.task_timeout = 0
    asyncio.run(cli.stream_turn(state, "问", RecordingUI()))
    assert sess.last_timeout is None
