from dataclasses import dataclass


@dataclass
class DataQualityScore:
    """数据质量评分模型"""

    overall: float
    completeness: float
    consistency: float
    accuracy: float
    grade: str
    passed: bool
