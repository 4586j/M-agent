"""任务自适应路由器：根据 prompt 特征自动选择执行策略。

分类规则（纯规则，不依赖 LLM）：
- simple → direct（直接 oneshot，跳过规划）
- moderate → oneshot_with_verify（oneshot + 结果验证）
- complex → plan_execute（Plan-Execute 引擎）

设计原则：保守路由，宁可多规划也不漏规划。
"""
import logging
import re
from dataclasses import dataclass

log = logging.getLogger(__name__)


@dataclass
class TaskProfile:
    """任务分类结果。"""

    complexity: str   # "simple" | "moderate" | "complex"
    strategy: str     # "direct" | "oneshot_with_verify" | "plan_execute"
    recommended_persona: str | None = None
    confidence: str = "medium"  # "low" | "medium" | "high"
    reason: str = ""


# 多步骤关键词模式（中文 + 英文）
_MULTI_STEP_PATTERNS = [
    re.compile(r"步骤\s*\d"),
    re.compile(r"首先.*然后.*最后", re.DOTALL),
    re.compile(r"第一步.*第二步", re.DOTALL),
    re.compile(r"step\s*\d", re.IGNORECASE),
    re.compile(r"first.*then.*finally", re.IGNORECASE | re.DOTALL),
    re.compile(r"1\.\s*.+\n\s*2\.\s*.+\n\s*3\.", re.DOTALL),
]

# 工具调用关键词（暗示需要工具操作，非简单问答）
_TOOL_KEYWORDS = [
    "创建文件", "写入文件", "修改代码", "运行测试", "执行命令",
    "部署", "安装", "配置", "生成报告", "分析数据",
    "create file", "write file", "run test", "execute",
    "deploy", "install", "configure", "generate report",
]

# 简单问答模式（暗示不需要规划）
_SIMPLE_PATTERNS = [
    re.compile(r"^(什么是|解释|说明|告诉我|what is|explain|describe)\s*.+[?？]?$",
               re.IGNORECASE),
    re.compile(r"^(帮我|请|请帮)\s*(翻译|转换|计算)\s*.+$", re.IGNORECASE),
]


def classify_task(prompt: str, persona_name: str = "") -> TaskProfile:
    """根据 prompt 特征分类任务复杂度并推荐执行策略。

    分类逻辑：
    1. 短 prompt（< 200 字）+ 匹配简单问答模式 → simple
    2. 匹配多步骤模式 或 prompt > 1000 字 → complex
    3. 其余 → moderate
    """
    prompt_stripped = prompt.strip()
    prompt_len = len(prompt_stripped)

    # 规则 1: 多步骤模式 → complex
    for pat in _MULTI_STEP_PATTERNS:
        if pat.search(prompt_stripped):
            return TaskProfile(
                complexity="complex",
                strategy="plan_execute",
                confidence="high",
                reason="检测到多步骤结构",
            )

    # 规则 2: 长 prompt（> 1000 字）→ complex
    if prompt_len > 1000:
        return TaskProfile(
            complexity="complex",
            strategy="plan_execute",
            confidence="medium",
            reason=f"prompt 较长（{prompt_len} 字），可能包含多步需求",
        )

    # 规则 3: 短 prompt + 简单问答模式 → simple
    if prompt_len < 200:
        for pat in _SIMPLE_PATTERNS:
            if pat.match(prompt_stripped):
                return TaskProfile(
                    complexity="simple",
                    strategy="direct",
                    confidence="high",
                    reason="短 prompt + 简单问答模式",
                )

    # 规则 4: 含工具调用关键词 → moderate（需要验证但不需要规划）
    has_tool_keywords = any(kw in prompt_stripped.lower()
                           for kw in _TOOL_KEYWORDS)
    if has_tool_keywords and prompt_len < 500:
        return TaskProfile(
            complexity="moderate",
            strategy="oneshot_with_verify",
            confidence="medium",
            reason="含工具调用关键词，建议执行后验证",
        )

    # 默认: moderate
    return TaskProfile(
        complexity="moderate",
        strategy="oneshot_with_verify",
        confidence="low",
        reason="默认分类为中等复杂度",
    )


def should_use_planning(profile: TaskProfile, planning_enabled: bool) -> bool:
    """根据路由结果决定是否启用规划。

    - simple: 强制跳过规划
    - complex: 强制启用规划
    - moderate: 按 planning_enabled 配置决定
    """
    if profile.complexity == "simple":
        return False
    if profile.complexity == "complex":
        return True
    return planning_enabled


def should_use_correction(profile: TaskProfile) -> bool:
    """根据路由结果决定是否启用结果验证/纠错。

    - simple: 不需要（简单问答无需验证）
    - moderate/complex: 启用
    """
    return profile.complexity != "simple"
