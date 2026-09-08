"""pytest 共享夹具与路径设置。

确保无论以何种方式调用 pytest，项目根都在 sys.path 上，使
``import agent_core.*`` 稳定可用（``python -m pytest`` 会自动加 cwd，
但直接 ``pytest agent_core/tests/`` 时不保证，这里显式兜底）。
"""
import sys
from pathlib import Path

import pytest

# agent_core/tests/conftest.py 的上三级即项目根
PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


@pytest.fixture
def project_root() -> str:
    """真实项目根路径（字符串），供需要扫描真实资产的测试使用。"""
    return str(PROJECT_ROOT)


@pytest.fixture
def persona_dir() -> str:
    """真实 .claude/agents/ 目录，供人设加载测试使用。"""
    return str(PROJECT_ROOT / ".claude" / "agents")
