"""文件上下文提取单元测试。"""
import json
import tempfile
from pathlib import Path

import pytest

from agent_core.file_context import (
    build_context_with_files,
    extract_text_from_file,
    save_upload_to_temp,
)


@pytest.fixture
def tmp_dir(tmp_path):
    return tmp_path


class TestExtractTextFromFile:
    def test_extract_txt(self, tmp_dir):
        f = tmp_dir / "test.txt"
        f.write_text("Hello, world!", encoding="utf-8")
        result = extract_text_from_file(str(f))
        assert result == "Hello, world!"

    def test_extract_csv(self, tmp_dir):
        f = tmp_dir / "data.csv"
        f.write_text("name,age\nAlice,30\nBob,25\n", encoding="utf-8")
        result = extract_text_from_file(str(f))
        assert "CSV 文件" in result
        assert "name" in result
        assert "age" in result
        assert "Alice" in result

    def test_extract_json(self, tmp_dir):
        f = tmp_dir / "data.json"
        f.write_text('{"key": "value", "num": 42}', encoding="utf-8")
        result = extract_text_from_file(str(f))
        assert "JSON 文件" in result
        assert "key" in result
        assert "value" in result

    def test_extract_json_invalid(self, tmp_dir):
        f = tmp_dir / "bad.json"
        f.write_text("not valid json", encoding="utf-8")
        result = extract_text_from_file(str(f))
        assert "非合法 JSON" in result

    def test_extract_markdown(self, tmp_dir):
        f = tmp_dir / "doc.md"
        f.write_text("# Title\n\nContent here.", encoding="utf-8")
        result = extract_text_from_file(str(f))
        assert "# Title" in result

    def test_extract_missing_file(self):
        with pytest.raises(FileNotFoundError):
            extract_text_from_file("/nonexistent/file.txt")

    def test_extract_pdf_without_pypdf(self, tmp_dir):
        f = tmp_dir / "doc.pdf"
        f.write_bytes(b"%PDF-1.4 fake content")
        result = extract_text_from_file(str(f))
        # 如果没有 pypdf，应返回提示信息
        assert "PDF" in result

    def test_extract_image(self, tmp_dir):
        f = tmp_dir / "photo.png"
        f.write_bytes(b"\x89PNG fake image data")
        result = extract_text_from_file(str(f))
        assert "图片文件" in result

    def test_extract_unknown_type_as_text(self, tmp_dir):
        f = tmp_dir / "data.xyz"
        f.write_text("some content", encoding="utf-8")
        result = extract_text_from_file(str(f))
        assert result == "some content"


class TestBuildContextWithFiles:
    def test_no_files(self):
        result = build_context_with_files("original prompt", [])
        assert result == "original prompt"

    def test_single_file(self, tmp_dir):
        f = tmp_dir / "test.txt"
        f.write_text("file content", encoding="utf-8")
        result = build_context_with_files("prompt", [str(f)])
        assert "prompt" in result
        assert "附件内容" in result
        assert "file content" in result

    def test_multiple_files(self, tmp_dir):
        f1 = tmp_dir / "a.txt"
        f2 = tmp_dir / "b.txt"
        f1.write_text("content A", encoding="utf-8")
        f2.write_text("content B", encoding="utf-8")
        result = build_context_with_files("prompt", [str(f1), str(f2)])
        assert "content A" in result
        assert "content B" in result

    def test_file_extraction_failure(self, tmp_dir):
        result = build_context_with_files("prompt", ["/nonexistent/file.txt"])
        assert "提取失败" in result


class TestSaveUploadToTemp:
    def test_save_to_temp(self, tmp_dir):
        path = save_upload_to_temp(b"file content", "test.txt", str(tmp_dir))
        assert Path(path).is_file()
        assert Path(path).read_bytes() == b"file content"
        assert Path(path).name == "test.txt"

    def test_save_creates_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            temp_dir = Path(tmp) / "subdir"
            path = save_upload_to_temp(b"data", "file.txt", str(temp_dir))
            assert Path(path).is_file()
