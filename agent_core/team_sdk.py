"""Explicit, process-isolated AgentKernel bridge for team planning and execution.

No personal sessions, memory, MCP discovery, environment credentials or fallback
providers are inherited. This is file-tool confinement, not an OS sandbox.
"""
from __future__ import annotations

import json
import math
import os
import re
import signal
import subprocess
import sys
import threading
import uuid
from dataclasses import asdict
from decimal import ROUND_CEILING, Decimal
from pathlib import Path

from agent_core.kernel import AgentKernel, KernelResult

UNITS_PER_USD = 1_000_000
FILE_TOOLS = frozenset({"Read", "Write", "Edit", "Glob", "Grep"})
MAX_RESPONSE_BYTES = 12 * 1024 * 1024


def cost_units(cost) -> int | None:
    if type(cost) not in (int, float) or not math.isfinite(cost) or cost < 0:
        return None
    return int((Decimal(str(cost)) * UNITS_PER_USD).to_integral_value(rounding=ROUND_CEILING))


class TeamConfig:
    """Node-local references, never credentials or paths supplied by a model."""

    def __init__(self, path: Path | str):
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        if not isinstance(data, dict) or data.get("version") != 1:
            raise ValueError("Invalid team SDK configuration")
        self.providers = data.get("providers", {})
        self.workspaces = data.get("workspaces", {})
        self.personas = data.get("personas", {})
        self.planner = data.get("planner", {})
        self.execution = data.get("execution", {})
        if any(not isinstance(v, dict) for v in (self.providers, self.workspaces,
                                                self.personas, self.planner, self.execution)):
            raise ValueError("Invalid team SDK references")
        self.limits(self.planner)
        self.limits(self.execution)
        for provider in self.providers.values():
            if (not isinstance(provider, dict) or not isinstance(provider.get("model"), str)
                    or not 1 <= len(provider["model"]) <= 512
                    or not re.fullmatch(r"[A-Z][A-Z0-9_]{0,63}", provider.get("auth_token_env", ""))):
                raise ValueError("Providers require a model and a credential environment name")
            from urllib.parse import urlsplit
            url = urlsplit(provider.get("base_url", ""))
            if (url.scheme != "https" or not url.hostname or url.username or url.password or url.query or url.fragment
                    or url.port is not None and not 1 <= url.port <= 65535):
                raise ValueError("Providers require a bare HTTPS API URL")

    def provider(self, ref: str) -> dict:
        provider = self.providers.get(ref)
        if provider is None or not os.environ.get(provider["auth_token_env"]):
            raise ValueError("Provider reference or credential unavailable")
        return provider

    @staticmethod
    def limits(values: dict) -> tuple[float, int, int]:
        cost = values.get("max_cost_usd", 0.1)
        turns, timeout = values.get("max_turns", 16), values.get("timeout_seconds", 180)
        if (type(cost) not in (int, float) or not math.isfinite(cost) or not 0 < cost <= 100
                or type(turns) is not int or not 1 <= turns <= 100
                or type(timeout) is not int or not 5 <= timeout <= 3600):
            raise ValueError("Invalid team SDK execution limits")
        return float(cost), turns, timeout


def tool_decision(tool: str, inputs: dict, root: Path, allowed: set[str]) -> bool:
    """Only bounded file tools; input references are read-only, including aliases."""
    if tool not in allowed or tool not in FILE_TOOLS or not isinstance(inputs, dict):
        return False
    field = "file_path" if tool in ("Read", "Write", "Edit") else "path"
    raw = inputs.get(field, "outputs" if tool in ("Glob", "Grep") else "")
    if not isinstance(raw, str) or not raw or "\x00" in raw:
        return False
    path = Path(raw)
    path = (root / path).resolve() if not path.is_absolute() else path.resolve()
    if not path.is_relative_to(root):
        return False
    relative = path.relative_to(root)
    if (not relative.parts or relative.parts[0] not in ("inputs", "outputs")
            or any(part.startswith(".") for part in relative.parts)):
        return False
    if tool in ("Write", "Edit") and (not relative.parts or relative.parts[0] != "outputs"):
        return False
    pattern = inputs.get("pattern", "")
    if tool == "Glob" and (not isinstance(pattern, str) or Path(pattern).is_absolute()
                           or ".." in pattern.replace("\\", "/").split("/")):
        return False
    return True


class TeamKernel(AgentKernel):
    def _cached_scan_skills(self):
        return []

    def _cached_system_prompt(self, persona, skills, query=""):
        return persona.system_prompt

    def _cached_memory_mcp_server(self):
        return None

    def _cached_disk_mcp_servers(self):
        return {}

    def _build_options(self, persona, cwd, resume_session_id=None, query=""):
        import claude_agent_sdk as sdk
        options = super()._build_options(persona, cwd, query=query)
        allowed = set(persona.tools or ())
        if not allowed.issubset(FILE_TOOLS):
            raise ValueError("Unsupported team tool")
        options.tools = sorted(allowed)  # allowed_tools alone is only auto-approval.
        options.allowed_tools = sorted(allowed)
        options.mcp_servers = {}
        options.agents = None
        options.resume = None
        root = Path(cwd).resolve()

        async def guard(data, tool_use_id, context):
            permitted = tool_decision(data.get("tool_name"), data.get("tool_input"), root, allowed)
            return {"hookSpecificOutput": {"hookEventName": "PreToolUse",
                    "permissionDecision": "allow" if permitted else "deny",
                    "permissionDecisionReason": "Team file-tool and workspace policy"}}

        options.hooks = {"PreToolUse": [sdk.HookMatcher(hooks=[guard])]}
        return options


class IsolatedSdk:
    def __init__(self, config: TeamConfig):
        self.config = config

    def invoke(self, *, prompt: str, system_prompt: str, cwd: Path, tools: list[str],
               provider_ref: str, max_cost_usd: float, max_turns: int,
               timeout_seconds: int, cancel: threading.Event) -> KernelResult:
        if cancel.is_set():
            return KernelResult(is_error=True, cancelled=True, cost_usd=0)
        provider = self.config.provider(provider_ref)
        cwd = cwd.resolve()
        cwd.mkdir(parents=True, exist_ok=True)
        home = cwd / ".sdk-home"
        home.mkdir(exist_ok=True)
        # No inherited COLLAB_* tokens, personal provider credentials or Python hooks.
        environment = {key: os.environ[key] for key in
                       ("PATH", "SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT", "TEMP", "TMP")
                       if key in os.environ}
        environment.update({"PYTHONPATH": str(Path(__file__).resolve().parents[1]),
                            "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1",
                            "HOME": str(home), "USERPROFILE": str(home),
                            "APPDATA": str(home), "LOCALAPPDATA": str(home),
                            "ANTHROPIC_BASE_URL": provider["base_url"],
                            "ANTHROPIC_AUTH_TOKEN": os.environ[provider["auth_token_env"]]})
        cancellation = cwd / (".cancel-" + uuid.uuid4().hex)
        payload = {"prompt": prompt, "system_prompt": system_prompt, "cwd": str(cwd),
                   "tools": tools, "model": provider["model"], "max_cost_usd": max_cost_usd,
                   "max_turns": max_turns, "timeout_seconds": timeout_seconds,
                   "cancel_path": str(cancellation)}
        process = subprocess.Popen([sys.executable, "-m", "agent_core.team_sdk"],
                                   cwd=cwd, env=environment, stdin=subprocess.PIPE,
                                   stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                   start_new_session=os.name != "nt")
        import time
        started = time.monotonic()
        cancelling_since = None
        raw_input = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        while True:
            try:
                output, _ = process.communicate(input=raw_input, timeout=0.2)
                break
            except subprocess.TimeoutExpired:
                raw_input = None
                if cancel.is_set() or time.monotonic() - started > timeout_seconds + 5:
                    cancellation.touch(exist_ok=True)
                    if cancelling_since is None:
                        cancelling_since = time.monotonic()
                if cancelling_since is not None and time.monotonic() - cancelling_since > 15:
                    if os.name == "nt":
                        subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                       check=False)
                    else:
                        os.killpg(process.pid, signal.SIGKILL)
                    process.communicate(timeout=5)
                    return KernelResult(is_error=True, error_type="unconfirmed_exit")
        if process.returncode or len(output) > MAX_RESPONSE_BYTES:
            return KernelResult(is_error=True, error_type="team_sdk_process")
        try:
            return KernelResult(**json.loads(output))
        except (ValueError, TypeError):
            return KernelResult(is_error=True, error_type="team_sdk_response")


def _child():
    from agent_core.config import AgentConfig
    from agent_core.persona import Persona
    payload = json.loads(sys.stdin.buffer.read())
    cancel = threading.Event()
    def watch():
        while not cancel.wait(0.1):
            if Path(payload["cancel_path"]).exists():
                cancel.set()
    threading.Thread(target=watch, daemon=True).start()
    config = AgentConfig(
        model=payload["model"], project_root=payload["cwd"], work_dir=payload["cwd"],
        max_turns=payload["max_turns"], task_budget_usd=payload["max_cost_usd"],
        task_timeout=payload["timeout_seconds"], max_thinking_tokens=0,
        permission_mode="default", streaming=False, auto_record_experience=False,
        correction_enabled=False, output_guard_max_retries=0, subagents_enabled=False,
        experience_store_dir=str(Path(payload["cwd"]) / ".experiences"))
    result = TeamKernel(config).run_oneshot(
        payload["prompt"], Persona("team", tools=payload["tools"],
                                   system_prompt=payload["system_prompt"]), payload["cwd"],
        cancel_event=cancel, record_experience=False)
    sys.stdout.write(json.dumps(asdict(result), ensure_ascii=False))


if __name__ == "__main__":
    _child()
