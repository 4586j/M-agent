import pandas as pd
import numpy as np
from dataclasses import dataclass


@dataclass
class DataQualityScore:
    overall: float
    completeness: float
    consistency: float
    accuracy: float
    grade: str
    passed: bool


class ScoreCalculator:
    """数据质量评分"""

    WEIGHTS = {
        "completeness": 0.4,
        "consistency": 0.3,
        "accuracy": 0.3,
    }

    def calculate(self, df: pd.DataFrame) -> DataQualityScore:
        completeness = self._completeness_score(df)
        consistency = self._consistency_score(df)
        accuracy = self._accuracy_score(df)

        overall = (
            completeness * self.WEIGHTS["completeness"]
            + consistency * self.WEIGHTS["consistency"]
            + accuracy * self.WEIGHTS["accuracy"]
        )

        grade = self._score_to_grade(overall)
        return DataQualityScore(
            overall=overall,
            completeness=completeness,
            consistency=consistency,
            accuracy=accuracy,
            grade=grade,
            passed=overall >= 60,
        )

    def _completeness_score(self, df: pd.DataFrame) -> float:
        total_cells = df.shape[0] * df.shape[1]
        if total_cells == 0:
            return 0.0
        non_null_cells = df.notnull().sum().sum()
        return (non_null_cells / total_cells) * 100

    def _consistency_score(self, df: pd.DataFrame) -> float:
        score = 100.0
        for col in df.columns:
            if df[col].dtype == "object":
                numeric_ratio = df[col].str.match(r"^\d+(\.\d+)?$").mean() if len(df) > 0 else 0
                if numeric_ratio > 0.5:
                    score -= 10
        return max(0, score)

    def _accuracy_score(self, df: pd.DataFrame) -> float:
        numeric_cols = df.select_dtypes(include=[np.number]).columns
        if len(numeric_cols) == 0:
            return 100.0

        outlier_ratios = []
        for col in numeric_cols:
            std_val = df[col].std()
            if std_val == 0 or pd.isna(std_val):
                continue
            z_scores = np.abs((df[col] - df[col].mean()) / std_val)
            outlier_ratio = (z_scores > 3).mean()
            outlier_ratios.append(outlier_ratio)

        if not outlier_ratios:
            return 100.0

        avg_outlier_ratio = np.mean(outlier_ratios)
        return max(0, 100 * (1 - avg_outlier_ratio))

    def _score_to_grade(self, score: float) -> str:
        if score >= 90:
            return "A"
        elif score >= 80:
            return "B"
        elif score >= 70:
            return "C"
        elif score >= 60:
            return "D"
        else:
            return "F"
