import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from experience_store import ExperienceStore, ExperienceRecord


def make_exp(exp_id="exp-1", role="executor", prompt="analyze data",
             output="result", success=True):
    return ExperienceRecord(
        exp_id=exp_id, role=role, prompt=prompt,
        output_summary=output, success=success,
        created_at="t0",
    )


def test_write_and_get(tmp_path):
    store = ExperienceStore(str(tmp_path))
    rec = make_exp()
    store.write(rec)
    got = store.get("exp-1")
    assert got is not None
    assert got.prompt == "analyze data"
    assert got.success is True


def test_get_missing_returns_none(tmp_path):
    store = ExperienceStore(str(tmp_path))
    assert store.get("nope") is None


def test_delete(tmp_path):
    store = ExperienceStore(str(tmp_path))
    store.write(make_exp())
    assert store.delete("exp-1") is True
    assert store.get("exp-1") is None
    assert store.delete("exp-1") is False


def test_list_all(tmp_path):
    store = ExperienceStore(str(tmp_path))
    store.write(make_exp("exp-1"))
    store.write(make_exp("exp-2", role="analyzer"))
    recs = store.list_all()
    assert len(recs) == 2
    assert {r.exp_id for r in recs} == {"exp-1", "exp-2"}


def test_list_by_role(tmp_path):
    store = ExperienceStore(str(tmp_path))
    store.write(make_exp("exp-1", role="executor"))
    store.write(make_exp("exp-2", role="analyzer"))
    store.write(make_exp("exp-3", role="executor"))
    execs = store.list_by_role("executor")
    assert {r.exp_id for r in execs} == {"exp-1", "exp-3"}


def test_list_successful(tmp_path):
    store = ExperienceStore(str(tmp_path))
    store.write(make_exp("exp-1", success=True))
    store.write(make_exp("exp-2", success=False))
    store.write(make_exp("exp-3", success=True))
    ok = store.list_successful()
    assert {r.exp_id for r in ok} == {"exp-1", "exp-3"}


def test_search_by_keyword_overlap(tmp_path):
    store = ExperienceStore(str(tmp_path))
    store.write(make_exp("exp-1", prompt="analyze sales data", output="generated report"))
    store.write(make_exp("exp-2", prompt="clean user data", output="removed duplicates"))
    store.write(make_exp("exp-3", prompt="analyze revenue data", output="created chart"))
    results = store.search("analyze data")
    assert len(results) >= 2
    ids = [r.exp_id for r in results]
    assert "exp-1" in ids
    assert "exp-3" in ids


def test_search_filters_by_role(tmp_path):
    store = ExperienceStore(str(tmp_path))
    store.write(make_exp("exp-1", prompt="analyze data", role="executor"))
    store.write(make_exp("exp-2", prompt="analyze data", role="analyzer"))
    results = store.search("analyze data", role="analyzer")
    assert len(results) == 1
    assert results[0].exp_id == "exp-2"


def test_search_successful_only(tmp_path):
    store = ExperienceStore(str(tmp_path))
    store.write(make_exp("exp-1", prompt="analyze data", success=True))
    store.write(make_exp("exp-2", prompt="analyze data", success=False))
    results = store.search("analyze data", successful_only=True)
    assert len(results) == 1
    assert results[0].exp_id == "exp-1"
    results_all = store.search("analyze data", successful_only=False)
    assert len(results_all) == 2


def test_search_empty_query(tmp_path):
    store = ExperienceStore(str(tmp_path))
    store.write(make_exp("exp-1"))
    assert store.search("") == []


def test_search_limit(tmp_path):
    store = ExperienceStore(str(tmp_path))
    for i in range(10):
        store.write(make_exp(f"exp-{i}", prompt="analyze data"))
    results = store.search("analyze data", limit=3)
    assert len(results) == 3


def test_record_from_task(tmp_path):
    store = ExperienceStore(str(tmp_path))
    rec = store.record_from_task(
        task_id="task-abc123", role="executor", prompt="do something",
        output="task output", success=True, verdict=None,
        artifacts=["report.md"], created_at="t0",
    )
    assert rec.exp_id == "exp-abc123"
    assert rec.role == "executor"
    assert rec.success is True
    got = store.get(rec.exp_id)
    assert got is not None
    assert got.prompt == "do something"


def test_record_from_task_truncates_long_output(tmp_path):
    store = ExperienceStore(str(tmp_path))
    long_output = "x" * 1000
    rec = store.record_from_task(
        task_id="task-1", role="executor", prompt="test",
        output=long_output, success=True,
    )
    assert len(rec.output_summary) == 500


def test_cosine_similarity():
    from collections import Counter
    a = Counter({"hello": 2, "world": 1})
    b = Counter({"hello": 1, "world": 3})
    sim = ExperienceStore._cosine_similarity(a, b)
    assert sim > 0.5


def test_cosine_similarity_no_overlap():
    from collections import Counter
    a = Counter({"hello": 1})
    b = Counter({"world": 1})
    assert ExperienceStore._cosine_similarity(a, b) == 0.0


def test_cosine_similarity_empty():
    from collections import Counter
    assert ExperienceStore._cosine_similarity(Counter(), Counter({"a": 1})) == 0.0


class FakeEmbeddingModel:
    VOCAB = {"analyze": [1.0, 0.0, 0.0], "data": [0.0, 1.0, 0.0],
             "report": [0.0, 0.0, 1.0], "clean": [0.5, 0.5, 0.0],
             "sales": [1.0, 0.5, 0.0], "revenue": [0.8, 0.3, 0.1]}

    def encode(self, text: str) -> list[float]:
        words = text.lower().split()
        vec = [0.0, 0.0, 0.0]
        for w in words:
            if w in self.VOCAB:
                for i in range(3):
                    vec[i] += self.VOCAB[w][i]
        return vec


def test_search_with_embedding_model(tmp_path):
    emb = FakeEmbeddingModel()
    store = ExperienceStore(str(tmp_path), embedding_model=emb)
    store.write(make_exp("exp-1", prompt="analyze sales data", output="generated report"))
    store.write(make_exp("exp-2", prompt="clean user data", output="removed duplicates"))
    store.write(make_exp("exp-3", prompt="analyze revenue data", output="created chart"))

    # Reindex to add embeddings
    store.reindex()

    results = store.search("analyze data")
    assert len(results) >= 2
    ids = [r.exp_id for r in results]
    assert "exp-1" in ids
    assert "exp-3" in ids


def test_record_from_task_with_embedding(tmp_path):
    emb = FakeEmbeddingModel()
    store = ExperienceStore(str(tmp_path), embedding_model=emb)
    rec = store.record_from_task(
        task_id="task-1", role="executor", prompt="analyze data",
        output="report generated", success=True, created_at="t0",
    )
    assert rec.embedding is not None
    assert len(rec.embedding) == 3
    got = store.get(rec.exp_id)
    assert got.embedding is not None


def test_record_from_task_without_embedding(tmp_path):
    store = ExperienceStore(str(tmp_path))
    rec = store.record_from_task(
        task_id="task-1", role="executor", prompt="analyze data",
        output="report", success=True,
    )
    assert rec.embedding is None


def test_reindex_fills_missing_embeddings(tmp_path):
    emb = FakeEmbeddingModel()
    store = ExperienceStore(str(tmp_path), embedding_model=emb)
    store.write(make_exp("exp-1", prompt="analyze data"))
    store.write(make_exp("exp-2", prompt="clean user data"))

    count = store.reindex()
    assert count == 2
    assert store.get("exp-1").embedding is not None
    assert store.get("exp-2").embedding is not None

    # Second reindex should be no-op
    count2 = store.reindex()
    assert count2 == 0


def test_reindex_without_embedding_model(tmp_path):
    store = ExperienceStore(str(tmp_path))
    store.write(make_exp("exp-1"))
    assert store.reindex() == 0


def test_vector_search_skips_records_without_embedding(tmp_path):
    emb = FakeEmbeddingModel()
    store = ExperienceStore(str(tmp_path), embedding_model=emb)
    store.write(make_exp("exp-1", prompt="analyze data"))
    store.write(make_exp("exp-2", prompt="analyze data"))
    # Only reindex exp-1 manually
    rec1 = store.get("exp-1")
    rec1.embedding = emb.encode("analyze data")
    store.write(rec1)

    results = store.search("analyze data")
    # 混合检索语义：exp-1 双通道命中 RRF 排名最高；exp-2 无向量仍可经关键词通道召回
    assert results[0].exp_id == "exp-1"
    assert {r.exp_id for r in results} == {"exp-1", "exp-2"}


def test_capacity_eviction_failed_first(tmp_path):
    store = ExperienceStore(str(tmp_path), max_count=2)
    store.write(make_exp("exp-f1", prompt="f1", success=False))
    store.write(make_exp("exp-s1", prompt="s1"))
    store.write(make_exp("exp-s2", prompt="s2"))  # 触发淘汰
    ids = {r.exp_id for r in store.list_all()}
    assert "exp-f1" not in ids  # 失败经验最先被淘汰
    assert {"exp-s1", "exp-s2"} <= ids


def test_capacity_eviction_protects_hit_records(tmp_path):
    store = ExperienceStore(str(tmp_path), max_count=2)
    store.write(make_exp("exp-hot", prompt="hot topic data"))
    store.write(make_exp("exp-cold", prompt="cold topic data"))
    store.search("hot unique")  # 仅命中 exp-hot（cold 共享 topic/data 会误命中）
    store.write(make_exp("exp-new", prompt="new topic data"))  # 触发淘汰
    ids = {r.exp_id for r in store.list_all()}
    assert len(ids) == 2
    assert "exp-hot" in ids  # 命中多的被保护
    assert "exp-cold" not in ids


def test_ttl_purges_expired(tmp_path):
    import time as _t
    store = ExperienceStore(str(tmp_path), ttl_days=1)
    old = make_exp("exp-old", prompt="old")
    old.created_at = str(int(_t.time()) - 2 * 86400)  # 两天前
    store.write(old)
    fresh = make_exp("exp-new", prompt="new")
    fresh.created_at = str(int(_t.time()))  # make_exp 默认 t0 会被视为 1970 过期
    store.write(fresh)
    ids = {r.exp_id for r in store.list_all()}
    assert "exp-old" not in ids
    assert "exp-new" in ids


def test_search_tracks_hits(tmp_path):
    store = ExperienceStore(str(tmp_path))
    store.write(make_exp("exp-1", prompt="analyze data"))
    store.search("analyze data")
    assert store._hits["exp-1"] == 1
    store.search("analyze data")
    assert store._hits["exp-1"] == 2
