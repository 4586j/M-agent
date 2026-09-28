"""统一技能 / MCP 仓库（skillhub）：catalog + 完整性校验 + 安装到新节点。

技能源不搬家，仍留在 ``workspace/agent/skills/<name>/``；本模块在其上叠一层
对账目录 ``workspace/agent/skillhub/catalog.json``（单一事实源）：逐文件
sha256 完整性 + status 评审标记（verified / experimental / deprecated）。

「检验过」双层门：
- 硬门（完整性）：install 前重算源 hash 对账 catalog，不符的源一律拒绝，
  即使 ``--include-experimental`` 也不豁免（防装到半成品）；
- 软门（评审）：默认只装 ``status: verified``，其余须 ``--include-experimental``。

CLI（从仓库根跑 ``python -m agent_core.skillhub``）：
  list [--status a,b]            列 catalog：技能 × 源存在 × hash 对账
  add <name>                     扫描源技能入 catalog（status=experimental）
  verify <name> [--mark-verified] 重算源 hash 对账；--mark-verified 置 verified
  update <name>                  重算 hash 刷新 catalog（内容已变，回实验态）
  install --target <节点根> [...] 校验后把 verified 技能装进目标节点
"""
import argparse
import hashlib
import json
import logging
import os
import re
import sys
from pathlib import Path, PurePosixPath

log = logging.getLogger(__name__)

SCHEMA_VERSION = 1

# 模块位于 agent_core/ 下：仓库根 = 其上一级（不随 CWD 漂移）
MODULE_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = MODULE_DIR.parent

# 占位符 ${VAR}：MCP 配置模板里的环境变量引用，install 时从环境替换
_PLACEHOLDER_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


# ---------------------------------------------------------------------------
# 路径与 catalog 读写
# ---------------------------------------------------------------------------


def skillhub_dir(project_root: str | Path | None = None) -> Path:
    """skillhub 目录（catalog.json 所在）。缺省锚定本模块所在仓库根。"""
    root = Path(project_root) if project_root else PROJECT_ROOT
    return root / "workspace" / "agent" / "skillhub"


def skills_source_dir(project_root: str | Path | None = None) -> Path:
    """技能源目录（catalog 索引的对象，即 workspace/agent/skills/）。"""
    root = Path(project_root) if project_root else PROJECT_ROOT
    return root / "workspace" / "agent" / "skills"


def load_catalog(project_root=None) -> dict:
    """读 catalog；文件缺失返回空 catalog（skillhub 目录可渐进式建立）。"""
    cat = skillhub_dir(project_root) / "catalog.json"
    if not cat.is_file():
        return {"schema_version": SCHEMA_VERSION, "skills": [], "mcp_servers": []}
    data = json.loads(cat.read_text(encoding="utf-8"))
    data.setdefault("skills", [])
    data.setdefault("mcp_servers", [])
    return data


def save_catalog(data: dict, project_root=None) -> Path:
    cat = skillhub_dir(project_root) / "catalog.json"
    cat.parent.mkdir(parents=True, exist_ok=True)
    cat.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n",
                   encoding="utf-8")
    return cat


def _catalog_skill(cat: dict, name: str) -> dict | None:
    return next((s for s in cat.get("skills", []) if s["name"] == name), None)


# ---------------------------------------------------------------------------
# 完整性：逐文件 sha256
# ---------------------------------------------------------------------------

_SKIP_DIRS = {"__pycache__", ".pytest_cache"}


def _sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def hash_dir(d: str | Path) -> dict[str, str]:
    """递归计算目录内每个文件的 sha256，返回 {相对路径(posix): hex}。

    跳过 ``__pycache__``（字节码随解释器漂移，不属于技能内容），
    保证 hash 对账只看源码与文档。
    """
    base = Path(d)
    out: dict[str, str] = {}
    for p in sorted(base.rglob("*")):
        if not p.is_file():
            continue
        rel = p.relative_to(base)
        if any(part in _SKIP_DIRS for part in rel.parts):
            continue
        out[rel.as_posix()] = _sha256_file(p)
    return out


# ---------------------------------------------------------------------------
# 技能元数据（SKILL.md frontmatter）
# ---------------------------------------------------------------------------


def _skill_meta(skill_dir: Path) -> dict:
    """解析 SKILL.md frontmatter 的 name/version/description/requires_mcp。

    description 取首行、triggers 不取——与 scan_skills（agent_core/tools.py）
    的技能语义对齐；解析失败或字段缺失时给出可用的退化值。
    """
    meta: dict = {}
    md = skill_dir / "SKILL.md"
    if md.is_file():
        text = md.read_text(encoding="utf-8")
        if text.startswith("---"):
            parts = text.split("---", 2)
            if len(parts) >= 3:
                try:
                    import yaml
                    meta = yaml.safe_load(parts[1]) or {}
                except Exception:
                    meta = {}
    return {
        "name": str(meta.get("name") or skill_dir.name),
        "version": str(meta.get("version") or ""),
        "description": (str(meta.get("description") or "").strip().split("\n")[0]),
        "requires_mcp": [str(r) for r in (meta.get("requires_mcp") or [])],
    }


# ---------------------------------------------------------------------------
# 子命令实现
# ---------------------------------------------------------------------------


def add_skill(name: str, project_root=None) -> dict:
    """扫描 workspace/agent/skills/<name>/ 入 catalog（status=experimental）。"""
    src = skills_source_dir(project_root) / name
    if not (src / "SKILL.md").is_file():
        raise ValueError(f"技能源不存在（缺 SKILL.md）：{src}")
    files = hash_dir(src)
    if not files:
        raise ValueError(f"技能目录为空：{src}")
    meta = _skill_meta(src)
    cat = load_catalog(project_root)
    skills = cat["skills"]
    skills[:] = [s for s in skills if s["name"] != name]  # 同名校验：替换既有条目
    skills.append({
        "name": name,
        "source": f"../skills/{name}",
        "version": meta["version"],
        "status": "experimental",   # 未检验：新扫一律回到实验态，评审后才放行
        "description": meta["description"],
        "requires_mcp": meta["requires_mcp"],
        "files": files,
    })
    cat["skills"] = sorted(skills, key=lambda s: s["name"])
    save_catalog(cat, project_root)
    return _catalog_skill(cat, name)


def _source_hashes(cat: dict, name: str, project_root=None) -> tuple[dict | None, dict]:
    """返回 (entry, 源当前 hash)。catalog 无该技能或源缺失 → entry=None。"""
    entry = _catalog_skill(cat, name)
    if entry is None:
        return None, {}
    src = skills_source_dir(project_root) / name
    if not (src / "SKILL.md").is_file():
        return entry, {}
    return entry, hash_dir(src)


def _hash_diff(current: dict, catalog_files: dict) -> list[str]:
    """源 hash 与 catalog 对账：列出不一致的相对路径（含增删文件）。"""
    return sorted(k for k in set(current) | set(catalog_files)
                  if current.get(k) != catalog_files.get(k))


def verify_skill(name: str, *, mark_verified: bool = False,
                 project_root=None) -> dict:
    """重算源 hash 对账 catalog，报告完整性；--mark-verified 通过后置 verified。"""
    cat = load_catalog(project_root)
    entry = _catalog_skill(cat, name)
    if entry is None:
        raise ValueError(f"catalog 无技能 {name}（先 skillhub add {name}）")
    src = skills_source_dir(project_root) / name
    if not (src / "SKILL.md").is_file():
        raise ValueError(f"技能源不存在：{src}")
    current = hash_dir(src)
    mismatches = _hash_diff(current, entry.get("files") or {})
    report = {
        "name": name,
        "status": entry["status"],
        "files": len(entry.get("files") or {}),
        "match": not mismatches,
        "mismatches": mismatches,
    }
    if not mismatches and mark_verified:
        saved = entry["status"]
        entry["status"] = "verified"
        save_catalog(cat, project_root)
        report["status"] = "verified"
        report["marked"] = saved != "verified"
    return report


def update_skill(name: str, project_root=None) -> dict:
    """技能改动后重算 hash 刷新 catalog；内容已变 → 原评审失效，回实验态。"""
    cat = load_catalog(project_root)
    entry = _catalog_skill(cat, name)
    if entry is None:
        raise ValueError(f"catalog 无技能 {name}（先 skillhub add {name}）")
    src = skills_source_dir(project_root) / name
    if not (src / "SKILL.md").is_file():
        raise ValueError(f"技能源不存在：{src}")
    meta = _skill_meta(src)
    entry.update({
        "version": meta["version"],
        "description": meta["description"],
        "requires_mcp": meta["requires_mcp"],
        "files": hash_dir(src),
        "status": "experimental",  # 评审标记只对「当时那份内容」有效
    })
    save_catalog(cat, project_root)
    return entry


# ---------------------------------------------------------------------------
# MCP 配置生成
# ---------------------------------------------------------------------------


def generate_mcp_config(servers: list[dict], env: dict | None = None,
                        ) -> tuple[dict, list[str]]:
    """从 catalog 的 mcp_servers 条目生成 ``{"mcpServers": {名称: 配置}}``。

    - ``env_vars`` 声明条目所需环境变量（name / required / description / default）；
    - 必需的变量在 env（缺省为 os.environ）与 default 中都取不到 → **跳过该服务器并告警**
      （宁缺不装缺密钥的 MCP）；
    - 值齐后用实际值替换 config 里的 ``${VAR}`` 占位符；
    - 未声明的 ``${VAR}`` 按 os.environ 解析，取不到则保留占位符（可选语义）。

    返回 ``(生成结果, warnings)``。
    """
    env = dict(os.environ) if env is None else dict(env)
    warnings: list[str] = []
    result: dict[str, dict] = {}

    for s in servers:
        name = s.get("name")
        if not name:
            warnings.append("MCP 条目缺 name，跳过")
            continue
        if name in result:
            warnings.append(f"MCP 条目重名 '{name}'，只保留首个")
            continue
        declared = {ev["name"]: ev for ev in s.get("env_vars", [])
                    if isinstance(ev, dict) and ev.get("name")}

        def resolve(v):
            val = env.get(v)
            if val is None or val == "":
                d = declared.get(v)
                if d and d.get("default"):
                    return str(d["default"])
                return None
            return val

        missing = sorted(v for v, d in declared.items()
                         if d.get("required") and resolve(v) is None)
        if missing:
            warnings.append(f"MCP 条目 '{name}'：缺少必需环境变量 "
                            f"{', '.join(missing)}，跳过")
            continue
        cfg = _subst_placeholders(dict(s.get("config") or {}), resolve)
        if not (cfg.get("command") or cfg.get("url")):
            warnings.append(f"MCP 条目 '{name}'：config 缺少 command/url 字段，跳过")
            continue
        result[name] = cfg
    return {"mcpServers": result}, warnings


def _subst_placeholders(value, resolve) -> object:
    """递归替换字符串里的 ``${VAR}``；取不到则保留占位符。"""
    if isinstance(value, str):
        return _PLACEHOLDER_RE.sub(
            lambda m: str(resolve(m.group(1)) or m.group(0)), value)
    if isinstance(value, dict):
        return {k: _subst_placeholders(v, resolve) for k, v in value.items()}
    if isinstance(value, list):
        return [_subst_placeholders(v, resolve) for v in value]
    return value


# ---------------------------------------------------------------------------
# 安装
# ---------------------------------------------------------------------------


def install(*, target: str | Path, names: str | None = None,
            include_experimental: bool = False, with_mcp: bool = False,
            dry_run: bool = False, project_root=None, env: dict | None = None,
            write_report: bool = True) -> dict:
    """把 catalog 里通过校验的技能装进 ``<target>/workspace/agent/skills/``。

    - 硬门：源 hash 与 catalog 不符 → refused（不落盘，--include-experimental 不豁免）；
    - 软门：默认只装 verified；--include-experimental 才放行 experimental；
      deprecated 永不安装；
    - 幂等：目标已存在且 hash 相同 → skipped；
    - 目标与源不一致（缺失/漂移）→ 补齐/覆盖回 catalog 版本；
    - ``--with-mcp``：把 mcp.json 生成到 ``<target>/workspace/agent/skillhub_runtime/``；
    - ``--dry-run``：只报告不写盘（含不写 report / mcp 文件）。

    返回报告 ``{installed, skipped, refused, reasons, mcp, warnings, dry_run}``。
    """
    target = Path(target)
    cat = load_catalog(project_root)
    src_root = skills_source_dir(project_root)
    skill_dst = target / "workspace" / "agent" / "skills"
    runtime_dst = target / "workspace" / "agent" / "skillhub_runtime"

    picked = cat["skills"]
    if names:
        want = [n for n in names.split(",") if n.strip()]
        missing = [n for n in want if not any(s["name"] == n for s in picked)]
        if missing:
            raise ValueError(f"catalog 无这些技能：{', '.join(missing)}")
        picked = [s for s in picked if s["name"] in want]

    report = {
        "installed": [],
        "skipped": {},
        "refused": {},
        "reasons": [],
        "mcp": {"written": False, "path": ""},
        "warnings": [],
        "dry_run": dry_run,
    }

    for s in picked:
        name = s["name"]
        status = s.get("status")
        if status == "deprecated":
            report["skipped"][name] = "deprecated，已下架不安装"
            continue
        if status != "verified" and not include_experimental:
            report["skipped"][name] = (
                f"status={status}，非 verified（可用 --include-experimental 放行）")
            continue

        src = src_root / name
        current = hash_dir(src) if (src / "SKILL.md").is_file() else {}
        if not current:
            report["refused"][name] = ["技能源缺失（缺 SKILL.md）：" + str(src)]
            continue
        catalog_files = s.get("files") or {}
        mism = _hash_diff(current, catalog_files)
        if mism:
            report["refused"][name] = [
                "源 hash 与 catalog 不符（漂移？先 skillhub update "
                + f"{name}）：{', '.join(mism)}"]
            continue

        # 目标对账：全部一致 → 幂等跳过；否则补齐/覆盖（目标在技能名下）
        dst_dir = skill_dst / name
        diff = [rel for rel, digest in catalog_files.items()
                if not _sha256_matches(dst_dir / PurePosixPath(rel), digest)]
        if not diff:
            report["skipped"][name] = "已安装且校验通过"
            continue
        if not dry_run:
            for rel, digest in catalog_files.items():
                dst_file = dst_dir / PurePosixPath(rel)
                if not _sha256_matches(dst_file, digest):
                    dst_file.parent.mkdir(parents=True, exist_ok=True)
                    dst_file.write_bytes((src / PurePosixPath(rel)).read_bytes())
        report["installed"].append(name)
        report["reasons"].append(f"{name}：{'dry-run 待安装' if dry_run else '已安装'}")

    if with_mcp:
        generated, warns = generate_mcp_config(cat.get("mcp_servers", []), env=env)
        report["warnings"] = warns
        if generated["mcpServers"]:
            report["mcp"]["written"] = True
            if not dry_run:
                runtime_dst.mkdir(parents=True, exist_ok=True)
                mcp_path = runtime_dst / "mcp.json"
                mcp_path.write_text(
                    json.dumps(generated, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8")
                report["mcp"]["path"] = str(mcp_path)

    if write_report and not dry_run:
        runtime_dst.mkdir(parents=True, exist_ok=True)
        (runtime_dst / "last-install.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8")
    return report


def _sha256_matches(path: Path, digest: str) -> bool:
    return path.is_file() and _sha256_file(path) == digest


def auto_sync(project_root: str) -> dict:
    """worker 启动自动同步：把本项目 catalog 里 verified 技能对账到本项目。

    只修技能、不碰 MCP 文件（避免启动时覆盖运维手改的 MCP 配置）。任何失败
    只告警不抛异常（不阻断启动），返回报告供上层落日志。
    """
    try:
        report = install(target=project_root, project_root=project_root)
        if report["refused"]:
            log.warning("skillhub 自动同步：以下技能被拒绝（源漂移？先 skillhub "
                        "update 重算）：%s", report["refused"])
        return report
    except Exception as e:
        log.warning("skillhub 自动同步失败：%s", e)
        return {"auto": True, "error": str(e)}


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _iter_skill_rows(cat: dict, project_root):
    """list 用的对账行：逐一读源（成本低，技能数少）。"""
    src_root = skills_source_dir(project_root)
    for s in cat.get("skills", []):
        name = s["name"]
        src = src_root / name
        exists = (src / "SKILL.md").is_file()
        match = False
        if exists:
            current = hash_dir(src)
            catalog_files = s.get("files") or {}
            if not _hash_diff(current, catalog_files):
                match = True
        yield {"name": name, "status": s.get("status"), "version": s.get("version"),
               "exists": exists, "match": match,
               "files": len(s.get("files") or {})}


def _cmd_list(args, project_root) -> int:
    cat = load_catalog(project_root)
    if not cat.get("skills") and not cat.get("mcp_servers"):
        print("（catalog 尚不存在或为空。先 python -m agent_core.skillhub add "
              "<name> 登记技能）")
        return 0
    want = set(s.strip() for s in (args.status or "").split(",") if s.strip())
    rows = [r for r in _iter_skill_rows(cat, project_root)
            if not want or r["status"] in want]
    if rows:
        print(f"{'技能':<16}{'状态':<14}{'版本':<10}{'源':<6}{'hash':<6}文件数")
        for r in rows:
            src_mark = "OK" if r["exists"] else "NO"
            hash_mark = "OK" if (r["exists"] and r["match"]) else \
                        ("NG" if r["exists"] else "-")
            print(f"{r['name']:<16}{r['status']:<14}{r['version']:<10}"
                  f"{src_mark:<6}{hash_mark:<6}{r['files']}")
        bad = [r["name"] for r in rows if r["exists"] and not r["match"]]
        if bad:
            print(f"警告：{', '.join(bad)} 源 hash 与 catalog 不符（先 update 重算）")
    else:
        print("（无匹配的技能）")
    mcp_servers = cat.get("mcp_servers") or []
    if mcp_servers:
        print("\nMCP 服务器（{}）：".format(len(mcp_servers)))
        for m in mcp_servers:
            env_vars = ",".join(ev["name"] + ("*" if ev.get("required") else "")
                                for ev in m.get("env_vars") or [])
            print(f"  {m.get('name')}  [{m.get('kind')}, {m.get('status')}]"
                  f"  需要: {env_vars or '无'}")
    return 0


def _cmd_add(args, project_root) -> int:
    entry = add_skill(args.name, project_root)
    print(f"已登记 {entry['name']}（status=experimental，{len(entry['files'])} 个文件）。"
          f"检验通过后执行 verify --mark-verified 置为 verified")
    return 0


def _cmd_verify(args, project_root) -> int:
    report = verify_skill(args.name, mark_verified=args.mark_verified,
                          project_root=project_root)
    if report["match"]:
        print(f"{report['name']}：完整性通过（{report['files']} 个文件）")
        if report["marked"]:
            print("已置位 status=verified [OK]")
        else:
            print(f"当前 status={report['status']}（未变更，可用 --mark-verified 置位）")
        return 0
    print(f"{report['name']}：完整性不通过。漂移文件：")
    for m in report["mismatches"]:
        print("  FAIL " + m)
    print("（源被改过？先 python -m agent_core.skillhub update 重算后重新评审）")
    return 1


def _cmd_update(args, project_root) -> int:
    entry = update_skill(args.name, project_root)
    print(f"{entry['name']}：已重算 hash（{len(entry['files'])} 个文件），"
          f"status 回到 {entry['status']}（内容已变，需重新评审）")
    return 0


def _cmd_install(args, project_root) -> int:
    try:
        report = install(
            target=args.target, names=args.name, dry_run=args.dry_run,
            include_experimental=args.include_experimental,
            with_mcp=args.with_mcp, project_root=project_root)
    except ValueError as e:
        print(f"错误：{e}")
        return 2
    mode = "（dry-run，未写盘）" if report["dry_run"] else ""
    print(f"安装报告{mode}（target={args.target}）")
    if report["installed"]:
        print("  已安装:", ", ".join(report["installed"]))
    if report["skipped"]:
        for name, why in report["skipped"].items():
            print(f"  跳过 {name}：{why}")
    if report["refused"]:
        for name, whys in report["refused"].items():
            print(f"  拒绝 {name}：{'；'.join(whys)}")
    if report["mcp"]["written"]:
        print(f"  MCP 配置已生成（{report['mcp']['path'] or 'dry-run 未写'}"
              f"，{len(report.get('warnings', []))} 条告警）")
    for w in report.get("warnings", []):
        print(f"  MCP 告警：{w}")
    return 1 if report["refused"] else 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(
        prog="python -m agent_core.skillhub",
        description="统一技能 / MCP 仓库：catalog + 完整性校验 + 安装到新节点。")
    sub = p.add_subparsers(dest="cmd", required=True)

    for name, helper, args in [
        ("list", "列 catalog（技能 × 源存在 × hash 对账）与 MCP 服务器",
         [("-s", "--status", "只列出指定状态（逗号分隔）")]),
        ("add", "扫描 workspace/agent/skills/<name>/ 入 catalog（experimental）",
         [("name", None, "技能名")]),
        ("verify", "重算源 hash 对账 catalog；--mark-verified 置位",
         [("name", None, "技能名"),
          ("-m", "--mark-verified", "完整性通过后置 status=verified", "flag")]),
        ("update", "技能改动后重算 hash 刷新 catalog（回实验态）",
         [("name", None, "技能名")]),
        ("install", "校验后把 verified 技能装进目标节点",
         [("--target", None, "目标节点根目录（含 workspace/ 的目录）"),
          ("--name", None, "只装指定技能（逗号分隔，缺省全部）"),
          ("--include-experimental", None, "放行 experimental（硬门 hash 不豁免）", "flag"),
          ("--with-mcp", None, "额外生成 skillhub_runtime/mcp.json", "flag"),
          ("--dry-run", None, "只报告不写盘", "flag")]),
    ]:
        sp = sub.add_parser(name, help=helper)
        for short, long, desc, *flag in args:
            if flag and flag[0] == "flag":
                if long:
                    sp.add_argument(short, long, action="store_true", help=desc)
                else:
                    sp.add_argument(short, action="store_true", help=desc)
            elif short == "name":
                sp.add_argument("name", help=desc)
            elif long:
                sp.add_argument(short, long, help=desc)
            else:
                sp.add_argument(short, help=desc)

    pargs = p.parse_args(argv)
    # CLI 在仓库根跑：project_root 锚定模块所在仓库根，不依赖 CWD
    root = PROJECT_ROOT
    return {
        "list": _cmd_list, "add": _cmd_add, "verify": _cmd_verify,
        "update": _cmd_update, "install": _cmd_install,
    }[pargs.cmd](pargs, root)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    sys.exit(main())