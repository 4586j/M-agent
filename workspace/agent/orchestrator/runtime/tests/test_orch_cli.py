import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import orch_cli

# 项目根 = tests → runtime → orchestrator → workspace/agent → 项目根（上五级）
PROJECT_ROOT = Path(__file__).resolve().parents[5]


def test_strip_frontmatter():
    text = "---\nname: orchestrator\n---\n\n正文内容"
    assert orch_cli.strip_frontmatter(text) == "正文内容"
    assert orch_cli.strip_frontmatter("无 frontmatter") == "无 frontmatter"


def test_load_core_memories_block(tmp_path):
    assert orch_cli.load_core_memories_block(tmp_path) == ""  # 目录不存在
    d = tmp_path / "core"
    d.mkdir()
    assert orch_cli.load_core_memories_block(d) == ""  # 目录为空
    (d / "proj-pref.md").write_text("报告必须含改进建议", encoding="utf-8")
    block = orch_cli.load_core_memories_block(d)
    assert "proj-pref" in block and "报告必须含改进建议" in block


def test_parse_subagents_from_real_agents_dir():
    agents = orch_cli.parse_subagents(PROJECT_ROOT / ".claude" / "agents")
    assert {"executor", "security-guard"} <= set(agents.keys())
    assert agents["executor"]["model"] == "sonnet"
    assert agents["security-guard"]["model"] == "haiku"
    assert "大脑" not in agents["executor"]["prompt"][:20]  # 已剥离 frontmatter
    assert agents["security-guard"]["description"]


def test_build_brain_env_block_lists_skills_and_agents():
    block = orch_cli.build_brain_env_block(PROJECT_ROOT)
    assert "运行环境" in block
    assert str(PROJECT_ROOT.resolve()) in block
    assert "executor" in block and "security-guard" in block
    for name in ("data-expert", "web-expert", "back-expert"):
        assert name in block


def test_build_system_prompt_composes_workflow_and_env():
    prompt = orch_cli.build_system_prompt(PROJECT_ROOT)
    assert "七阶段工作流" in prompt  # orchestrator SKILL.md 正文
    assert "运行环境（orch CLI 注入）" in prompt
    # 核心记忆块是否注入由 load_core_memories_block 决定（空目录不注入），单独已测


def test_build_options_wires_agents_and_resume():
    options = orch_cli.build_options(PROJECT_ROOT, model="sonnet", max_turns=42,
                                     permission_mode="acceptEdits",
                                     resume_session_id="sess-test")
    assert options.max_turns == 42
    assert options.permission_mode == "acceptEdits"
    assert options.model == "sonnet"
    assert options.resume == "sess-test"
    assert {"executor", "security-guard"} <= set(options.agents.keys())


def test_session_file_roundtrip(tmp_path):
    f = tmp_path / "sess.json"
    orch_cli.save_session("sess-abc", path=f)
    assert orch_cli.load_session(path=f) == "sess-abc"
    orch_cli.save_session(None, path=f)  # 空不覆盖
    assert orch_cli.load_session(path=f) == "sess-abc"
    assert orch_cli.load_session(path=tmp_path / "missing.json") is None


def test_handle_command_registry(capsys):
    s = orch_cli.Session(model="sonnet", max_turns=10, permission_mode="default")
    s.session_id = "sess-old"
    assert orch_cli.handle_command("/new", s) is True
    assert s.session_id is None
    assert orch_cli.handle_command("/cost", s) is True  # 累计用量显示
    assert "turns=0" in capsys.readouterr().out
    assert orch_cli.handle_command("/exit", s) is True
    assert s.stop is True
    s.stop = False
    assert orch_cli.handle_command("/不存在的命令", s) is True  # 提示但不发送
    assert not capsys.readouterr().out.startswith("你>")
