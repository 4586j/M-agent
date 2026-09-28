"""测试 agent_core.errors：错误人性化翻译各分类。

只做纯文本特征断言，不触网、不依赖 SDK。
"""
from agent_core.errors import (
    format_error_line,
    humanize_sdk_error,
    humanize_timeout,
)


def _classify_of(e) -> str:
    etype, _, _ = humanize_sdk_error(e)
    return etype


# ---- 各分类 ----

def test_classify_sdk_missing_by_text():
    e = ModuleNotFoundError("No module named 'claude_agent_sdk'")
    etype, message, hint = humanize_sdk_error(e)
    assert etype == "sdk_missing"
    assert "claude-agent-sdk" in message
    assert "pip install" in hint


def test_classify_sdk_missing_by_type():
    """类型命中但缺失的不是 claude 系模块：归 unknown 并如实展示模块名，
    不能谎报"未装 SDK"。"""
    etype, message, _ = humanize_sdk_error(
        ModuleNotFoundError("No module named 'xyz'"))
    assert etype == "unknown"
    assert "xyz" in message          # unknown 附带原始文本，模块名可见


def test_classify_sdk_missing_by_type_claude():
    """类型 + 缺失模块名含 claude（非标准文本形态）仍归 sdk_missing。"""
    assert _classify_of(ModuleNotFoundError("claude_agent_sdk 未安装")) == "sdk_missing"


def test_classify_auth():
    for text in ("Authentication error: 401 Unauthorized",
                 "invalid x-api-key",
                 "API key expired"):
        etype, message, hint = humanize_sdk_error(Exception(text))
        assert etype == "auth", text
        assert "鉴权" in message
        assert "doctor" in hint


def test_classify_endpoint():
    for text in ("Connection refused", "getaddrinfo failed",
                 "Name or service not known", "HTTP 404 Not Found"):
        etype, _, _ = humanize_sdk_error(Exception(text))
        assert etype == "endpoint", text


def test_classify_model_not_found():
    etype, message, hint = humanize_sdk_error(Exception("model not found: sonnet-x"))
    assert etype == "model_not_found"
    assert "模型" in message
    assert "模型名" in hint


def test_classify_model_not_found_precedes_404():
    """模型 404 语义优先于一般端点不通。"""
    assert _classify_of(Exception("model not_found_error (404)")) == "model_not_found"


def test_classify_model_not_found_unknown_model_only():
    """"unknown model" 命中模型不存在；裸 "unknown" 不再误判。"""
    assert _classify_of(Exception("unknown model id: sonnet-x")) == "model_not_found"
    assert _classify_of(Exception("unknown variable foo")) == "unknown"


def test_classify_rate_limit():
    """429 / rate limit 归 rate_limit（自动故障切换「有池才切」的触发信号）。"""
    for text in ("429 Too Many Requests", "rate limit exceeded",
                 "error 429: request_rate_limit_exceeded", "too many requests"):
        assert _classify_of(Exception(text)) == "rate_limit", text


def test_classify_rate_limit_precedes_budget():
    """rate limit 判定置于 budget 之前：限流语义与预算不足不同（限流可切池重试）。"""
    assert _classify_of(Exception("429 usage limit reached")) == "rate_limit"


def test_classify_budget():
    for text in ("max_budget_usd exceeded", "Your credit balance is too low",
                 "billing error", "usage limit reached"):
        etype, message, hint = humanize_sdk_error(Exception(text))
        assert etype == "budget", text
        assert "预算" in message or "余额" in message


def test_classify_budget_quota_error_codes():
    """配额/资金类错误码仍归 budget（insufficient 收窄后的合法形态）。"""
    for text in ("Error 402: insufficient_quota", "insufficient_funds"):
        assert _classify_of(Exception(text)) == "budget", text


def test_classify_auth_403_forbidden():
    """403/forbidden 也归鉴权类（auth 特征扩展）。"""
    for text in ("403 Forbidden", "permission_denied: forbidden"):
        assert _classify_of(Exception(text)) == "auth", text


def test_classify_auth_precedes_budget():
    """"insufficient permission"(403) 是鉴权问题：auth 判定提前，
    不再被 insufficient 关键词抢先误判为 budget。"""
    assert _classify_of(Exception("Error 403: insufficient permission")) == "auth"


def test_classify_timeout():
    etype, message, hint = humanize_sdk_error(TimeoutError("timed out"))
    assert etype == "timeout"
    assert "超时" in message
    assert "AGENT_TASK_TIMEOUT" in hint


def test_classify_timeout_by_type_only():
    """无文本的 TimeoutError 也按超时分类。"""
    assert _classify_of(TimeoutError()) == "timeout"


def test_classify_unknown_includes_raw_text():
    etype, message, hint = humanize_sdk_error(RuntimeError("boom detail"))
    assert etype == "unknown"
    assert "boom detail" in message          # 未知类附带原始文本便于排查
    assert hint


# ---- 优先级 ----

def test_classify_priority_model_before_auth():
    """含 401 与 model not found 的复合文本按更具体的模型错误归类。"""
    # 顺序敏感：model_not_found 判定在 auth 之前（模型错误常带 404/401 混杂）
    etype, _, _ = humanize_sdk_error(Exception("model not found (authentication 401)"))
    assert etype == "model_not_found"


# ---- 健壮性 ----

def test_humanize_never_raises():
    """任何输入都有兜底返回，绝不再抛异常。"""
    assert humanize_sdk_error(Exception(""))[0] == "unknown"
    assert humanize_sdk_error(None)[0] == "unknown"      # type: ignore[arg-type]
    assert humanize_sdk_error(42)[0] == "unknown"        # type: ignore[arg-type]


def test_unknown_message_truncated():
    long = "x" * 5000
    _, message, _ = humanize_sdk_error(Exception(long))
    assert len(message) < 400  # 原始文本截断到 200 字符左右


# ---- humanize_timeout / format_error_line ----

def test_humanize_timeout_text():
    message, hint = humanize_timeout(900)
    assert "900" in message
    assert "超时" in message
    assert hint


def test_format_error_line():
    line = format_error_line(TimeoutError("timed out"))
    assert "超时" in line
    assert "建议：" in line
