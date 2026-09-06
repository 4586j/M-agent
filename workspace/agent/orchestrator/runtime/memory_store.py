"""记忆访问抽象。V2 落地为本地文件；接口便于将来换共享存储。"""
from abc import ABC, abstractmethod
from pathlib import Path

from safe_id import validate_id


class MemoryStore(ABC):
    @abstractmethod
    def read_core(self, name: str) -> str | None: ...
    @abstractmethod
    def write_core(self, name: str, content: str) -> None: ...
    @abstractmethod
    def delete_core(self, name: str) -> None: ...
    @abstractmethod
    def read_context(self, task_id: str, key: str) -> str | None: ...
    @abstractmethod
    def write_context(self, task_id: str, key: str, value: str) -> None: ...

    # 列举/归档为可选能力，默认空实现，便于自定义实现渐进接入
    def list_core(self) -> list[str]:
        return []

    def list_context(self, task_id: str) -> list[str]:
        return []

    def delete_context(self, task_id: str) -> bool:
        """归档清理整个任务的上下文记忆。返回是否有删除动作。"""
        return False


class FileMemoryStore(MemoryStore):
    def __init__(self, core_dir: str, context_dir: str):
        self.core_dir = Path(core_dir)
        self.context_dir = Path(context_dir)
        self.core_dir.mkdir(parents=True, exist_ok=True)
        self.context_dir.mkdir(parents=True, exist_ok=True)

    def read_core(self, name: str) -> str | None:
        validate_id(name, "name")
        p = self.core_dir / f"{name}.md"
        if not p.exists():
            return None
        return p.read_text(encoding="utf-8")

    def write_core(self, name: str, content: str) -> None:
        validate_id(name, "name")
        (self.core_dir / f"{name}.md").write_text(content, encoding="utf-8")

    def delete_core(self, name: str) -> None:
        validate_id(name, "name")
        p = self.core_dir / f"{name}.md"
        if p.exists():
            p.unlink()

    def read_context(self, task_id: str, key: str) -> str | None:
        validate_id(task_id, "task_id")
        validate_id(key, "key")
        p = self.context_dir / task_id / f"{key}.md"
        if not p.exists():
            return None
        return p.read_text(encoding="utf-8")

    def write_context(self, task_id: str, key: str, value: str) -> None:
        validate_id(task_id, "task_id")
        validate_id(key, "key")
        d = self.context_dir / task_id
        d.mkdir(parents=True, exist_ok=True)
        (d / f"{key}.md").write_text(value, encoding="utf-8")

    def list_core(self) -> list[str]:
        if not self.core_dir.is_dir():
            return []
        return sorted(p.stem for p in self.core_dir.glob("*.md"))

    def list_context(self, task_id: str) -> list[str]:
        validate_id(task_id, "task_id")
        d = self.context_dir / task_id
        if not d.is_dir():
            return []
        return sorted(p.stem for p in d.glob("*.md"))

    def delete_context(self, task_id: str) -> bool:
        """归档清理整个任务的上下文目录（任务结束约定）。"""
        validate_id(task_id, "task_id")
        d = self.context_dir / task_id
        if not d.is_dir():
            return False
        for p in d.iterdir():
            if p.is_file():
                p.unlink()
        d.rmdir()
        return True
