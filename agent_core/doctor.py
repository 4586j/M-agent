"""doctor 自检：只读环境检查，启动前给出「通过/警告/失败」报告。

检查项（全部只读，不联网、不写文件、不启动子进程）：
- claude_agent_sdk 可导入性；
- 鉴权配置：ANTHROPIC_API_KEY（官方/第三方均支持）或
  ANTHROPIC_AUTH_TOKEN + ANTHROPIC_BASE_URL（第三方中转姿势）二选一；
- ``tools.load_mcp_servers`` 解析结果：对被过滤/损坏的 MCP 条目逐条告警；
- persona 目录扫描：列出可用人设与当前默认人设是否存在；
- .env 文件是否存在。

用法：
- CLI：``agent --doctor``（装配 config 后、进 REPL 前执行，打印报告即退出）；
- service：``main()`` 启动时跑一遍并 log 告警，/health 响应附 checks 汇总。
"""
import importlib
import os
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from agent_core.persona import list_persona_names
from agent_core.tools import load_mcp_servers

# 检查结论三种状态
STATUS_OK = "ok"
STATUS_WARN = "warn"
STATUS_FAIL = "fail"

# 状态 → 中文标记（报告逐项前缀）
_MARKS = {STATUS_OK: "[通过]", STATUS_WARN: "[警告]", STATUS_FAIL: "[失败]"}


@dataclass
class CheckResult:
    """单项检查结论。status ∈ ok | warn | fail；detail 为人类可读说明。"""

    name: str
    status: str
    detail: str

    def to_dict(self) -> dict:
        return asdict(self)


def _import_sdk():
    """SDK 导入探测点（独立函数便于测试 monkeypatch 模拟未安装）。"""
    return importlib.import_module("claude_agent_sdk")


def check_sdk() -> CheckResult:
    """检查 claude_agent_sdk 是否可导入。"""
    try:
        _import_sdk()
    except Exception as e:  # noqa: BLE001 - 任何导入失败都按缺失处理
        return CheckResult(
            "sdk", STATUS_FAIL,
            f"claude-agent-sdk 不可用：{e}。执行 pip install claude-agent-sdk 安装。")
    return CheckResult("sdk", STATUS_OK, "claude-agent-sdk 可导入")


def check_auth(_config=None) -> CheckResult:
    """检查鉴权配置：API Key 或 AUTH_TOKEN+BASE_URL 二选一。

    - ANTHROPIC_API_KEY：官方直连或带 BASE_URL 的第三方中转均视为通过；
    - ANTHROPIC_AUTH_TOKEN + ANTHROPIC_BASE_URL：第三方中转姿势，缺 BASE_URL 告警；
    - 两者皆无：失败并给出缺失项提示。
    """
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    auth_token = os.environ.get("ANTHROPIC_AUTH_TOKEN", "")
    base_url = os.environ.get("ANTHROPIC_BASE_URL", "")

    if api_key:
        if base_url:
            return CheckResult(
                "auth", STATUS_OK,
                "第三方中转姿势：ANTHROPIC_API_KEY + ANTHROPIC_BASE_URL 已配置"
                f"（BASE_URL={base_url}）")
        return CheckResult("auth", STATUS_OK, "官方姿势：ANTHROPIC_API_KEY 已配置")
    if auth_token:
        if base_url:
            return CheckResult(
                "auth", STATUS_OK,
                f"第三方中转姿势：ANTHROPIC_AUTH_TOKEN + ANTHROPIC_BASE_URL 已配置"
                f"（BASE_URL={base_url}）")
        return CheckResult(
            "auth", STATUS_WARN,
            "已设 ANTHROPIC_AUTH_TOKEN 但缺 ANTHROPIC_BASE_URL：第三方中转"
            "通常两者必须同时配置，请补齐或改用 ANTHROPIC_API_KEY。")
    return CheckResult(
        "auth", STATUS_FAIL,
        "未检测到任何鉴权配置：需要 ANTHROPIC_API_KEY（官方/中转均可），"
        "或 ANTHROPIC_AUTH_TOKEN + ANTHROPIC_BASE_URL（第三方中转）。"
        "可在项目根 .env 中配置。")


def check_mcp(config) -> CheckResult:
    """检查 MCP 配置解析：逐条告警被过滤/损坏的条目。"""
    servers, warnings = load_mcp_servers(config.mcp_config_path,
                                         return_warnings=True)
    if not config.mcp_config_path:
        return CheckResult("mcp", STATUS_OK,
                           "未配置 MCP 配置文件（AGENT_MCP_CONFIG / .mcp.json），跳过")
    names = list(servers.keys()) if servers else []
    if warnings and not servers:
        return CheckResult("mcp", STATUS_FAIL,
                           f"MCP 配置不可用（{'；'.join(warnings)}）")
    if warnings:
        return CheckResult("mcp", STATUS_WARN,
                           f"已加载 {len(names)} 个 MCP 服务（{', '.join(names) or '无'}）；"
                           f"告警：{'；'.join(warnings)}")
    return CheckResult("mcp", STATUS_OK,
                       f"已加载 {len(names)} 个 MCP 服务"
                       + (f"：{', '.join(names)}" if names else "（配置为空）"))


def check_personas(config, default_persona: str = "executor") -> CheckResult:
    """扫描 persona 目录：列出可用人设，核对默认人设是否有定义文件。"""
    available = list_persona_names(config.persona_dir)
    if not Path(config.persona_dir).is_dir():
        detail = (f"persona 目录不存在：{config.persona_dir}。"
                  "将以最小人设兜底（orchestrator 仍有内置大脑人设）。")
        return CheckResult("personas", STATUS_WARN, detail)
    if not available:
        return CheckResult("personas", STATUS_WARN,
                           f"persona 目录为空：{config.persona_dir}。"
                           "将以最小人设兜底（orchestrator 仍有内置大脑人设）。")
    if default_persona in available:
        return CheckResult(
            "personas", STATUS_OK,
            f"可用人设：{', '.join(available)}；当前默认：{default_persona}")
    return CheckResult(
        "personas", STATUS_WARN,
        f"可用人设：{', '.join(available)}；当前默认 '{default_persona}' 无定义"
        f"文件，将回退最小人设（目录：{config.persona_dir}）。")


def check_env_file(config) -> CheckResult:
    """检查项目根 .env 是否存在（不存在仅提示，不阻塞）。"""
    p = Path(config.project_root) / ".env" if config.project_root else None
    if p is None:
        return CheckResult("env_file", STATUS_WARN, "project_root 未配置，无法定位 .env")
    if p.is_file():
        return CheckResult("env_file", STATUS_OK, f"已找到 {p}")
    return CheckResult(
        "env_file", STATUS_WARN,
        f"未找到 {p}：环境变量需自行注入（AGENT_ENV_FILE 可指定其他路径）。")


def run_checks(config, default_persona: str = "executor") -> list[CheckResult]:
    """执行全部检查项，返回有序结论列表。任何单项异常不拖垮整体。"""
    checks: list[CheckResult] = []
    items: list[tuple[str, Any]] = [
        ("sdk", check_sdk),
        ("auth", lambda: check_auth(config)),
        ("mcp", lambda: check_mcp(config)),
        ("personas", lambda: check_personas(config, default_persona)),
        ("env_file", lambda: check_env_file(config)),
    ]
    for name, fn in items:
        try:
            checks.append(fn())
        except Exception as e:  # noqa: BLE001 - 单项检查崩溃降级为失败项（保留检查名）
            checks.append(CheckResult(name, STATUS_FAIL,
                                      f"检查项执行异常：{e}"))
    return checks


def summarize(checks: list[CheckResult]) -> dict:
    """汇总 checks 为可 JSON 化的 summary（/health 用）。"""
    counts = {STATUS_OK: 0, STATUS_WARN: 0, STATUS_FAIL: 0}
    for c in checks:
        counts[c.status] = counts.get(c.status, 0) + 1
    return {
        "total": len(checks),
        "ok": counts[STATUS_OK],
        "warn": counts[STATUS_WARN],
        "fail": counts[STATUS_FAIL],
        "items": [c.to_dict() for c in checks],
    }


def format_report(checks: list[CheckResult]) -> str:
    """渲染纯文本报告：逐项 [通过]/[警告]/[失败] + 说明 + 汇总行。"""
    lines = ["agent 环境自检（doctor）", "-" * 40]
    for c in checks:
        lines.append(f"{_MARKS.get(c.status, '[?]')} {c.name}：{c.detail}")
    total = len(checks)
    n_ok = sum(1 for c in checks if c.status == STATUS_OK)
    n_warn = sum(1 for c in checks if c.status == STATUS_WARN)
    n_fail = sum(1 for c in checks if c.status == STATUS_FAIL)
    lines.append("-" * 40)
    lines.append(f"汇总：{total} 项 | 通过 {n_ok} | 警告 {n_warn} | 失败 {n_fail}")
    return "\n".join(lines)


def report_text(config, default_persona: str = "executor") -> str:
    """便捷入口：跑检查并直接返回报告文本。"""
    return format_report(run_checks(config, default_persona))
