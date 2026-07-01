import pandas as pd
import numpy as np
from typing import Dict


class StatAnalyzer:
    """统计分析引擎"""

    def describe(self, df: pd.DataFrame) -> Dict:
        """生成描述性统计（含偏度、峰度）"""
        numeric_cols = df.select_dtypes(include=[np.number]).columns
        if len(numeric_cols) == 0:
            return {}

        desc = df[numeric_cols].describe().to_dict()
        skewness = df[numeric_cols].skew().to_dict()
        kurtosis = df[numeric_cols].kurtosis().to_dict()
        return {
            "basic_stats": desc,
            "skewness": skewness,
            "kurtosis": kurtosis,
        }

    def correlation(self, df: pd.DataFrame, method: str = "pearson") -> pd.DataFrame:
        """计算相关性矩阵"""
        numeric_df = df.select_dtypes(include=[np.number])
        if numeric_df.shape[1] < 2:
            return pd.DataFrame()
        return numeric_df.corr(method=method)

    def detect_outliers_iqr(self, df: pd.DataFrame, col: str) -> Dict:
        """检测某列的异常值"""
        Q1 = df[col].quantile(0.25)
        Q3 = df[col].quantile(0.75)
        IQR = Q3 - Q1
        lower = Q1 - 1.5 * IQR
        upper = Q3 + 1.5 * IQR
        outliers = df[(df[col] < lower) | (df[col] > upper)]
        return {
            "count": len(outliers),
            "lower_bound": lower,
            "upper_bound": upper,
            "ratio": len(outliers) / len(df) if len(df) > 0 else 0,
        }
