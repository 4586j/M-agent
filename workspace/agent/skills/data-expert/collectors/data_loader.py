import pandas as pd
from pathlib import Path
from typing import Optional, Union


class DataLoader:
    """支持多种格式的数据加载器"""

    @staticmethod
    def load(file_path: str, **kwargs) -> pd.DataFrame:
        """根据文件扩展名自动选择读取方法"""
        path = Path(file_path)
        suffix = path.suffix.lower()

        if suffix == ".csv":
            return pd.read_csv(file_path, **kwargs)
        elif suffix in (".xls", ".xlsx"):
            return pd.read_excel(file_path, **kwargs)
        elif suffix == ".json":
            return pd.read_json(file_path, **kwargs)
        elif suffix == ".parquet":
            return pd.read_parquet(file_path, **kwargs)
        else:
            raise ValueError(f"不支持的文件格式: {suffix}")

    @staticmethod
    def load_large_csv(file_path: str, chunksize: int = 10000):
        """分块读取大文件，返回迭代器"""
        return pd.read_csv(file_path, chunksize=chunksize)
