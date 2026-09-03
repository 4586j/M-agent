"""Embedding 模型抽象层。可选依赖 sentence-transformers，缺失时降级为关键词搜索。"""
import logging
import os
from abc import ABC, abstractmethod

log = logging.getLogger(__name__)


class EmbeddingModel(ABC):
    @abstractmethod
    def encode(self, text: str) -> list[float]:
        """将文本编码为向量。"""


class SentenceTransformerModel(EmbeddingModel):
    def __init__(self, model_name: str = "all-MiniLM-L6-v2"):
        from sentence_transformers import SentenceTransformer
        self._model = SentenceTransformer(model_name)

    def encode(self, text: str) -> list[float]:
        return self._model.encode(text).tolist()


def create_embedding_model() -> EmbeddingModel | None:
    """工厂函数：尝试加载 sentence-transformers，失败返回 None。

    设置 ORCH_EMBEDDING_MODEL="" 可显式禁用。
    """
    model_name = os.environ.get("ORCH_EMBEDDING_MODEL", "all-MiniLM-L6-v2")
    if not model_name:
        return None
    try:
        return SentenceTransformerModel(model_name)
    except ImportError:
        log.info("sentence-transformers 未安装，语义检索降级为关键词搜索")
        return None
    except Exception:
        log.exception("Embedding 模型加载失败，降级为关键词搜索")
        return None


def cosine_similarity_vec(a: list[float], b: list[float]) -> float:
    """纯 Python 余弦相似度（不依赖 numpy）。"""
    if len(a) != len(b) or not a:
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = sum(x * x for x in a) ** 0.5
    norm_b = sum(x * x for x in b) ** 0.5
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)
