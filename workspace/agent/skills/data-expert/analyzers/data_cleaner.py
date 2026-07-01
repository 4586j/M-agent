import pandas as pd
import numpy as np
from typing import Dict, Optional


class DataCleaner:
    """数据清洗引擎"""

    def __init__(self, config: Optional[Dict] = None):
        self.config = config or {}
        self.missing_threshold = self.config.get("missing_threshold", 0.3)

    def clean(self, df: pd.DataFrame) -> pd.DataFrame:
        """执行完整清洗流程"""
        df = self._remove_duplicates(df)
        df = self._handle_missing(df)
        df = self._standardize_dtypes(df)
        df = self._handle_outliers(df)
        return df

    def _remove_duplicates(self, df: pd.DataFrame) -> pd.DataFrame:
        return df.drop_duplicates()

    def _handle_missing(self, df: pd.DataFrame) -> pd.DataFrame:
        missing_ratio = df.isnull().mean()
        cols_to_drop = missing_ratio[missing_ratio > self.missing_threshold].index.tolist()
        df = df.drop(columns=cols_to_drop)

        for col in df.columns:
            if df[col].dtype in ("float64", "int64"):
                df[col].fillna(df[col].median(), inplace=True)
            else:
                mode_series = df[col].mode()
                fill_value = mode_series[0] if not mode_series.empty else "Unknown"
                df[col].fillna(fill_value, inplace=True)
        return df

    def _standardize_dtypes(self, df: pd.DataFrame) -> pd.DataFrame:
        for col in df.select_dtypes(include=["object"]).columns:
            try:
                df[col] = pd.to_datetime(df[col])
            except (ValueError, TypeError):
                pass
        return df

    def _handle_outliers(self, df: pd.DataFrame) -> pd.DataFrame:
        for col in df.select_dtypes(include=["float64", "int64"]).columns:
            Q1 = df[col].quantile(0.25)
            Q3 = df[col].quantile(0.75)
            IQR = Q3 - Q1
            lower = Q1 - 1.5 * IQR
            upper = Q3 + 1.5 * IQR
            df[col] = df[col].clip(lower, upper)
        return df

    def get_cleaning_report(self, original: pd.DataFrame, cleaned: pd.DataFrame) -> Dict:
        """生成清洗报告"""
        return {
            "rows_removed": len(original) - len(cleaned),
            "cols_removed": len(original.columns) - len(cleaned.columns),
            "missing_handled": int(original.isnull().sum().sum() - cleaned.isnull().sum().sum()),
        }
