"""测试 agent_core.config：默认值、环境变量覆盖、默认路径定位。"""
import os
from pathlib import Path

import pytest

from agent_core.config import (AgentConfig, _to_bool, _to_float,
                               _to_optional_float, _to_optional_int,
                               load_agent_config, load_dotenv)


# 一个保证不存在的 .env 路径：令 load_dotenv 在测试中空跑，隔离项目根真实 .env
_MISSING_ENV = str(Path(__file__).with_name(".env.pytest-missing"))


@pytest.fixture(autouse=True)
def _restore_environ():
    """全量快照并还原 os.environ。

    load_dotenv 会直接写入 os.environ（monkeypatch 不追踪这些写入），
    本 fixture 保证它们不污染后续用例。
    """
    saved = dict(os.environ)
    yield
    os.environ.clear()
    os.environ.update(saved)


def _clear_agent_env(monkeypatch):
    """清掉所有 AGENT_ / ORCH_ 前缀环境变量，确保默认值断言不受宿主环境干扰。

    同时把 AGENT_ENV_FILE 指向一个不存在的文件，令 load_agent_config 内的
    load_dotenv 空跑——否则项目根真实 .env（用户自建的密钥文件）会渗入测试。
    """
    for key in list(os.environ.keys()):
        if key.startswith("AGENT_") or key.startswith("ORCH_"):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("AGENT_ENV_FILE", _MISSING_ENV)


# ---- 默认值 ----

def test_load_agent_config_returns_agentconfig(monkeypatch):
    _clear_agent_env(monkeypatch)
    cfg = load_agent_config()
    assert isinstance(cfg, AgentConfig)


def test_default_values(monkeypatch):
    _clear_agent_env(monkeypatch)
    cfg = load_agent_config()
    # 模型与执行控制
    assert cfg.model is None
    assert cfg.max_turns == 50
    assert cfg.max_thinking_tokens == 10000
    assert cfg.task_budget_usd is None
    assert cfg.task_timeout == 900
    # 阶段一安全加固：默认权限收敛为 default（bypassPermissions 须显式设置）
    assert cfg.permission_mode == "default"
    # 记忆后端与大脑地址
    assert cfg.memory_backend == "local"
    assert cfg.brain_url == "http://127.0.0.1:7000"
    assert cfg.api_key == ""
    # 增强功能开关
    assert cfg.planning_enabled is False
    assert cfg.reflection_enabled is False
    assert cfg.rag_enabled is False
    assert cfg.streaming is True
    assert cfg.auto_record_experience is True
    # Plan-Execute 断点续跑：计划年龄上限默认 24h
    assert cfg.plan_resume_max_age_hours == 24.0
    # 输出质量守卫：默认开启、重试 1 次（保守化后降低成本放大）、不换模型
    assert cfg.output_guard_enabled is True
    assert cfg.output_guard_max_retries == 1
    assert cfg.fallback_model == ""
    # 子代理派发开关 + AI 推荐/解释顾问：默认值保持既有行为
    assert cfg.subagents_enabled is True
    assert cfg.advisor_enabled is True
    assert cfg.advisor_auto_apply is False
    assert cfg.advisor_model == ""
    assert cfg.advisor_work_dir == ""
    assert cfg.advisor_use_plan_engine is False
    # 结果翻译：默认关（须显式开启），目标语言默认中文
    assert cfg.result_translate_enabled is False
    assert cfg.translate_model == ""
    assert cfg.translate_target_lang == "zh"
    assert cfg.translate_work_dir == ""


# ---- 环境变量覆盖 ----

def test_env_override_model_and_turns(monkeypatch):
    _clear_agent_env(monkeypatch)
    monkeypatch.setenv("AGENT_MODEL", "opus")
    monkeypatch.setenv("AGENT_MAX_TURNS", "7")
    monkeypatch.setenv("AGENT_MAX_THINKING_TOKENS", "4096")
    monkeypatch.setenv("AGENT_TASK_TIMEOUT", "120")
    monkeypatch.setenv("AGENT_PERMISSION_MODE", "acceptEdits")
    cfg = load_agent_config()
    assert cfg.model == "opus"
    assert cfg.max_turns == 7
    assert cfg.max_thinking_tokens == 4096
    assert cfg.task_timeout == 120
    assert cfg.permission_mode == "acceptEdits"


def test_permission_mode_bypass_requires_explicit_env(monkeypatch):
    """默认 default；仅显式设置环境变量时才回到 bypassPermissions。"""
    _clear_agent_env(monkeypatch)
    monkeypatch.setenv("AGENT_PERMISSION_MODE", "bypassPermissions")
    cfg = load_agent_config()
    assert cfg.permission_mode == "bypassPermissions"


def test_env_override_orch_fallback(monkeypatch):
    """AGENT_ 未设置时回退到 ORCH_ 前缀。"""
    _clear_agent_env(monkeypatch)
    monkeypatch.setenv("ORCH_MODEL", "haiku")
    monkeypatch.setenv("ORCH_MAX_TURNS", "3")
    cfg = load_agent_config()
    assert cfg.model == "haiku"
    assert cfg.max_turns == 3


def test_agent_prefix_takes_priority_over_orch(monkeypatch):
    _clear_agent_env(monkeypatch)
    monkeypatch.setenv("AGENT_MODEL", "opus")
    monkeypatch.setenv("ORCH_MODEL", "haiku")
    cfg = load_agent_config()
    assert cfg.model == "opus"  # AGENT_ 优先


def test_env_override_bool_flags(monkeypatch):
    _clear_agent_env(monkeypatch)
    monkeypatch.setenv("AGENT_PLANNING_ENABLED", "true")
    monkeypatch.setenv("AGENT_REFLECTION_ENABLED", "1")
    monkeypatch.setenv("AGENT_RAG_ENABLED", "yes")
    monkeypatch.setenv("AGENT_STREAMING", "0")
    cfg = load_agent_config()
    assert cfg.planning_enabled is True
    assert cfg.reflection_enabled is True
    assert cfg.rag_enabled is True
    assert cfg.streaming is False


def test_env_override_subagents_and_advisor(monkeypatch):
    """子代理派发开关与顾问配置由 AGENT_/ORCH_ 前缀设值生效。"""
    _clear_agent_env(monkeypatch)
    monkeypatch.setenv("AGENT_SUBAGENTS_ENABLED", "0")
    monkeypatch.setenv("ORCH_ADVISOR_AUTO_APPLY", "true")
    monkeypatch.setenv("AGENT_ADVISOR_MODEL", "haiku")
    monkeypatch.setenv("AGENT_ADVISOR_ENABLED", "off")
    monkeypatch.setenv("ORCH_ADVISOR_USE_PLAN_ENGINE", "1")
    monkeypatch.setenv("AGENT_ADVISOR_WORK_DIR", "/tmp/adv")
    cfg = load_agent_config()
    assert cfg.subagents_enabled is False
    assert cfg.advisor_auto_apply is True
    assert cfg.advisor_model == "haiku"
    assert cfg.advisor_enabled is False
    assert cfg.advisor_use_plan_engine is True
    assert cfg.advisor_work_dir == "/tmp/adv"


def test_env_override_result_translate(monkeypatch):
    """结果翻译配置由 AGENT_/ORCH_ 前缀设值生效（默认关、目标语言中文）。"""
    _clear_agent_env(monkeypatch)
    monkeypatch.setenv("AGENT_RESULT_TRANSLATE_ENABLED", "1")
    monkeypatch.setenv("AGENT_TRANSLATE_MODEL", "haiku")
    monkeypatch.setenv("ORCH_TRANSLATE_TARGET_LANG", "en")
    monkeypatch.setenv("AGENT_TRANSLATE_WORK_DIR", "/tmp/tr")
    cfg = load_agent_config()
    assert cfg.result_translate_enabled is True
    assert cfg.translate_model == "haiku"
    assert cfg.translate_target_lang == "en"
    assert cfg.translate_work_dir == "/tmp/tr"


def test_env_override_output_guard(monkeypatch):
    """守卫三配置由 AGENT_ 前缀设值生效。"""
    _clear_agent_env(monkeypatch)
    monkeypatch.setenv("AGENT_OUTPUT_GUARD_ENABLED", "0")
    monkeypatch.setenv("AGENT_OUTPUT_GUARD_MAX_RETRIES", "2")
    monkeypatch.setenv("AGENT_FALLBACK_MODEL", "big-model")
    cfg = load_agent_config()
    assert cfg.output_guard_enabled is False
    assert cfg.output_guard_max_retries == 2
    assert cfg.fallback_model == "big-model"


def test_env_override_output_guard_orch_fallback(monkeypatch):
    """AGENT_ 未设置时守卫三配置回退 ORCH_ 前缀（worker 集群共用）。"""
    _clear_agent_env(monkeypatch)
    monkeypatch.setenv("ORCH_OUTPUT_GUARD_ENABLED", "false")
    monkeypatch.setenv("ORCH_OUTPUT_GUARD_MAX_RETRIES", "3")
    monkeypatch.setenv("ORCH_FALLBACK_MODEL", "fb-model")
    cfg = load_agent_config()
    assert cfg.output_guard_enabled is False
    assert cfg.output_guard_max_retries == 3
    assert cfg.fallback_model == "fb-model"


def test_env_override_plan_resume_max_age_hours(monkeypatch):
    """计划年龄上限可由环境变量覆盖；AGENT_ 优先、ORCH_ 兜底、非法回退默认。"""
    _clear_agent_env(monkeypatch)
    monkeypatch.setenv("AGENT_PLAN_RESUME_MAX_AGE_HOURS", "48")
    assert load_agent_config().plan_resume_max_age_hours == 48.0
    monkeypatch.setenv("AGENT_PLAN_RESUME_MAX_AGE_HOURS", "")
    monkeypatch.setenv("ORCH_PLAN_RESUME_MAX_AGE_HOURS", "7.5")
    assert load_agent_config().plan_resume_max_age_hours == 7.5
    monkeypatch.setenv("ORCH_PLAN_RESUME_MAX_AGE_HOURS", "abc")
    assert load_agent_config().plan_resume_max_age_hours == 24.0


def test_env_override_budget_and_backend(monkeypatch):
    _clear_agent_env(monkeypatch)
    monkeypatch.setenv("AGENT_TASK_BUDGET_USD", "0.5")
    monkeypatch.setenv("AGENT_MEMORY_BACKEND", "remote")
    monkeypatch.setenv("AGENT_BRAIN_URL", "http://10.0.0.1:9000")
    monkeypatch.setenv("AGENT_API_KEY", "secret-key")
    cfg = load_agent_config()
    assert cfg.task_budget_usd == 0.5
    assert cfg.memory_backend == "remote"
    assert cfg.brain_url == "http://10.0.0.1:9000"
    assert cfg.api_key == "secret-key"


def test_kwargs_overrides_win_over_env(monkeypatch):
    _clear_agent_env(monkeypatch)
    monkeypatch.setenv("AGENT_MODEL", "opus")
    cfg = load_agent_config(model="sonnet", max_turns=99)
    assert cfg.model == "sonnet"   # 显式 override 压过环境变量
    assert cfg.max_turns == 99


def test_kwargs_override_ignores_unknown_key(monkeypatch):
    _clear_agent_env(monkeypatch)
    cfg = load_agent_config(not_a_field="x")  # 未知键被忽略，不报错
    assert not hasattr(cfg, "not_a_field")


# ---- 默认路径定位 ----

def test_default_paths_locate_real_dirs(monkeypatch):
    _clear_agent_env(monkeypatch)
    cfg = load_agent_config()
    assert Path(cfg.project_root).is_dir()
    assert Path(cfg.persona_dir).is_dir()
    # persona_dir 应指向 .claude/agents
    assert Path(cfg.persona_dir).name == "agents"
    assert Path(cfg.persona_dir).parent.name == ".claude"
    # work_dir 基于项目根定位
    assert Path(cfg.work_dir).is_absolute()


def test_default_memory_dirs_under_project_root(monkeypatch):
    _clear_agent_env(monkeypatch)
    cfg = load_agent_config()
    root = Path(cfg.project_root).resolve()
    assert Path(cfg.memory_core_dir).is_absolute()
    assert root in Path(cfg.memory_core_dir).parents or Path(cfg.memory_core_dir) == root
    assert Path(cfg.experience_store_dir).is_absolute()


def test_env_override_paths(monkeypatch, tmp_path):
    _clear_agent_env(monkeypatch)
    monkeypatch.setenv("AGENT_PROJECT_ROOT", str(tmp_path))
    monkeypatch.setenv("AGENT_WORK_DIR", str(tmp_path / "work"))
    monkeypatch.setenv("AGENT_PERSONA_DIR", str(tmp_path / "agents"))
    cfg = load_agent_config()
    assert cfg.project_root == str(tmp_path)
    assert cfg.work_dir == str(tmp_path / "work")
    assert cfg.persona_dir == str(tmp_path / "agents")


# ---- 内部解析辅助函数 ----

@pytest.mark.parametrize("raw,default,expected", [
    ("", False, False),
    ("", True, True),
    ("1", False, True),
    ("true", False, True),
    ("YES", False, True),
    ("on", False, True),
    ("0", True, False),
    ("off", True, False),
    ("garbage", True, False),
])
def test_to_bool(raw, default, expected):
    assert _to_bool(raw, default) is expected


@pytest.mark.parametrize("raw,expected", [
    ("", None),
    ("   ", None),
    ("0", None),        # 0 视为不限制
    ("10000", 10000),
    ("abc", None),
])
def test_to_optional_int(raw, expected):
    assert _to_optional_int(raw) == expected


@pytest.mark.parametrize("raw,expected", [
    ("", None),
    ("0", None),        # 0 视为不限制
    ("0.5", 0.5),
    ("abc", None),
])
def test_to_optional_float(raw, expected):
    assert _to_optional_float(raw) == expected


@pytest.mark.parametrize("raw,default,expected", [
    ("", 3.0, 3.0),
    ("   ", 3.0, 3.0),
    ("2.5", 3.0, 2.5),
    ("-1", 3.0, -1.0),   # 合法数字照实返回（<=0 的语义由消费方决定）
    ("abc", 3.0, 3.0),
])
def test_to_float(raw, default, expected):
    assert _to_float(raw, default) == expected


# ---- .env 文件加载（load_dotenv）----

def test_load_dotenv_sets_vars_from_file(monkeypatch, tmp_path):
    # 清除环境已有同名键（开发机 repo .env 在导入时可能已注入），
    # 否则 override=False 不会覆盖，断言会随机器环境漂移。
    monkeypatch.delenv("AGENT_MODEL", raising=False)
    monkeypatch.delenv("AGENT_MAX_TURNS", raising=False)
    env = tmp_path / ".env"
    env.write_text("# 注释行\n\nAGENT_MODEL=sonnet\nAGENT_MAX_TURNS=7\n",
                   encoding="utf-8")
    loaded = load_dotenv(path=env)
    assert loaded == str(env)
    assert os.environ["AGENT_MODEL"] == "sonnet"
    assert os.environ["AGENT_MAX_TURNS"] == "7"


def test_load_dotenv_does_not_override_existing(monkeypatch, tmp_path):
    """默认真实环境变量优先，文件值不覆盖已存在的键。"""
    monkeypatch.setenv("AGENT_MODEL", "opus")
    env = tmp_path / ".env"
    env.write_text("AGENT_MODEL=sonnet\n", encoding="utf-8")
    load_dotenv(path=env)
    assert os.environ["AGENT_MODEL"] == "opus"


def test_load_dotenv_override_true_replaces(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_MODEL", "opus")
    env = tmp_path / ".env"
    env.write_text("AGENT_MODEL=sonnet\n", encoding="utf-8")
    load_dotenv(path=env, override=True)
    assert os.environ["AGENT_MODEL"] == "sonnet"


def test_load_dotenv_strips_quotes_and_export(monkeypatch, tmp_path):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("AGENT_MODEL", raising=False)
    monkeypatch.delenv("AGENT_PERSONA", raising=False)
    env = tmp_path / ".env"
    env.write_text(
        'export ANTHROPIC_API_KEY="sk-ant-x"\n'
        "AGENT_MODEL='sonnet'\n"
        "AGENT_PERSONA = executor \n",   # 键值两侧空白
        encoding="utf-8")
    load_dotenv(path=env)
    assert os.environ["ANTHROPIC_API_KEY"] == "sk-ant-x"
    assert os.environ["AGENT_MODEL"] == "sonnet"
    assert os.environ["AGENT_PERSONA"] == "executor"


def test_load_dotenv_ignores_malformed_lines(monkeypatch, tmp_path):
    monkeypatch.delenv("AGENT_MODEL", raising=False)
    env = tmp_path / ".env"
    env.write_text("无等号行\n=只值无键\nAGENT_MODEL=sonnet\n", encoding="utf-8")
    load_dotenv(path=env)
    assert os.environ["AGENT_MODEL"] == "sonnet"
    assert "无等号行" not in os.environ  # 畸形行未写入任何键


def test_load_dotenv_missing_file_returns_none(tmp_path):
    assert load_dotenv(path=tmp_path / "nope.env") is None


def test_load_dotenv_via_agent_env_file(monkeypatch, tmp_path):
    """不传 path 时走 AGENT_ENV_FILE 指定的文件。"""
    env = tmp_path / "custom.env"
    env.write_text("AGENT_MODEL=haiku\n", encoding="utf-8")
    monkeypatch.setenv("AGENT_ENV_FILE", str(env))
    monkeypatch.delenv("AGENT_MODEL", raising=False)
    assert load_dotenv() == str(env)
    assert os.environ["AGENT_MODEL"] == "haiku"


def test_load_agent_config_auto_reads_dotenv(monkeypatch, tmp_path):
    """端到端：load_agent_config 自动加载 AGENT_ENV_FILE 指向的 .env。"""
    _clear_agent_env(monkeypatch)
    env = tmp_path / ".env"
    env.write_text("AGENT_MODEL=sonnet\nAGENT_MAX_TURNS=3\n"
                   "ANTHROPIC_API_KEY=sk-ant-e2e\n", encoding="utf-8")
    # 覆盖 _clear_agent_env 设的缺失路径，指向真实临时 .env
    monkeypatch.setenv("AGENT_ENV_FILE", str(env))
    cfg = load_agent_config()
    assert cfg.model == "sonnet"
    assert cfg.max_turns == 3
    # SDK 鉴权用的 key 也进了 os.environ（供 claude-agent-sdk 子进程继承）
    assert os.environ["ANTHROPIC_API_KEY"] == "sk-ant-e2e"


# ---- 持久化执行日志 rollout 开关（Slice 3）----

def test_durable_journal_defaults_off(monkeypatch):
    """默认关：durable_journal_enabled=False，journal_path 空，max_active=1。"""
    _clear_agent_env(monkeypatch)
    cfg = load_agent_config()
    assert cfg.durable_journal_enabled is False
    assert cfg.journal_path == ""
    assert cfg.max_active_executions == 1


def test_durable_journal_env_override(monkeypatch):
    _clear_agent_env(monkeypatch)
    monkeypatch.setenv("AGENT_DURABLE_JOURNAL_ENABLED", "true")
    monkeypatch.setenv("AGENT_JOURNAL_PATH", "/tmp/j.sqlite3")
    monkeypatch.setenv("AGENT_MAX_ACTIVE_EXECUTIONS", "4")
    cfg = load_agent_config()
    assert cfg.durable_journal_enabled is True
    assert cfg.journal_path == "/tmp/j.sqlite3"
    assert cfg.max_active_executions == 4


def test_durable_journal_orch_fallback(monkeypatch):
    """AGENT_ 未设时回退 ORCH_ 前缀（与其他字段一致）。"""
    _clear_agent_env(monkeypatch)
    monkeypatch.setenv("ORCH_DURABLE_JOURNAL_ENABLED", "1")
    monkeypatch.setenv("ORCH_MAX_ACTIVE_EXECUTIONS", "2")
    cfg = load_agent_config()
    assert cfg.durable_journal_enabled is True
    assert cfg.max_active_executions == 2
