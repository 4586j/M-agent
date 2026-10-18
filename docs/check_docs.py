"""Validate maintained documentation without network, models, or third-party packages."""
from __future__ import annotations

import datetime as dt
import re
import sys
from pathlib import Path
from urllib.parse import unquote, urlsplit

ROOT = Path(__file__).resolve().parents[1]
MAINTAINED = (
    "README.md", "AGENTS.md", "CHANGELOG.md", "docs/README.md",
    "docs/项目介绍.md", "docs/系统架构.md", "docs/开发计划.md",
    "docs/问题修复与解决方法.md", "docs/开发与文档维护规范.md",
    "docs/团队真实规划与执行接通_2026-10-18.md",
    "docs/待机动画方案与接入边界_2026-10-18.md",
    "docs/templates/修复记录模板.md", "docs/templates/架构决策模板.md",
    "docs/templates/开发任务模板.md", ".github/pull_request_template.md",
)
LINK = re.compile(r"!?\[[^\]\n]*\]\(\s*(<[^>]+>|[^\s)]+)(?:\s+\"[^\"]*\")?\s*\)")
DATE = re.compile(r"最后核对：(\d{4}-\d{2}-\d{2})")


def markdown_body(text: str, errors: list[str], name: str) -> str:
    """Omit fenced examples so placeholder links are not treated as real navigation."""
    lines, fence = [], None
    for number, line in enumerate(text.splitlines(), 1):
        marker = re.match(r"^\s{0,3}(`{3,}|~{3,})", line)
        if marker:
            token = marker.group(1)
            if fence is None:
                fence = token
            elif token[0] == fence[0] and len(token) >= len(fence):
                fence = None
            lines.append("")
        elif fence is None:
            lines.append(line)
        else:
            lines.append("")
        if number == 1 and not line.startswith("# "):
            errors.append(f"{name}: first line must be an H1 title")
    if fence is not None:
        errors.append(f"{name}: unclosed fenced code block")
    return "\n".join(lines)


def validate(root: Path = ROOT, names=MAINTAINED) -> tuple[list[str], int]:
    errors, links = [], 0
    root = root.resolve()
    for name in names:
        source = root / name
        if not source.is_file():
            errors.append(f"{name}: missing maintained document")
            continue
        try:
            text = source.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            errors.append(f"{name}: cannot read as UTF-8")
            continue
        if not text.strip():
            errors.append(f"{name}: empty document")
            continue
        if name not in ("AGENTS.md", ".github/pull_request_template.md"):
            match = DATE.search(text)
            try:
                if match is None:
                    raise ValueError("missing date")
                dt.date.fromisoformat(match.group(1))
            except ValueError:
                errors.append(f"{name}: missing or invalid last-reviewed date")
            if "文档类型：" not in text:
                errors.append(f"{name}: missing document type")
        if name == ".github/pull_request_template.md":
            # GitHub templates conventionally start with H2 sections.
            body = text
        else:
            body = markdown_body(text, errors, name)
        for match in LINK.finditer(body):
            target = match.group(1).strip("<>")
            parsed = urlsplit(target)
            if parsed.scheme or parsed.netloc or not parsed.path:
                continue
            path_text = unquote(parsed.path)
            if path_text.startswith("/"):
                errors.append(f"{name}: use portable relative link: {target}")
                continue
            resolved = (source.parent / path_text).resolve()
            try:
                resolved.relative_to(root)
            except ValueError:
                errors.append(f"{name}: link leaves repository: {target}")
                continue
            links += 1
            if not resolved.exists():
                errors.append(f"{name}: missing local target: {target}")
    return errors, links


def main() -> int:
    errors, links = validate()
    if errors:
        for error in errors:
            print(error, file=sys.stderr)
        print(f"Documentation check failed: {len(errors)} issue(s).", file=sys.stderr)
        return 1
    print(f"Documentation check passed: {len(MAINTAINED)} maintained files, {links} local links.")
    print("Scope: file structure, metadata, fences and local link targets; not factual or runtime validation.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
