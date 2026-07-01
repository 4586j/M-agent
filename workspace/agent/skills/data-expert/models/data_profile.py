from dataclasses import dataclass, field
from typing import Dict, List


@dataclass
class DataProfile:
    """数据概况模型"""

    file_name: str
    row_count: int
    col_count: int
    column_names: List[str] = field(default_factory=list)
    dtypes: Dict[str, str] = field(default_factory=dict)
    memory_usage: float = 0.0
    missing_summary: Dict[str, float] = field(default_factory=dict)
