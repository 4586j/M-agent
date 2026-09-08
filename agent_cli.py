#!/usr/bin/env python3
"""向后兼容薄 wrapper：真正的 CLI 逻辑已迁入包内模块 ``agent_core.cli``。

保留本文件仅为兼容既有习惯用法 ``python agent_cli.py ...``；其行为与迁移前
一致（同样委托 agent_core 内核）。推荐的新用法是编辑安装后直接用 ``agent``
命令（见 agent_core/pyproject.toml 的 [project.scripts]）：

    pip install -e agent_core/
    agent                     # 交互 REPL（tty 下启用富终端界面）
    agent "任务"              # headless 单发（纯文本）
    agent --help

本 wrapper 只做两件事：把仓库根加入 sys.path（未安装包时也能直接跑源码），
然后转调 ``agent_core.cli.main``。
"""
import sys
from pathlib import Path

# 仓库根（本文件所在目录）加入 sys.path，保证未 pip 安装时也能 import agent_core
_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from agent_core.cli import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
