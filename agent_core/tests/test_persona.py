"""测试 agent_core.persona：frontmatter 处理、人设加载、子 agent 批量解析。"""
from pathlib import Path

import pytest

from agent_core.persona import (Persona, _normalize_tools, _split_frontmatter,
                                load_persona, load_persona_by_name,
                                parse_subagents, strip_frontmatter)


# ---- strip_frontmatter ----

def test_strip_frontmatter_normal():
    text = "---\nname: executor\nmodel: sonnet\n---\n\n正文内容在这里。\n"
    assert strip_frontmatter(text) == "正文内容在这里。"


def test_strip_frontmatter_no_frontmatter_returns_as_is():
    text = "这是一段没有 frontmatter 的普通文本。"
    assert strip_frontmatter(text) == text


def test_strip_frontmatter_empty_string():
    assert strip_frontmatter("") == ""


def test_strip_frontmatter_multiline_body():
    text = "---\nname: x\n---\n第一行\n第二行\n第三行"
    body = strip_frontmatter(text)
    assert "第一行" in body
    assert "第三行" in body
    assert "name: x" not in body  # frontmatter 被剥离


def test_split_frontmatter_incomplete_returns_original():
    """以 --- 开头但不足三段：按无 frontmatter 处理。"""
    text = "---\nname: x\n"  # 只有两段
    meta, body = _split_frontmatter(text)
    assert meta == {}
    assert body == text.strip()


def test_split_frontmatter_bad_yaml_yields_empty_meta():
    """YAML 解析失败：meta 为空，正文仍尽量保留。"""
    text = "---\n: [bad yaml: {\n---\n正文保留"
    meta, body = _split_frontmatter(text)
    assert meta == {}
    assert "正文保留" in body


# ---- _normalize_tools ----

def test_normalize_tools_comma_string():
    assert _normalize_tools("Read, Write, Grep") == ["Read", "Write", "Grep"]


def test_normalize_tools_list():
    assert _normalize_tools(["Read", "Bash"]) == ["Read", "Bash"]


def test_normalize_tools_none_and_empty():
    assert _normalize_tools(None) is None
    assert _normalize_tools("") is None
    assert _normalize_tools("   ") is None


# ---- load_persona（真实文件）----

def test_load_persona_from_real_executor(persona_dir):
    p = Path(persona_dir) / "executor.md"
    persona = load_persona(p)
    assert isinstance(persona, Persona)
    assert persona.name == "executor"
    assert persona.model == "sonnet"
    assert "executor" in persona.system_prompt
    assert persona.description != ""
    # executor.md frontmatter 未限定 tools，应为 None（放开全部工具）
    assert persona.tools is None


def test_load_persona_security_guard_tools_parsed(persona_dir):
    p = Path(persona_dir) / "security-guard.md"
    persona = load_persona(p)
    assert persona.name == "security-guard"
    assert persona.model == "haiku"
    # frontmatter 的 tools 逗号串转列表
    assert persona.tools == ["Read", "Glob", "Grep", "WebFetch"]


def test_load_persona_missing_file_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        load_persona(tmp_path / "does-not-exist.md")


def test_load_persona_name_defaults_to_stem(tmp_path):
    """frontmatter 无 name 时，name 取文件名主干。"""
    f = tmp_path / "my-agent.md"
    f.write_text("---\ndescription: 描述\n---\n正文", encoding="utf-8")
    persona = load_persona(f)
    assert persona.name == "my-agent"
    assert persona.description == "描述"
    assert persona.system_prompt == "正文"


# ---- load_persona_by_name ----

def test_load_persona_by_name_executor(persona_dir):
    persona = load_persona_by_name("executor", persona_dir)
    assert persona.name == "executor"


def test_load_persona_by_name_security_guard(persona_dir):
    persona = load_persona_by_name("security-guard", persona_dir)
    assert persona.name == "security-guard"


def test_load_persona_by_name_missing_raises(persona_dir):
    with pytest.raises(FileNotFoundError):
        load_persona_by_name("no-such-agent", persona_dir)


# ---- parse_subagents ----

def test_parse_subagents_real_dir(persona_dir):
    agents = parse_subagents(persona_dir)
    assert isinstance(agents, dict)
    assert "executor" in agents
    assert "security-guard" in agents
    # 每个条目为 dict，含 description 与 prompt
    for name, definition in agents.items():
        assert isinstance(definition, dict)
        assert "description" in definition
        assert "prompt" in definition
        assert isinstance(definition["prompt"], str)


def test_parse_subagents_shape_fields(persona_dir):
    agents = parse_subagents(persona_dir)
    sg = agents["security-guard"]
    assert sg.get("model") == "haiku"
    assert sg.get("tools") == ["Read", "Glob", "Grep", "WebFetch"]
    ex = agents["executor"]
    assert ex.get("model") == "sonnet"


def test_parse_subagents_missing_dir(tmp_path):
    assert parse_subagents(tmp_path / "not-a-dir") == {}


def test_parse_subagents_skips_entries_without_name_or_description(tmp_path):
    d = tmp_path / "agents"
    d.mkdir()
    # 缺 description：跳过
    (d / "no-desc.md").write_text("---\nname: nodesc\n---\n正文", encoding="utf-8")
    # 缺 name：跳过
    (d / "no-name.md").write_text("---\ndescription: 有描述\n---\n正文", encoding="utf-8")
    # 完整：保留
    (d / "good.md").write_text(
        "---\nname: good\ndescription: 好人设\n---\n好正文", encoding="utf-8")
    agents = parse_subagents(d)
    assert set(agents.keys()) == {"good"}
    assert agents["good"]["description"] == "好人设"
    assert agents["good"]["prompt"] == "好正文"


# ---- 损坏文件容错 ----

def test_parse_subagents_tolerates_corrupt_file(tmp_path):
    """目录里混入格式损坏的 .md，不应崩溃，且有效条目仍被解析。"""
    d = tmp_path / "agents"
    d.mkdir()
    # 损坏：非法 YAML frontmatter
    (d / "broken.md").write_text(
        "---\nname: [unclosed\n: bad: : yaml:\n---\n乱码正文", encoding="utf-8")
    # 有效条目
    (d / "valid.md").write_text(
        "---\nname: valid\ndescription: 有效\nmodel: sonnet\n---\n有效正文",
        encoding="utf-8")
    agents = parse_subagents(d)  # 不抛异常
    assert isinstance(agents, dict)
    assert "valid" in agents
    assert agents["valid"]["prompt"] == "有效正文"


def test_load_persona_tolerates_bad_frontmatter(tmp_path):
    """单文件 frontmatter 损坏时仍可加载：meta 空，name 回退到文件名。"""
    f = tmp_path / "weird.md"
    f.write_text("---\n: [bad\n---\n正文尚在", encoding="utf-8")
    persona = load_persona(f)
    assert persona.name == "weird"       # 回退文件名主干
    assert "正文尚在" in persona.system_prompt
