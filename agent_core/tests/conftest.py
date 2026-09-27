"""pytest 共享夹具与路径设置。

确保无论以何种方式调用 pytest，项目根都在 sys.path 上，使
``import agent_core.*`` 稳定可用（``python -m pytest`` 会自动加 cwd，
但直接 ``pytest agent_core/tests/`` 时不保证，这里显式兜底）。
"""
import os
import sys
from pathlib import Path

import pytest

# agent_core/tests/conftest.py 的上三级即项目根
PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# 不存在的哨兵 .env 路径：dotenv 相关测试用它阻断真实 .env 回读
_MISSING_ENV = Path(__file__).with_name(".env.pytest-missing")


@pytest.fixture(autouse=True)
def _block_dotenv_leak():
    """统一阻断 .env 泄漏并净化环境，保证用例确定性（Slice 4：deterministic suites pass）。

    两处污染源：
    1) load_dotenv 未设 AGENT_ENV_FILE 时会回读项目根真实 .env（开发机常存
       ANTHROPIC_API_KEY / AGENT_MODEL 等）；
    2) 某些模块在导入期即产生副作用——如 agent_core.service 模块级
       ``app = create_app(...)`` 会调 load_agent_config→load_dotenv，而导入发生在
       pytest collection 阶段（早于任何 fixture），会把开发机 .env 的 AGENT_MODEL
       等直接写进 os.environ，污染随后所有用例（表现为顺序相关的路标失败）。

    因此在每个用例 setup 时：快照 os.environ → 清除 AGENT_/ORCH_ 前缀变量
    （抹去导入期泄漏）→ 把 AGENT_ENV_FILE 指向不存在的哨兵；teardown 时精确还原。
    不依赖 monkeypatch：load_dotenv 直写 os.environ，monkeypatch 不追踪这些写入，
    故用显式快照/还原才能彻底隔离。
    """
    saved = dict(os.environ)
    for key in list(os.environ.keys()):
        if key.startswith("AGENT_") or key.startswith("ORCH_"):
            del os.environ[key]
    os.environ["AGENT_ENV_FILE"] = str(_MISSING_ENV)
    try:
        yield
    finally:
        os.environ.clear()
        os.environ.update(saved)


@pytest.fixture
def project_root() -> str:
    """真实项目根路径（字符串），供需要扫描真实资产的测试使用。"""
    return str(PROJECT_ROOT)


@pytest.fixture
def persona_dir() -> str:
    """真实 .claude/agents/ 目录，供人设加载测试使用。"""
    return str(PROJECT_ROOT / ".claude" / "agents")
