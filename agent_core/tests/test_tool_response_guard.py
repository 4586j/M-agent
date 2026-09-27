"""测试 tool_response_guard.py 的工具响应验证。

覆盖：MCP 结构检查、搜索/读取/写入工具特定检查、
错误响应跳过、正常响应通过、异常隔离。
"""
import json

import pytest

from agent_core.tool_response_guard import (validate_tool_response,
                                             ToolResponseVerdict)


def _mcp_text(text: str) -> dict:
    """构造标准 MCP 文本响应。"""
    return {"content": [{"type": "text", "text": text}]}


def _mcp_json(obj) -> dict:
    """构造 JSON 序列化的 MCP 响应。"""
    return _mcp_text(json.dumps(obj, ensure_ascii=False))


# ---- ToolResponseVerdict ----

class TestToolResponseVerdict:
    def test_default_valid(self):
        v = ToolResponseVerdict()
        assert v.is_valid
        assert v.warnings == []

    def test_to_dict(self):
        v = ToolResponseVerdict(is_valid=False, warnings=["test"],
                                checks_run=["mcp_structure"])
        d = v.to_dict()
        assert not d["is_valid"]
        assert d["warnings"] == ["test"]


# ---- MCP 结构检查 ----

class TestMCPStructure:
    def test_valid_structure(self):
        result = _mcp_text("hello")
        v = validate_tool_response("some_tool", result)
        assert v.is_valid
        assert "mcp_structure" in v.checks_run

    def test_missing_content_and_error(self):
        result = {"something": "else"}
        v = validate_tool_response("some_tool", result)
        assert not v.is_valid
        assert any("content" in w for w in v.warnings)

    def test_content_not_list(self):
        result = {"content": "not a list"}
        v = validate_tool_response("some_tool", result)
        assert not v.is_valid

    def test_content_empty_list(self):
        result = {"content": []}
        v = validate_tool_response("some_tool", result)
        assert not v.is_valid

    def test_content_block_not_dict(self):
        result = {"content": ["not a dict"]}
        v = validate_tool_response("some_tool", result)
        assert not v.is_valid

    def test_content_block_missing_type(self):
        result = {"content": [{"text": "hello"}]}
        v = validate_tool_response("some_tool", result)
        assert not v.is_valid

    def test_non_dict_result(self):
        v = validate_tool_response("some_tool", "not a dict")
        assert not v.is_valid


# ---- 搜索类工具 ----

class TestSearchResponse:
    def test_valid_search_results(self):
        results = [{"prompt": "task1", "output_summary": "result1"}]
        result = _mcp_json(results)
        v = validate_tool_response("search_experiences", result)
        assert v.is_valid

    def test_empty_search_results(self):
        result = _mcp_json([])
        v = validate_tool_response("search_experiences", result)
        assert v.warnings  # 有警告但不阻断
        assert any("空列表" in w for w in v.warnings)

    def test_empty_text_search(self):
        result = _mcp_text("")
        v = validate_tool_response("search_experiences", result)
        assert v.warnings
        assert any("空文本" in w for w in v.warnings)


# ---- 读取类工具 ----

class TestReadResponse:
    def test_valid_read_result(self):
        data = {"name": "test", "content": "some memory content"}
        result = _mcp_json(data)
        v = validate_tool_response("read_core_memory", result)
        assert v.is_valid

    def test_empty_content_read(self):
        data = {"name": "test", "content": ""}
        result = _mcp_json(data)
        v = validate_tool_response("read_core_memory", result)
        assert v.warnings  # 有警告但不阻断
        assert any("内容为空" in w for w in v.warnings)

    def test_empty_text_read(self):
        result = _mcp_text("")
        v = validate_tool_response("read_core_memory", result)
        assert v.warnings
        assert any("空内容" in w for w in v.warnings)


# ---- 写入类工具 ----

class TestWriteResponse:
    def test_valid_write_result(self):
        data = {"status": "written", "name": "test"}
        result = _mcp_json(data)
        v = validate_tool_response("write_core_memory", result)
        assert v.is_valid

    def test_abnormal_write_status(self):
        data = {"status": "failed", "name": "test"}
        result = _mcp_json(data)
        v = validate_tool_response("write_core_memory", result)
        assert v.warnings  # 有警告但不阻断
        assert any("状态异常" in w for w in v.warnings)

    def test_ok_write_status(self):
        data = {"status": "ok", "task_id": "t1"}
        result = _mcp_json(data)
        v = validate_tool_response("write_task_context", result)
        assert v.is_valid


# ---- 错误响应跳过 ----

class TestErrorResponse:
    def test_error_response_skipped(self):
        result = {"is_error": True, "content": [{"type": "text", "text": "error"}]}
        v = validate_tool_response("some_tool", result)
        assert v.is_valid  # 错误响应不验证
        assert "error_skip" in v.checks_run


# ---- 通用工具 ----

class TestGenericTool:
    def test_valid_generic_response(self):
        result = _mcp_text("some output")
        v = validate_tool_response("bash", result)
        assert v.is_valid

    def test_generic_no_specific_validator(self):
        result = _mcp_text("output")
        v = validate_tool_response("unknown_tool", result)
        assert v.is_valid
        # 没有特定验证器，只跑结构检查
        assert not any("tool_specific" in c for c in v.checks_run)
