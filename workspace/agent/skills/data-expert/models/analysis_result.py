from dataclasses import dataclass, field
from typing import Dict, List

from .data_profile import DataProfile
from .quality_metric import DataQualityScore


@dataclass
class AnalysisResult:
    """分析结果模型"""

    data_profile: DataProfile
    quality_score: DataQualityScore
    cleaning_report: Dict = field(default_factory=dict)
    statistics: Dict = field(default_factory=dict)
    chart_paths: List[str] = field(default_factory=list)
    report_path: str = ""
