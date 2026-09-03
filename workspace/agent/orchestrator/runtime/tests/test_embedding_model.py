import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from embedding_model import EmbeddingModel, cosine_similarity_vec


def test_cosine_similarity_vec_identical():
    a = [1.0, 0.0, 0.0]
    assert abs(cosine_similarity_vec(a, a) - 1.0) < 1e-9


def test_cosine_similarity_vec_orthogonal():
    a = [1.0, 0.0]
    b = [0.0, 1.0]
    assert cosine_similarity_vec(a, b) == 0.0


def test_cosine_similarity_vec_empty():
    assert cosine_similarity_vec([], []) == 0.0


def test_cosine_similarity_vec_different_lengths():
    assert cosine_similarity_vec([1.0], [1.0, 2.0]) == 0.0


def test_cosine_similarity_vec_zero_vector():
    assert cosine_similarity_vec([0.0, 0.0], [1.0, 2.0]) == 0.0


def test_cosine_similarity_vec_partial():
    a = [1.0, 1.0, 0.0]
    b = [1.0, 0.0, 0.0]
    sim = cosine_similarity_vec(a, b)
    assert 0 < sim < 1


class FakeEmbeddingModel(EmbeddingModel):
    """Simple fake: maps words to dimensions for deterministic testing."""
    VOCAB = {"hello": [1.0, 0.0, 0.0], "world": [0.0, 1.0, 0.0],
             "foo": [0.0, 0.0, 1.0], "bar": [0.5, 0.5, 0.0]}

    def encode(self, text: str) -> list[float]:
        words = text.lower().split()
        vec = [0.0, 0.0, 0.0]
        for w in words:
            if w in self.VOCAB:
                for i in range(3):
                    vec[i] += self.VOCAB[w][i]
        return vec


def test_fake_embedding_encode():
    m = FakeEmbeddingModel()
    vec = m.encode("hello world")
    assert vec == [1.0, 1.0, 0.0]
