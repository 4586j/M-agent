"""错误人性化：把 SDK/内核抛出的底层异常翻译成人话。

统一出口 ``humanize_sdk_error(e) -> (error_type, message, hint)``：
- error_type：错误分类标识（sdk_missing / auth / endpoint / model_not_found /
  budget / timeout / unknown），供上层精确映射（HTTP code、CLI 着色等）；
- message：中文一句话原因（unknown 类附带原始错误文本，便于排查）；
- hint：一条建议动作（中文）。

设计约束：纯文本特征匹配（异常类型名 + str(e) 小写化），不 import SDK、
不做网络请求，任何输入都要有兜底返回（unknown），绝不再抛异常。
"""

def _classify(text: str, tname: str) -> str:
    """按异常文本特征分类。顺序敏感：先具体后一般。"""
    low = (text or "").lower()

    # 未安装 SDK：ModuleNotFoundError 文本特征
    if "no module named" in low and "claude" in low:
        return "sdk_missing"
    if tname == "ModuleNotFoundError":
        # 类型命中但缺失的不是 claude 系模块（如业务依赖 xyz）：
        # 不能谎报"未装 SDK"，归 unknown 并如实展示原始模块名
        if "claude" in low:
            return "sdk_missing"
        return "unknown"

    # 模型不存在（先于一般 404/端点判断：模型错误的 404 语义不同）。
    # unknown 收窄为 "unknown model"：裸 "unknown" 过宽，会把
    # "unknown variable foo" 之类无关错误误判成模型不存在
    if ("model" in low
            and ("not found" in low or "does not exist" in low
                 or "not_found" in low or "unknown model" in low)):
        return "model_not_found"
    if "not_found_error" in low:
        return "model_not_found"

    # 限流（429 / rate limit / too many requests）——判定提前到 budget 类之前：
    # 限流是**可换 provider 重试**的信号（内核自动故障切换「有池才切」），
    # 与预算熔断（不可换、只能调额度）语义不同，必须分开归类。
    if ("429" in low or "rate limit" in low or "too many requests" in low
            or "ratelimit" in low or "request_limit_exceeded" in low):
        return "rate_limit"

    # 鉴权失败（401/403 / unauthorized / forbidden / invalid api key）——
    # 判定提前到预算类之前：避免 "insufficient permission"(403) 等
    # 鉴权表述被余额关键词抢先误判为 budget
    if ("401" in low or "403" in low or "unauthorized" in low
            or "forbidden" in low or "authentication" in low
            or "invalid api key" in low or "invalid x-api-key" in low
            or "api key" in low and ("invalid" in low or "expired" in low)):
        return "auth"

    # 预算熔断 / 余额类（insufficient 收窄为配额/资金错误码，
    # 避免吞掉 "insufficient permission" 之类鉴权错误）
    if ("budget" in low or "max_budget_usd" in low
            or "insufficient_quota" in low or "insufficient_funds" in low
            or "credit" in low or "balance" in low
            or "billing" in low or "usage limit" in low):
        return "budget"

    # 超时（先于连接类判断：连接超时也按超时提示重试）
    if ("timed out" in low or "timeout" in low
            or tname == "TimeoutError"
            or "请求超时" in low or "任务超时" in low):
        return "timeout"

    # 端点不通 / 连接失败
    if ("connection" in low or "connect" in low and ("refused" in low or "failed" in low)
            or "econnrefused" in low or "connectionerror" in low
            or "getaddrinfo" in low or "name or service not known" in low
            or "unreachable" in low or "404" in low or "404" in text):
        return "endpoint"

    return "unknown"


# 各分类的中文一句话原因与建议动作
_TEMPLATES: dict[str, tuple[str, str]] = {
    "sdk_missing": (
        "未安装 claude-agent-sdk，内核无法驱动底层 Claude Code CLI。",
        "在当前 Python 环境执行 pip install claude-agent-sdk 后重试。",
    ),
    "rate_limit": (
        "触发限流（429，请求频率超额度，或账户并发/每分钟配额用尽）。",
        "稍后重试，或配置 provider_pool 让内核自动切到备用 provider（有池才切）。",
    ),
    "auth": (
        "鉴权失败（API Key 缺失、无效或已过期，服务端返回 401）。",
        "检查 ANTHROPIC_API_KEY 或 ANTHROPIC_AUTH_TOKEN + ANTHROPIC_BASE_URL 配置是否匹配，可用 agent --doctor 自检。",
    ),
    "endpoint": (
        "后端端点不可达（连接被拒、域名解析失败或路径不存在）。",
        "确认 ANTHROPIC_BASE_URL 指向可用服务且网络可达，可用 agent --doctor 自检。",
    ),
    "model_not_found": (
        "模型名不存在或无权访问（服务端返回 not found）。",
        "核对 ANTHROPIC_MODEL / --model 与端点支持的模型名是否一致。",
    ),
    "budget": (
        "触发预算/余额限制（任务预算熔断或账户额度不足）。",
        "调高 AGENT_TASK_BUDGET_USD 或检查账户余额后重试。",
    ),
    "timeout": (
        "请求超时（墙钟超时或后端长时间无响应）。",
        "调大 AGENT_TASK_TIMEOUT 重试，或检查网络与端点负载。",
    ),
}


def humanize_sdk_error(e: BaseException) -> tuple[str, str, str]:
    """把异常翻译为 (error_type, message, hint)。

    未知类型归入 unknown：message 附带原始错误文本（截断），hint 指向
    doctor 自检与日志。永不抛异常。
    """
    try:
        text = str(e) or ""
        tname = type(e).__name__
        error_type = _classify(text, tname)
        if error_type == "unknown":
            excerpt = " ".join(text.split())[:200]
            return ("unknown",
                    f"发生未识别的错误：{excerpt}" if excerpt else "发生未识别的错误（无错误信息）。",
                    "查看完整日志/堆栈定位原因，或运行 agent --doctor 自检环境。")
        message, hint = _TEMPLATES[error_type]
        return (error_type, message, hint)
    except Exception:  # noqa: BLE001 - 兜底：人性化失败也不能抛
        return ("unknown", "发生未识别的错误。", "查看完整日志定位原因。")


def humanize_timeout(seconds: int | float) -> tuple[str, str]:
    """墙钟超时（内核 asyncio.wait_for 触发）的统一文案。"""
    return (f"任务超时（>{seconds}s）",
            "调大 AGENT_TASK_TIMEOUT 重试，或检查网络与端点负载。")


def format_error_line(e: BaseException) -> str:
    """渲染成单行「原因 + 建议」文本，供 CLI/日志直接打印。"""
    _, message, hint = humanize_sdk_error(e)
    return f"{message} 建议：{hint}"
