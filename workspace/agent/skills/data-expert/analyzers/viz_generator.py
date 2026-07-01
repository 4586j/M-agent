import matplotlib
matplotlib.use("Agg")  # 非交互后端，避免 GUI 依赖

import matplotlib.pyplot as plt
import seaborn as sns
import pandas as pd
from pathlib import Path
from typing import Optional, List


class VizGenerator:
    """自动生成图表"""

    def __init__(self, output_dir: str = "workspace/agent/reports/data-expert"):
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        plt.rcParams["font.sans-serif"] = ["SimHei", "DejaVu Sans"]
        plt.rcParams["axes.unicode_minus"] = False

    def auto_generate(self, df: pd.DataFrame, prefix: str = "analysis") -> List[str]:
        """根据数据类型自动生成图表"""
        saved_files: List[str] = []
        numeric_cols = df.select_dtypes(include=["float64", "int64"]).columns
        categorical_cols = df.select_dtypes(include=["object", "category"]).columns

        for col in numeric_cols[:5]:
            path = self._plot_histogram(df, col, prefix)
            if path:
                saved_files.append(path)

        if len(numeric_cols) >= 2:
            path = self._plot_correlation_heatmap(df, numeric_cols, prefix)
            if path:
                saved_files.append(path)

        for col in categorical_cols[:3]:
            if df[col].nunique() <= 20:
                path = self._plot_countplot(df, col, prefix)
                if path:
                    saved_files.append(path)

        return saved_files

    def _plot_histogram(self, df: pd.DataFrame, col: str, prefix: str) -> Optional[str]:
        fig, ax = plt.subplots(figsize=(8, 5))
        df[col].hist(bins=30, ax=ax, edgecolor="black")
        ax.set_title(f"{col} 分布直方图")
        ax.set_xlabel(col)
        ax.set_ylabel("频数")
        filepath = self.output_dir / f"{prefix}_hist_{col}.png"
        plt.savefig(str(filepath), dpi=100, bbox_inches="tight")
        plt.close()
        return str(filepath)

    def _plot_correlation_heatmap(self, df: pd.DataFrame, cols: List[str], prefix: str) -> Optional[str]:
        corr = df[cols].corr()
        fig, ax = plt.subplots(figsize=(10, 8))
        sns.heatmap(corr, annot=True, fmt=".2f", cmap="coolwarm", ax=ax)
        ax.set_title("相关性热力图")
        filepath = self.output_dir / f"{prefix}_corr.png"
        plt.savefig(str(filepath), dpi=100, bbox_inches="tight")
        plt.close()
        return str(filepath)

    def _plot_countplot(self, df: pd.DataFrame, col: str, prefix: str) -> Optional[str]:
        fig, ax = plt.subplots(figsize=(10, 6))
        df[col].value_counts().head(15).plot(kind="bar", ax=ax)
        ax.set_title(f"{col} 类别计数")
        ax.set_xlabel(col)
        ax.set_ylabel("数量")
        plt.xticks(rotation=45, ha="right")
        filepath = self.output_dir / f"{prefix}_count_{col}.png"
        plt.savefig(str(filepath), dpi=100, bbox_inches="tight")
        plt.close()
        return str(filepath)
