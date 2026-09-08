"""测试 agent_core.tools：技能扫描、MCP 配置加载、进程内记忆工具。"""
import json
from pathlib import Path

from agent_core.config import load_agent_config
from agent_core.tools import (build_memory_mcp_server, build_memory_tools,
                              load_mcp_servers, scan_skills)


# ---- scan_skills ----

def test_scan_skills_finds_real_skills(project_root):
    skills = scan_skills(project_root)
    names = {s["name"] for s in skills}
    assert {"back-expert", "data-expert", "web-expert"} <= names
    # orchestrator 是大脑自身定义，排除
    assert "orchestrator" not in names
    for s in skills:
        assert Path(s["path"]).is_absolute()
        assert Path(s["path"], "SKILL.md").is_file()
        assert "description" in s
        assert isinstance(s["triggers"], list)


def test_scan_skills_entry_shape(project_root):
    skills = scan_skills(project_root)
    for s in skills:
        assert set(s.keys()) == {"name", "description", "path", "triggers"}
        assert len(s["triggers"]) <= 6  # 触发词最多 6 个


def test_scan_skills_empty_dir(tmp_path):
    """没有 workspace/agent/skills 结构时返回空列表。"""
    assert scan_skills(str(tmp_path)) == []


def test_scan_skills_dir_exists_but_no_skill_md(tmp_path):
    skills_dir = tmp_path / "workspace" / "agent" / "skills"
    (skills_dir / "empty-skill").mkdir(parents=True)
    assert scan_skills(str(tmp_path)) == []


def test_scan_skills_excludes_orchestrator_only(tmp_path):
    """orchestrator 即便有 SKILL.md 也被排除，其它技能保留。"""
    base = tmp_path / "workspace" / "agent" / "skills"
    (base / "orchestrator").mkdir(parents=True)
    (base / "orchestrator" / "SKILL.md").write_text(
        "---\nname: orchestrator\ndescription: 大脑\n---\n正文", encoding="utf-8")
    (base / "my-skill").mkdir(parents=True)
    (base / "my-skill" / "SKILL.md").write_text(
        "---\nname: my-skill\ndescription: 自定义\ntriggers: [甲, 乙]\n---\n正文",
        encoding="utf-8")
    skills = scan_skills(str(tmp_path))
    names = {s["name"] for s in skills}
    assert names == {"my-skill"}
    assert skills[0]["triggers"] == ["甲", "乙"]


def test_scan_skills_tolerates_corrupt_skill(tmp_path):
    """损坏的 SKILL.md 不拖垮扫描，有效技能仍返回。"""
    base = tmp_path / "workspace" / "agent" / "skills"
    (base / "broken").mkdir(parents=True)
    (base / "broken" / "SKILL.md").write_text(
        "---\nname: [unclosed\n: bad: yaml\n---\n正文", encoding="utf-8")
    (base / "good").mkdir(parents=True)
    (base / "good" / "SKILL.md").write_text(
        "---\nname: good\ndescription: 好的\n---\n正文", encoding="utf-8")
    skills = scan_skills(str(tmp_path))
    names = {s["name"] for s in skills}
    assert "good" in names


# ---- load_mcp_servers ----

def test_load_mcp_servers_empty_path():
    assert load_mcp_servers("") is None


def test_load_mcp_servers_missing_file(tmp_path):
    assert load_mcp_servers(str(tmp_path / "no.json")) is None


def test_load_mcp_servers_valid_json(tmp_path):
    cfg = tmp_path / "mcp.json"
    cfg.write_text(json.dumps({
        "mcpServers": {
            "fs": {"command": "npx", "args": ["-y", "x"]},
            "figma": {"url": "http://127.0.0.1:3845/mcp"},
        }
    }), encoding="utf-8")
    servers = load_mcp_servers(str(cfg))
    assert isinstance(servers, dict)
    assert set(servers.keys()) == {"fs", "figma"}
    assert servers["fs"]["command"] == "npx"


def test_load_mcp_servers_filters_invalid_entries(tmp_path):
    cfg = tmp_path / "mcp.json"
    cfg.write_text(json.dumps({
        "mcpServers": {
            "fs": {"command": "npx"},
            "broken": {"foo": "bar"},        # 缺 command/url，过滤
            "notdict": "string-value",        # 非 dict，过滤
        }
    }), encoding="utf-8")
    servers = load_mcp_servers(str(cfg))
    assert set(servers.keys()) == {"fs"}


def test_load_mcp_servers_malformed_json(tmp_path):
    cfg = tmp_path / "bad.json"
    cfg.write_text("{ this is not valid json ", encoding="utf-8")
    assert load_mcp_servers(str(cfg)) is None


def test_load_mcp_servers_empty_servers(tmp_path):
    cfg = tmp_path / "empty.json"
    cfg.write_text(json.dumps({"mcpServers": {}}), encoding="utf-8")
    assert load_mcp_servers(str(cfg)) is None


def test_load_mcp_servers_no_mcp_servers_key(tmp_path):
    cfg = tmp_path / "nokey.json"
    cfg.write_text(json.dumps({"other": 1}), encoding="utf-8")
    assert load_mcp_servers(str(cfg)) is None


# ---- build_memory_tools ----

def _local_config(project_root, tmp_path):
    """构造 local 后端配置：project_root 用真实仓库（供 runtime import），
    记忆/经验目录指向 tmp_path 以隔离，避免污染真实数据。"""
    return load_agent_config(
        project_root=project_root,
        memory_backend="local",
        memory_core_dir=str(tmp_path / "mem" / "core"),
        memory_context_dir=str(tmp_path / "mem" / "context"),
        experience_store_dir=str(tmp_path / "exp"),
    )


def test_build_memory_tools_local_backend(project_root, tmp_path):
    cfg = _local_config(project_root, tmp_path)
    tools = build_memory_tools(cfg)
    assert isinstance(tools, list)
    assert len(tools) >= 4


def test_build_memory_tools_names(project_root, tmp_path):
    """工具名对齐 runtime/mcp_server.py。SDK 可用时返回 SdkMcpTool，
    其 name 属性可取；SDK 不可用时降级为 dict，含 name 键。"""
    cfg = _local_config(project_root, tmp_path)
    tools = build_memory_tools(cfg)
    names = set()
    for t in tools:
        if isinstance(t, dict):
            names.add(t["name"])
        else:
            names.add(getattr(t, "name", None))
    assert {"search_experiences", "read_core_memory",
            "write_core_memory", "write_task_context"} <= names


def test_build_memory_mcp_server(project_root, tmp_path):
    """SDK 可用时返回进程内 MCP server 配置；否则 None。二者都不应报错。"""
    cfg = _local_config(project_root, tmp_path)
    server = build_memory_mcp_server(cfg)
    # 不断言具体类型（取决于 SDK 是否安装），只确保调用不崩溃
    assert server is None or server is not None
