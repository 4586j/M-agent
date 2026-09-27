"""工具响应验证器：对工具调用返回结果做基础质量检查。

验证策略按工具类型自动选择：
- 搜索类（search_experiences）：结果非空、格式合规
- 读取类（read_core_memory）：内容非空、编码合法
- 写入类（write_core_memory/write_task_context）：状态确认
- 通用 MCP 工具：响应结构合规（content 字段存在）

设计原则：保守验证，宁可漏判不误拦——只捕获明确的结构性异常，
不做主观质量评判。验证失败时附加 warning 而非阻断（避免影响正常流程）。
"""
import json
import logging
from dataclasses import dataclass, field

log = logging.getLogger(__name__)


@dataclass
class ToolResponseVerdict:
    """工具响应验证结果。

    is_valid: 响应是否通过验证（无明确异常）。
    warnings: 发现的问题列表（空 = 无问题）。
    checks_run: 实际执行的检查名列表。
    """

    is_valid: bool = True
    warnings: list[str] = field(default_factory=list)
    checks_run: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "is_valid": self.is_valid,
            "warnings": self.warnings,
            "checks_run": self.checks_run,
        }


def _check_mcp_structure(result: dict) -> list[str]:
    """检查 MCP 响应基本结构。"""
    issues = []
    if not isinstance(result, dict):
        issues.append(f"响应类型错误：期望 dict，实际 {type(result).__name__}")
        return issues
    # MCP 标准格式：{"content": [...]} 或 {"is_error": bool}
    if "content" not in result and "is_error" not in result:
        issues.append("响应缺少 content 和 is_error 字段")
    if "content" in result:
        content = result["content"]
        if not isinstance(content, list):
            issues.append(f"content 字段类型错误：期望 list，实际 {type(content).__name__}")
        elif len(content) == 0:
            issues.append("content 列表为空")
        else:
            for i, block in enumerate(content):
                if not isinstance(block, dict):
                    issues.append(f"content[{i}] 类型错误：期望 dict")
                    break
                if "type" not in block:
                    issues.append(f"content[{i}] 缺少 type 字段")
                    break
    return issues


def _check_search_response(tool_name: str, result: dict) -> list[str]:
    """检查搜索类工具响应。"""
    issues = []
    # 提取实际结果内容
    content = result.get("content", [])
    if not content:
        return issues  # 空结果由结构检查处理
    first = content[0]
    if not isinstance(first, dict):
        return issues
    text = first.get("text", "")
    if not text or text.strip() == "":
        issues.append("搜索结果为空文本")
        return issues
    # 尝试解析 JSON 数组（search_experiences 返回 JSON 序列化的列表）
    try:
        parsed = json.loads(text)
        if isinstance(parsed, list) and len(parsed) == 0:
            issues.append("搜索返回空列表（无匹配结果）")
    except (json.JSONDecodeError, TypeError):
        pass  # 非 JSON 文本不报错（可能是纯文本摘要）
    return issues


def _check_read_response(tool_name: str, result: dict) -> list[str]:
    """检查读取类工具响应。"""
    issues = []
    content = result.get("content", [])
    if not content:
        return issues
    first = content[0]
    if not isinstance(first, dict):
        return issues
    text = first.get("text", "")
    if not text or text.strip() == "":
        issues.append("读取结果为空内容")
        return issues
    # 尝试解析 JSON（read_core_memory 返回 {"name": ..., "content": ...}）
    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict):
            if "content" in parsed and not parsed["content"]:
                issues.append("读取的记忆内容为空")
    except (json.JSONDecodeError, TypeError):
        pass  # 非 JSON 不报错
    return issues


def _check_write_response(tool_name: str, result: dict) -> list[str]:
    """检查写入类工具响应。"""
    issues = []
    content = result.get("content", [])
    if not content:
        return issues
    first = content[0]
    if not isinstance(first, dict):
        return issues
    text = first.get("text", "")
    # 写入成功通常返回 {"status": "written", ...}
    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict):
            status = parsed.get("status", "")
            if status and status not in ("written", "ok", "success", "created", "updated"):
                issues.append(f"写入状态异常：{status}")
    except (json.JSONDecodeError, TypeError):
        pass  # 非 JSON 不报错
    return issues


# 工具名到验证函数的映射
_TOOL_VALIDATORS = {
    "search_experiences": _check_search_response,
    "search": _check_search_response,
    "read_core_memory": _check_read_response,
    "read": _check_read_response,
    "write_core_memory": _check_write_response,
    "write_task_context": _check_write_response,
    "write": _check_write_response,
}


def validate_tool_response(tool_name: str, result: dict) -> ToolResponseVerdict:
    """对工具响应执行验证。

    tool_name: 工具名称（用于选择验证策略）。
    result: MCP 格式的响应 dict（{"content": [...], "is_error": bool}）。

    验证策略：
    1. 始终检查 MCP 响应结构合规性
    2. 按工具名选择特定验证器（搜索/读取/写入）
    3. 任何检查异常不阻断（异常隔离）
    """
    verdict = ToolResponseVerdict()

    if not isinstance(result, dict):
        verdict.is_valid = False
        verdict.warnings.append(f"响应类型错误：期望 dict，实际 {type(result).__name__}")
        return verdict

    # 错误响应不验证（调用方已处理）
    if result.get("is_error"):
        verdict.checks_run.append("error_skip")
        return verdict

    # 1. MCP 结构检查
    try:
        struct_issues = _check_mcp_structure(result)
        if struct_issues:
            verdict.warnings.extend(struct_issues)
            verdict.is_valid = False
        verdict.checks_run.append("mcp_structure")
    except Exception:  # noqa: BLE001
        log.warning("MCP 结构检查异常，跳过", exc_info=True)

    # 2. 工具特定检查
    validator = None
    for key, fn in _TOOL_VALIDATORS.items():
        if key in tool_name.lower():
            validator = fn
            break

    if validator is not None:
        try:
            specific_issues = validator(tool_name, result)
            if specific_issues:
                verdict.warnings.extend(specific_issues)
                # 空结果不算严重错误，只记 warning
            verdict.checks_run.append(f"tool_specific:{tool_name}")
        except Exception:  # noqa: BLE001
            log.warning("工具 %s 响应检查异常，跳过", tool_name, exc_info=True)

    return verdict
