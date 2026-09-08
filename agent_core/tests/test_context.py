"""测试 agent_core.context：核心记忆块、环境块、系统提示、用户消息前缀。"""
from pathlib import Path

from agent_core.context import (build_env_block, build_system_prompt,
                                build_user_message_prefix,
                                load_core_memories_block)
from agent_core.persona import Persona
from agent_core.tools import scan_skills


# ---- load_core_memories_block ----

def test_load_core_memories_block_with_files(tmp_path):
    core = tmp_path / "core"
    core.mkdir()
    (core / "proj-pref.md").write_text("项目偏好：使用 pytest。", encoding="utf-8")
    (core / "conventions.md").write_text("约定：中文注释。", encoding="utf-8")
    block = load_core_memories_block(core)
    assert "核心记忆" in block
    # 文件名主干作为小标题
    assert "proj-pref" in block
    assert "conventions" in block
    # 内容被包含
    assert "使用 pytest" in block
    assert "中文注释" in block
    # 按文件名排序：conventions 在 proj-pref 之前
    assert block.index("conventions") < block.index("proj-pref")


def test_load_core_memories_block_empty_dir(tmp_path):
    core = tmp_path / "core"
    core.mkdir()
    assert load_core_memories_block(core) == ""


def test_load_core_memories_block_missing_dir(tmp_path):
    assert load_core_memories_block(tmp_path / "no-such-dir") == ""


def test_load_core_memories_block_skips_empty_files(tmp_path):
    core = tmp_path / "core"
    core.mkdir()
    (core / "empty.md").write_text("   \n  ", encoding="utf-8")  # 只有空白
    (core / "real.md").write_text("有内容", encoding="utf-8")
    block = load_core_memories_block(core)
    assert "有内容" in block
    assert "empty" not in block  # 空白文件被跳过


# ---- build_env_block ----

def test_build_env_block_contains_project_root(project_root):
    block = build_env_block(project_root)
    assert "运行环境" in block
    assert str(Path(project_root).resolve()) in block


def test_build_env_block_lists_skills(project_root):
    skills = scan_skills(project_root)
    assert skills, "真实仓库应扫描到技能"
    block = build_env_block(project_root, skills=skills)
    for s in skills:
        assert s["name"] in block
        assert s["path"] in block  # executor 姿态给出绝对路径


def test_build_env_block_no_skills(project_root):
    block = build_env_block(project_root, skills=[])
    assert "无（未扫描到带 SKILL.md 的技能目录）" in block


def test_build_env_block_with_subagents(project_root):
    """大脑姿态：有 sub_agents 时输出「子 agent 已注册」。"""
    skills = scan_skills(project_root)
    sub_agents = {"executor": {"description": "执行"}, "security-guard": {"description": "审查"}}
    block = build_env_block(project_root, skills=skills, sub_agents=sub_agents)
    assert "子 agent 已注册" in block
    assert "executor" in block
    assert "security-guard" in block
    # 大脑姿态只列技能名，不列绝对路径
    assert "Agent 工具派发" in block


def test_build_env_block_triggers_hint(project_root):
    skills = [{"name": "demo", "path": "/abs/demo", "description": "演示技能",
               "triggers": ["做演示", "demo"]}]
    block = build_env_block(project_root, skills=skills)
    assert "触发词" in block
    assert "做演示" in block


# ---- build_system_prompt ----

def test_build_system_prompt_combines_all_parts(project_root, tmp_path):
    core = tmp_path / "core"
    core.mkdir()
    (core / "mem.md").write_text("记忆内容 XYZ", encoding="utf-8")
    persona = Persona(name="executor", system_prompt="你是 executor 人设正文。")
    skills = scan_skills(project_root)
    prompt = build_system_prompt(persona, project_root,
                                 core_memory_dir=core, skills=skills)
    # persona 正文
    assert "你是 executor 人设正文。" in prompt
    # 核心记忆块
    assert "核心记忆" in prompt
    assert "记忆内容 XYZ" in prompt
    # 环境块
    assert "运行环境" in prompt
    assert str(Path(project_root).resolve()) in prompt


def test_build_system_prompt_without_core_memory(project_root):
    persona = Persona(name="executor", system_prompt="人设正文")
    prompt = build_system_prompt(persona, project_root, core_memory_dir=None)
    assert "人设正文" in prompt
    assert "核心记忆" not in prompt
    assert "运行环境" in prompt


def test_build_system_prompt_empty_persona_still_has_env(project_root):
    persona = Persona(name="x", system_prompt="")
    prompt = build_system_prompt(persona, project_root)
    assert "运行环境" in prompt


def test_build_system_prompt_subagents_from_persona(project_root):
    persona = Persona(name="brain", system_prompt="大脑",
                      sub_agents={"executor": {"description": "执行"}})
    prompt = build_system_prompt(persona, project_root)
    assert "子 agent 已注册" in prompt


# ---- build_user_message_prefix ----

def test_build_user_message_prefix_contains_cwd(tmp_path):
    prefix = build_user_message_prefix(str(tmp_path))
    assert "任务工作目录" in prefix
    assert str(Path(tmp_path).resolve()) in prefix


def test_build_user_message_prefix_with_context_memory(tmp_path):
    prefix = build_user_message_prefix(str(tmp_path), context_memory="先读 tokens 文件")
    assert "上下文记忆" in prefix
    assert "先读 tokens 文件" in prefix


def test_build_user_message_prefix_no_context_memory(tmp_path):
    prefix = build_user_message_prefix(str(tmp_path))
    assert "上下文记忆" not in prefix


def test_task_dir_not_in_system_prompt_but_in_prefix(project_root, tmp_path):
    """KV-cache 稳定性：任务工作目录只进用户消息前缀，不进 system prompt。"""
    persona = Persona(name="executor", system_prompt="人设")
    system_prompt = build_system_prompt(persona, project_root)
    prefix = build_user_message_prefix(str(tmp_path))
    task_dir = str(Path(tmp_path).resolve())
    assert task_dir not in system_prompt
    assert task_dir in prefix
