"""文件上下文提取（File Context）。

从上传的文件中提取文本内容，拼入 prompt 作为上下文。
支持 CSV、TXT、JSON、PDF（可选依赖 pypdf）。
"""
import csv
import io
import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


# 支持的文件类型
SUPPORTED_TEXT_TYPES = {".txt", ".md", ".json", ".csv", ".tsv", ".log", ".yaml", ".yml", ".xml"}
SUPPORTED_PDF_TYPES = {".pdf"}
SUPPORTED_IMAGE_TYPES = {".png", ".jpg", ".jpeg", ".gif", ".bmp"}


def extract_text_from_file(file_path: str | Path, file_type: str | None = None) -> str:
    """从文件中提取文本内容。

    Args:
        file_path: 文件路径
        file_type: 文件类型（扩展名，如 ".csv"）；None 则从路径推断

    Returns:
        提取的文本内容

    Raises:
        ValueError: 不支持的文件类型
        FileNotFoundError: 文件不存在
    """
    path = Path(file_path)
    if not path.is_file():
        raise FileNotFoundError(f"文件不存在: {file_path}")

    ext = file_type or path.suffix.lower()

    if ext in SUPPORTED_TEXT_TYPES:
        return _extract_text_file(path, ext)
    elif ext in SUPPORTED_PDF_TYPES:
        return _extract_pdf_file(path)
    elif ext in SUPPORTED_IMAGE_TYPES:
        return _describe_image_file(path)
    else:
        # 尝试当纯文本读取
        try:
            return path.read_text(encoding="utf-8", errors="replace")
        except OSError as e:
            raise ValueError(f"无法读取文件: {e}")


def _extract_text_file(path: Path, ext: str) -> str:
    """提取纯文本文件。"""
    if ext == ".csv":
        return _extract_csv(path)
    elif ext == ".tsv":
        return _extract_csv(path, delimiter="\t")
    elif ext == ".json":
        return _extract_json(path)
    else:
        return path.read_text(encoding="utf-8", errors="replace")


def _extract_csv(path: Path, delimiter: str = ",") -> str:
    """提取 CSV 文件，返回前 N 行预览 + 统计信息。"""
    try:
        with path.open("r", encoding="utf-8", errors="replace") as f:
            reader = csv.reader(f, delimiter=delimiter)
            rows = list(reader)
    except OSError as e:
        raise ValueError(f"读取 CSV 失败: {e}")

    if not rows:
        return "(空 CSV 文件)"

    total_rows = len(rows) - 1  # 减去表头
    headers = rows[0] if rows else []
    preview_rows = rows[:11]  # 表头 + 10 行数据

    lines = [f"CSV 文件：共 {total_rows} 行，{len(headers)} 列"]
    lines.append(f"列名：{', '.join(headers)}")
    lines.append("\n数据预览（前 10 行）：")

    # 简单表格输出
    col_widths = [max(len(str(row[i])) for row in preview_rows if i < len(row)) for i in range(len(headers))]
    col_widths = [min(w, 30) for w in col_widths]  # 限制列宽

    for i, row in enumerate(preview_rows):
        cells = []
        for j, w in enumerate(col_widths):
            val = str(row[j]) if j < len(row) else ""
            cells.append(val[:w].ljust(w))
        lines.append(" | ".join(cells))
        if i == 0:
            lines.append("-+-".join("-" * w for w in col_widths))

    return "\n".join(lines)


def _extract_json(path: Path) -> str:
    """提取 JSON 文件，格式化输出。"""
    try:
        text = path.read_text(encoding="utf-8")
        data = json.loads(text)
        return f"JSON 文件内容：\n{json.dumps(data, ensure_ascii=False, indent=2)}"
    except json.JSONDecodeError:
        # 不是合法 JSON，当纯文本返回
        return f"文件内容（非合法 JSON）：\n{text}"
    except OSError as e:
        raise ValueError(f"读取 JSON 失败: {e}")


def _extract_pdf_file(path: Path) -> str:
    """提取 PDF 文件文本。需要 pypdf 库。"""
    try:
        from pypdf import PdfReader
    except ImportError:
        return f"[PDF 文件: {path.name}]（需要安装 pypdf 库才能提取 PDF 文本）"

    try:
        reader = PdfReader(str(path))
        pages = []
        for i, page in enumerate(reader.pages):
            text = page.extract_text() or ""
            if text.strip():
                pages.append(f"--- 第 {i + 1} 页 ---\n{text}")
        if not pages:
            return f"[PDF 文件: {path.name}]（无可提取文本，可能是扫描件）"
        return "\n\n".join(pages)
    except Exception as e:
        logger.warning("PDF 提取失败 %s: %s", path, e)
        return f"[PDF 文件: {path.name}]（提取失败: {e}）"


def _describe_image_file(path: Path) -> str:
    """描述图片文件。"""
    size_kb = path.stat().st_size / 1024
    return f"[图片文件: {path.name}, {size_kb:.1f} KB]（需要 OCR 或视觉模型处理）"


def build_context_with_files(
    prompt: str,
    file_paths: list[str | Path],
    cleanup_paths: list[str] | None = None,
) -> str:
    """将多个文件内容拼入 prompt 上下文。

    Args:
        prompt: 原始 prompt
        file_paths: 文件路径列表
        cleanup_paths: 执行完成后需要清理的临时文件路径

    Returns:
        增强后的 prompt（包含文件上下文）
    """
    if not file_paths:
        return prompt

    contexts = []
    for fp in file_paths:
        try:
            text = extract_text_from_file(fp)
            name = Path(fp).name
            contexts.append(f"\n\n--- 文件: {name} ---\n{text}")
        except Exception as e:
            logger.warning("文件 %s 提取失败: %s", fp, e)
            contexts.append(f"\n\n--- 文件: {Path(fp).name} ---\n[提取失败: {e}]")

    if contexts:
        return prompt + "\n\n=== 附件内容 ===" + "".join(contexts)
    return prompt


def save_upload_to_temp(content: bytes, filename: str, temp_dir: str | None = None) -> str:
    """将上传文件内容保存到临时目录。

    Args:
        content: 文件内容
        filename: 原始文件名
        temp_dir: 临时目录（默认自动创建）

    Returns:
        保存的文件路径
    """
    if temp_dir is None:
        temp_dir = tempfile.mkdtemp(prefix="agent_upload_")

    dir_path = Path(temp_dir)
    dir_path.mkdir(parents=True, exist_ok=True)
    path = dir_path / filename
    path.write_bytes(content)
    return str(path)
