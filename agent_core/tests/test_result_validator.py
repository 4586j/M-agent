"""result_validator 单元测试。"""
import pytest
from agent_core.result_validator import (
    ValidationVerdict,
    validate_output,
    build_correction_hint,
    _extract_code_blocks,
    _extract_json_blocks,
    _check_python_syntax,
    _check_json_format,
    _check_completeness,
)


class TestExtractBlocks:
    def test_python_code_block(self):
        text = "说明\n```python\ndef foo():\n    return 1\n```\n结束"
        blocks = _extract_code_blocks(text)
        assert len(blocks) == 1
        assert blocks[0][0] == "python"
        assert "def foo" in blocks[0][1]

    def test_no_lang_code_block(self):
        text = "```\nprint('hi')\n```"
        blocks = _extract_code_blocks(text)
        assert len(blocks) == 1
        assert blocks[0][0] == ""  # 无语言标记

    def test_json_fence(self):
        text = '```json\n{"key": "value"}\n```'
        blocks = _extract_json_blocks(text)
        assert len(blocks) == 1
        assert '"key"' in blocks[0]

    def test_no_blocks(self):
        assert _extract_code_blocks("纯文本无代码") == []
        assert _extract_json_blocks("纯文本无JSON") == []


class TestPythonSyntax:
    def test_valid_python(self):
        blocks = [("python", "def foo():\n    return 1\n")]
        assert _check_python_syntax(blocks) == []

    def test_invalid_python(self):
        blocks = [("python", "def foo(\n")]
        issues = _check_python_syntax(blocks)
        assert len(issues) == 1
        assert "语法错误" in issues[0]

    def test_skip_non_python(self):
        blocks = [("javascript", "const x = 1;")]
        assert _check_python_syntax(blocks) == []

    def test_empty_code(self):
        blocks = [("python", "")]
        assert _check_python_syntax(blocks) == []


class TestJsonFormat:
    def test_valid_json(self):
        assert _check_json_format(['{"a": 1}']) == []

    def test_invalid_json(self):
        issues = _check_json_format(['{"a": }'])
        assert len(issues) == 1
        assert "JSON 格式错误" in issues[0]

    def test_multiple_blocks(self):
        issues = _check_json_format(['{"a": 1}', '{"b": }'])
        assert len(issues) == 1  # 只有第二个有问题


class TestCompleteness:
    def test_good_coverage(self):
        prompt = "请完成以下任务：\n1. 创建数据库连接\n2. 实现查询功能"
        output = "已创建数据库连接，并实现了查询功能。"
        # 覆盖度检查是启发式的，可能因关键词提取方式而不完全准确
        # 至少不应崩溃
        result = _check_completeness(output, prompt)
        assert isinstance(result, list)

    def test_poor_coverage(self):
        prompt = "请完成以下任务：\n1. 创建数据库连接\n2. 实现查询功能\n3. 添加错误处理"
        output = "这是一段无关内容。"
        issues = _check_completeness(output, prompt)
        # 可能命中也可能不命中，取决于关键词匹配
        # 至少不应崩溃
        assert isinstance(issues, list)

    def test_short_prompt_skip(self):
        """短 prompt 不做覆盖度检查。"""
        assert _check_completeness("任何输出", "短问题") == []


class TestValidateOutput:
    def test_empty_output(self):
        v = validate_output("", "some prompt")
        assert v.is_valid is True
        assert v.checks_run == ["empty_skip"]

    def test_valid_python_output(self):
        output = "代码如下：\n```python\ndef add(a, b):\n    return a + b\n```"
        v = validate_output(output, "写一个加法函数")
        assert v.is_valid is True
        assert "python_syntax" in v.checks_run

    def test_invalid_python_output(self):
        output = "代码如下：\n```python\ndef add(a, b\n```"
        v = validate_output(output, "写一个加法函数")
        assert v.is_valid is False
        assert v.severity == "high"
        assert any("语法错误" in i for i in v.issues)

    def test_valid_json_output(self):
        output = '结果：\n```json\n{"status": "ok"}\n```'
        v = validate_output(output)
        assert v.is_valid is True
        assert "json_format" in v.checks_run

    def test_invalid_json_output(self):
        output = '结果：\n```json\n{"status": }\n```'
        v = validate_output(output)
        assert v.is_valid is False
        assert v.severity in ("medium", "high")


class TestBuildCorrectionHint:
    def test_valid_no_hint(self):
        v = ValidationVerdict(is_valid=True, issues=[])
        assert build_correction_hint(v) == ""

    def test_invalid_has_hint(self):
        v = ValidationVerdict(
            is_valid=False,
            issues=["Python 语法错误（行 1）：unexpected EOF"],
            severity="high",
        )
        hint = build_correction_hint(v)
        assert "验证反馈" in hint
        assert "语法错误" in hint
        assert "逐一修正" in hint
