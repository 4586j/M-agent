"""Select the terminal's actual shell and keep its output encoding explicit."""
import base64
import locale
import os
import shutil
from pathlib import Path


def terminal_shell() -> str:
    configured = os.environ.get("AGENT_TERMINAL_SHELL", "").strip().strip('"')
    if configured:
        return configured
    if os.name == "nt":
        return (shutil.which("pwsh.exe") or shutil.which("powershell.exe")
                or os.environ.get("COMSPEC", "cmd.exe"))
    return os.environ.get("SHELL") or "/bin/sh"


def terminal_command(shell: str, command: str) -> tuple[list[str] | str, str]:
    """Return argv and the encoding used by that shell's redirected output."""
    name = Path(shell).stem.lower()
    if name in {"pwsh", "powershell"}:
        # Encode the script, not the output: preserve Unicode and nested quotes in argv.
        # Invoke through -Command: Windows PowerShell serializes redirected errors as
        # CLIXML for -EncodedCommand, even with -OutputFormat Text.
        script = (
            "$OutputEncoding = [Console]::OutputEncoding = "
            "[System.Text.UTF8Encoding]::new($false); "
            "$LASTEXITCODE = 0;\n" + command + "\n"
            "if (-not $?) { if ($LASTEXITCODE) { exit $LASTEXITCODE }; exit 1 }"
        )
        encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
        invoke = ("& ([scriptblock]::Create([Text.Encoding]::Unicode.GetString("
                  f"[Convert]::FromBase64String('{encoded}'))))")
        return [shell, "-NoLogo", "-NoProfile", "-NonInteractive", "-OutputFormat", "Text",
                "-Command", invoke], "utf-8"
    if name == "cmd":
        # Hidden cmd processes emit their OEM code page even when chcp is invoked with
        # redirected handles. Decode those bytes as OEM instead of guessing UTF-8.
        import ctypes
        encoding = f"cp{ctypes.windll.kernel32.GetOEMCP()}"
        # cmd has its own quoting rules; list2cmdline would escape embedded quotes
        # with backslashes, which cmd treats as literal characters.
        return f'"{shell}" /d /s /c "{command}"', encoding
    return [shell, "-c", command], locale.getpreferredencoding(False)
