"""skillhub：catalog + 完整性校验 + 安装器。

全部在 tmp 项目根构造假技能目录做对账，不触碰真实仓库的
workspace/agent/skillhub/ 与 workspace/agent/skills/。
"""
import pytest

from agent_core import skillhub
from agent_core.skillhub import (add_skill, auto_sync, generate_mcp_config,
                                 hash_dir, install, load_catalog,
                                 update_skill, verify_skill)


def make_skill(root, name, *, files=None, extra_meta=""):
    """在 <root>/workspace/agent/skills/<name>/ 造一个技能（含 SKILL.md）。"""
    d = root / "workspace" / "agent" / "skills" / name
    d.mkdir(parents=True, exist_ok=True)
    meta = (f"---\nname: {name}\nversion: 1.0.0\ndescription: {name} 技能\n"
            f"{extra_meta}---\n正文 {name}\n")
    (d / "SKILL.md").write_text(meta, encoding="utf-8")
    for rel, content in (files or {}).items():
        p = d / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
    return d


def make_project(root, *names):
    """建项目根并登记技能；返回 (root, 技能目录 dict)。"""
    dirs = {}
    for n in names:
        dirs[n] = make_skill(root, n)
    return root, dirs


@pytest.fixture
def project(tmp_path):
    """带一个 verified 技能 demo-skill 的项目根（catalog 已 seed）。"""
    root = make_project(tmp_path, "demo-skill")[0]
    add_skill("demo-skill", project_root=root)
    verify_skill("demo-skill", mark_verified=True, project_root=root)
    return root


# ---------------------------------------------------------------------------
# hash_dir / add
# ---------------------------------------------------------------------------


def test_hash_dir_skips_pycache(tmp_path):
    d = make_skill(tmp_path, "exp-skill")
    (d / "__pycache__").mkdir()
    (d / "__pycache__" / "m.pyc").write_bytes(b"\x00")
    files = hash_dir(d)
    assert "SKILL.md" in files and len(files) == 1
    assert not any("__pycache__" in k for k in files)


def test_add_skill_writes_catalog_experimental(tmp_path):
    root, dirs = make_project(tmp_path, "new-skill")
    entry = add_skill("new-skill", project_root=root)
    assert entry["status"] == "experimental"  # 未检验
    assert entry["source"] == "../skills/new-skill"
    assert len(entry["files"]) == 1 and "SKILL.md" in entry["files"]
    cat = load_catalog(project_root=root)
    assert [s["name"] for s in cat["skills"]] == ["new-skill"]
    # 重 add 同名 → 替换不重复
    add_skill("new-skill", project_root=root)
    assert len(load_catalog(project_root=root)["skills"]) == 1


def test_add_skill_missing_source_raises(tmp_path):
    with pytest.raises(ValueError):
        add_skill("nope", project_root=tmp_path)


# ---------------------------------------------------------------------------
# verify / update
# ---------------------------------------------------------------------------


def test_verify_mismatch_then_mark_verified(tmp_path):
    root, dirs = make_project(tmp_path, "s")
    add_skill("s", project_root=root)
    assert verify_skill("s", project_root=root)["match"] is True

    # 改源 → 漂移 → 不通过
    (dirs["s"] / "SKILL.md").write_text("---\nname: s\n---\n变了\n", encoding="utf-8")
    rep = verify_skill("s", project_root=root)
    assert rep["match"] is False and rep["mismatches"] == ["SKILL.md"]

    # 复原 → 通过并置位
    (dirs["s"] / "SKILL.md").write_text(
        "---\nname: s\nversion: 1.0.0\ndescription: s 技能\n---\n正文 s\n",
        encoding="utf-8")
    assert verify_skill("s", project_root=root)["match"] is True
    rep = verify_skill("s", mark_verified=True, project_root=root)
    assert rep["status"] == "verified"
    assert load_catalog(project_root=root)["skills"][0]["status"] == "verified"


def test_update_rehashes_and_resets_status(tmp_path):
    root, dirs = make_project(tmp_path, "s")
    add_skill("s", project_root=root)
    verify_skill("s", mark_verified=True, project_root=root)

    (dirs["s"] / "SKILL.md").write_text(
        "---\nname: s\nversion: 2.0.0\ndescription: 升级\n---\n正文 v2\n",
        encoding="utf-8")
    entry = update_skill("s", project_root=root)
    # 内容变了：hash 重算、status 回实验态（旧评审失效）
    assert entry["version"] == "2.0.0"
    assert entry["status"] == "experimental"
    assert verify_skill("s", project_root=root)["match"] is True


def test_verify_unknown_skill_raises(tmp_path):
    make_project(tmp_path, "s")
    with pytest.raises(ValueError):
        verify_skill("nope", project_root=tmp_path)


# ---------------------------------------------------------------------------
# install
# ---------------------------------------------------------------------------


def test_install_copies_verified_and_skips_idempotent(project, tmp_path):
    node = tmp_path / "node"
    rep = install(target=node, project_root=project)
    assert rep["installed"] == ["demo-skill"] and not rep["refused"]
    skd = node / "workspace/agent/skills/demo-skill"
    assert (skd / "SKILL.md").read_text(encoding="utf-8").startswith("---\nname:")
    # 幂等二跑 → skipped
    rep2 = install(target=node, project_root=project)
    assert rep2["installed"] == []
    assert rep2["skipped"]["demo-skill"] == "已安装且校验通过"


def test_install_experimental_skipped_unless_optin(tmp_path):
    root = make_project(tmp_path, "exp-skill")[0]
    add_skill("exp-skill", project_root=root)  # experimental
    node = tmp_path / "node"
    rep = install(target=node, project_root=root)
    assert "exp-skill" not in rep["installed"]
    assert "非 verified" in rep["skipped"]["exp-skill"]
    rep2 = install(target=node, project_root=root, include_experimental=True)
    assert rep2["installed"] == ["exp-skill"]
    assert (node / "workspace/agent/skills/exp-skill/SKILL.md").is_file()


def test_install_refuses_tampered_source(project, tmp_path):
    (project / "workspace/agent/skills/demo-skill/SKILL.md").write_text(
        "---\nname: demo-skill\n---\n被篡改\n", encoding="utf-8")
    node = tmp_path / "node"
    # 硬门：--include-experimental 不豁免
    rep = install(target=node, project_root=project,
                  include_experimental=True)
    assert rep["installed"] == []
    assert "demo-skill" in rep["refused"]
    assert not (node / "workspace/agent/skills").exists()  # 不落盘
    assert "漂移" in rep["refused"]["demo-skill"][0]


def test_install_dry_run_zero_writes(project, tmp_path):
    node = tmp_path / "node"
    rep = install(target=node, project_root=project, dry_run=True)
    assert rep["installed"] == ["demo-skill"] and rep["dry_run"] is True
    assert not (node / "workspace").exists()  # 技能与报告都不写


def test_install_name_filter_and_unknown(project, tmp_path):
    node = tmp_path / "node"
    # 全部装好后再 --name 过滤 → 全 skipped
    install(target=node, project_root=project)
    rep = install(target=node, project_root=project, names="demo-skill")
    assert rep["installed"] == [] and "demo-skill" in rep["skipped"]
    with pytest.raises(ValueError):
        install(target=node, project_root=project, names="ghost")


def test_install_repairs_target_drift(project, tmp_path):
    node = tmp_path / "node"
    install(target=node, project_root=project)
    skd = node / "workspace/agent/skills/demo-skill"
    (skd / "SKILL.md").write_text("hack\n", encoding="utf-8")
    rep = install(target=node, project_root=project)
    assert rep["installed"] == ["demo-skill"]  # 目标被改 → 覆盖回 catalog 版本
    assert (skd / "SKILL.md").read_text(encoding="utf-8").startswith("---")


def test_install_with_mcp_writes_file(project, tmp_path, monkeypatch):
    # 先登记一个 MCP 条目（catalog 是单文件，直接改后存档）
    cat = load_catalog(project_root=project)
    cat["mcp_servers"] = [{
        "name": "alpha", "status": "verified", "kind": "command",
        "config": {"command": "python", "args": ["-m", "demo"],
                   "env": {"K": "${DEMO_K}"}},
        "env_vars": [{"name": "DEMO_K", "required": True, "description": ""}],
    }]
    skillhub.save_catalog(cat, project_root=project)
    monkeypatch.setenv("DEMO_K", "secret")
    node = tmp_path / "node"
    rep = install(target=node, project_root=project, with_mcp=True)
    assert rep["mcp"]["written"] is True
    mcp_path = node / "workspace/agent/skillhub_runtime/mcp.json"
    assert mcp_path.is_file()
    import json
    data = json.loads(mcp_path.read_text(encoding="utf-8"))
    assert data["mcpServers"]["alpha"]["env"]["K"] == "secret"
    # 报告也落盘
    assert (node / "workspace/agent/skillhub_runtime/last-install.json").is_file()


# ---------------------------------------------------------------------------
# generate_mcp_config
# ---------------------------------------------------------------------------


def test_mcp_env_substitution():
    servers = [{"name": "alpha", "kind": "command",
                "config": {"command": "python", "env": {"K": "${K}"}},
                "env_vars": [{"name": "K", "required": True, "description": ""}]}]
    out, warns = generate_mcp_config(servers, env={"K": "v1"})
    assert out["mcpServers"]["alpha"]["env"]["K"] == "v1"
    assert warns == []


def test_mcp_missing_required_skips_server():
    servers = [{"name": "alpha", "kind": "command",
                "config": {"command": "python", "env": {"K": "${K}"}},
                "env_vars": [{"name": "K", "required": True, "description": ""}]}]
    out, warns = generate_mcp_config(servers, env={})
    assert "alpha" not in out["mcpServers"]
    assert any("缺少必需环境变量" in w for w in warns)


def test_mcp_optional_missing_keeps_placeholder():
    servers = [{"name": "beta", "kind": "url",
                "config": {"url": "https://x/?k=${T}"},
                "env_vars": [{"name": "T", "required": False, "description": ""}]}]
    out, _ = generate_mcp_config(servers, env={})
    assert "beta" in out["mcpServers"]
    assert "${T}" in out["mcpServers"]["beta"]["url"]


def test_mcp_default_fallback_and_invalid_entry():
    servers = [
        {"name": "gamma", "kind": "command",
         "config": {"command": "e", "env": {"P": "${PORT}"}},
         "env_vars": [{"name": "PORT", "required": False,
                       "description": "", "default": "8080"}]},
        {"name": "broken", "kind": "url", "config": {},
         "env_vars": []},
    ]
    out, warns = generate_mcp_config(servers, env={"PORT": "9000"})
    # 环境优先于 default
    assert out["mcpServers"]["gamma"]["env"]["P"] == "9000"
    assert "broken" not in out["mcpServers"]
    assert any("缺少" in w and "command/url" in w for w in warns)


# ---------------------------------------------------------------------------
# auto_sync
# ---------------------------------------------------------------------------


def test_auto_sync_self_reconcile_noop(project):
    rep = auto_sync(str(project))
    assert rep["installed"] == []            # 自我对账已是 verified → 幂等
    assert rep["skipped"].get("demo-skill")
    assert rep["refused"] == {}


def test_auto_sync_missing_catalog_no_crash(tmp_path):
    rep = auto_sync(str(tmp_path))
    assert rep["installed"] == []  # 无 catalog：空对账，不抛异常