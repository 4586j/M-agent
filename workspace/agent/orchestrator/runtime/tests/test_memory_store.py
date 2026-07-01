import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from memory_store import FileMemoryStore


def test_core_write_read_delete(tmp_path):
    ms = FileMemoryStore(core_dir=str(tmp_path / "core"), context_dir=str(tmp_path / "ctx"))
    ms.write_core("proj-pref", "用户偏好报告含改进建议")
    assert ms.read_core("proj-pref") == "用户偏好报告含改进建议"
    ms.delete_core("proj-pref")
    assert ms.read_core("proj-pref") is None


def test_core_read_missing_returns_none(tmp_path):
    ms = FileMemoryStore(core_dir=str(tmp_path / "core"), context_dir=str(tmp_path / "ctx"))
    assert ms.read_core("nope") is None


def test_context_write_read_per_task(tmp_path):
    ms = FileMemoryStore(core_dir=str(tmp_path / "core"), context_dir=str(tmp_path / "ctx"))
    ms.write_context("task-1", "tried", "方案A失败")
    ms.write_context("task-2", "tried", "方案B")
    assert ms.read_context("task-1", "tried") == "方案A失败"
    assert ms.read_context("task-2", "tried") == "方案B"
    assert ms.read_context("task-1", "missing") is None
