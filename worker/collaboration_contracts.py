"""Runner contracts shared by the collaboration CLI and adapters."""
import threading
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Produced:
    name: str
    media_type: str
    content: bytes


@dataclass(frozen=True)
class RunRequest:
    attempt_id: str
    work_item_id: str
    input_hash: str
    requirement_hash: str
    package: dict
    requirement_text: str
    work_dir: Path
    allowed_tools: tuple
    cancel: threading.Event


@dataclass(frozen=True)
class RunOutcome:
    status: str
    summary: str
    produced: tuple = ()
    usage_units: int = 0
