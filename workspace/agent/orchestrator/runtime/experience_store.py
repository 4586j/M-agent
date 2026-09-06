"""任务经验库：记录已完成任务的经验，供后续相似任务参考。

每个经验记录包含任务的关键信息（prompt、role、输出摘要、成功状态等），
支持按关键词检索相似经验，帮助 Worker 在执行新任务时参考历史成功案例。
可选接入 EmbeddingModel 实现语义检索。
"""
import json
import re
import time
from collections import Counter
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from embedding_model import EmbeddingModel, cosine_similarity_vec
from safe_id import validate_id


@dataclass
class ExperienceRecord:
    exp_id: str
    role: str
    prompt: str
    output_summary: str
    success: bool
    verdict: dict | None = None
    artifacts: list[str] = field(default_factory=list)
    created_at: str = ""
    tags: list[str] = field(default_factory=list)
    embedding: list[float] | None = None
    hits: int = 0  # 被检索命中次数（持久化，支撑淘汰保护）
    last_hit_at: str = ""  # 最近命中时间（epoch 秒字符串）
    version: int = 0  # 内容修改版本号：显式 write 递增，命中计数不递增


def _parse_ts(created_at: str) -> float:
    """created_at 存的是 epoch 秒字符串；解析失败按 0（最旧）处理。"""
    try:
        return float(created_at)
    except (TypeError, ValueError):
        return 0.0


def _rrf_fuse(ranked_lists: list[list["ExperienceRecord"]],
              k: int = 60) -> list[tuple["ExperienceRecord", float]]:
    """Reciprocal Rank Fusion：融合多个排序列表，对分数尺度不敏感、结果确定。

    同一记录（按 exp_id）在越多列表越靠前，融合分越高。
    """
    scores: dict[str, float] = {}
    order: dict[str, ExperienceRecord] = {}
    for lst in ranked_lists:
        for rank, rec in enumerate(lst):
            scores[rec.exp_id] = scores.get(rec.exp_id, 0.0) + 1.0 / (k + rank + 1)
            order.setdefault(rec.exp_id, rec)
    ranked = sorted(scores.items(), key=lambda x: x[1], reverse=True)
    return [(order[eid], s) for eid, s in ranked]


class ExperienceStore:
    """经验持久化。每个经验一个 JSON 文件，支持关键词/语义混合检索。

    带淘汰机制：容量超限时优先淘汰失败经验（最旧先出），成功经验按
    命中次数（内存计数）保护——遗忘与保留同等重要，防止检索噪声累积。
    可选按 TTL 天数清理过期记录（ttl_days=0 关闭）。
    """

    def __init__(self, store_dir: str, embedding_model: EmbeddingModel | None = None,
                 max_count: int = 500, ttl_days: int = 0):
        self.store_dir = Path(store_dir)
        self.store_dir.mkdir(parents=True, exist_ok=True)
        self.embedding_model = embedding_model
        self.max_count = max_count
        self.ttl_days = ttl_days
        self._cache: list[ExperienceRecord] | None = None

    def _invalidate(self) -> None:
        self._cache = None

    def _path(self, exp_id: str) -> Path:
        validate_id(exp_id, "exp_id")
        return self.store_dir / f"{exp_id}.json"

    def _dump(self, record: ExperienceRecord) -> None:
        """直接落盘单条记录。缓存内是同一对象，原地变更已同步，无需失效。"""
        with open(self._path(record.exp_id), "w", encoding="utf-8") as f:
            json.dump(asdict(record), f, ensure_ascii=False, indent=2)

    def write(self, record: ExperienceRecord) -> None:
        record.version = (record.version or 0) + 1
        self._dump(record)
        self._invalidate()
        self._enforce_limits()

    def _enforce_limits(self) -> int:
        """容量与 TTL 淘汰。返回淘汰条数。

        策略：先清 TTL 过期（ttl_days>0），再在超容量时按
        「失败经验最旧先出 → 成功经验按命中少、created_at 旧先出」淘汰。
        """
        removed = 0
        recs = self.list_all()
        if self.ttl_days > 0:
            cutoff = time.time() - self.ttl_days * 86400
            expired = [r for r in recs if _parse_ts(r.created_at) < cutoff]
            for r in expired:
                self.delete(r.exp_id)
            removed += len(expired)
            recs = [r for r in recs if r not in expired]
        if self.max_count > 0 and len(recs) > self.max_count:
            def evict_key(r: ExperienceRecord):
                # 失败优先淘汰；同级别内命中少的先出，再按 created_at 旧的先出
                return (0 if not r.success else 1,
                        r.hits,
                        _parse_ts(r.created_at))
            victims = sorted(recs, key=evict_key)[: len(recs) - self.max_count]
            for r in victims:
                self.delete(r.exp_id)
            removed += len(victims)
        return removed

    def get(self, exp_id: str) -> ExperienceRecord | None:
        rec = self._get_raw(exp_id)
        if rec is not None:
            self._touch_hits([rec])
        return rec

    def _touch_hits(self, records: list[ExperienceRecord]) -> None:
        """命中计数持久化：检索/读取命中即更新 hits 与 last_hit_at 并落盘。

        淘汰策略据此保护高频经验；写入量小（每次检索 ≤ limit 条）。
        """
        now = str(int(time.time()))
        for rec in records:
            rec.hits = (rec.hits or 0) + 1
            rec.last_hit_at = now
            self._dump(rec)

    def _get_raw(self, exp_id: str) -> ExperienceRecord | None:
        if self._cache is not None:
            for r in self._cache:
                if r.exp_id == exp_id:
                    return r
            return None
        p = self._path(exp_id)
        if not p.exists():
            return None
        with open(p, encoding="utf-8") as f:
            return ExperienceRecord(**json.load(f))

    def delete(self, exp_id: str) -> bool:
        p = self._path(exp_id)
        if p.exists():
            p.unlink()
            self._invalidate()
            return True
        return False

    def list_all(self) -> list[ExperienceRecord]:
        if self._cache is not None:
            return list(self._cache)
        out = []
        for p in sorted(self.store_dir.glob("*.json")):
            with open(p, encoding="utf-8") as f:
                out.append(ExperienceRecord(**json.load(f)))
        self._cache = out
        return list(out)

    def list_by_role(self, role: str) -> list[ExperienceRecord]:
        return [r for r in self.list_all() if r.role == role]

    def list_successful(self) -> list[ExperienceRecord]:
        return [r for r in self.list_all() if r.success]

    def search(self, query: str, role: str | None = None,
               successful_only: bool = True, limit: int = 5) -> list[ExperienceRecord]:
        """混合检索相似经验：关键词词频余弦 + 语义向量（可选）RRF 融合排序。

        有 embedding_model 时两路分数用 Reciprocal Rank Fusion 融合
        （对分数尺度不敏感、实现确定）；否则降级为纯关键词。
        命中记录累计持久化命中计数，供淘汰策略保护高频经验。
        """
        if not query:
            return []

        candidates = self.list_all()
        if role:
            candidates = [r for r in candidates if r.role == role]
        if successful_only:
            candidates = [r for r in candidates if r.success]

        kw_ranked = self._search_keyword(query, candidates, limit=len(candidates))
        if self.embedding_model:
            vec_ranked = self._search_vector(query, candidates, limit=len(candidates))
            fused = _rrf_fuse([kw_ranked, vec_ranked])
            results = [rec for rec, _ in fused]
        else:
            results = kw_ranked
        results = results[:limit]
        self._touch_hits(results)
        return results

    def _search_vector(self, query: str, candidates: list[ExperienceRecord],
                       limit: int) -> list[ExperienceRecord]:
        query_vec = self.embedding_model.encode(query)
        scored = []
        for rec in candidates:
            if rec.embedding is None:
                continue
            score = cosine_similarity_vec(query_vec, rec.embedding)
            if score > 0:
                scored.append((score, rec))
        scored.sort(key=lambda x: x[0], reverse=True)
        return [rec for _, rec in scored[:limit]]

    def _search_keyword(self, query: str, candidates: list[ExperienceRecord],
                        limit: int) -> list[ExperienceRecord]:
        query_tokens = self._tokenize(query)
        if not query_tokens:
            return []
        scored = []
        for rec in candidates:
            text = f"{rec.prompt} {rec.output_summary}"
            doc_tokens = self._tokenize(text)
            score = self._cosine_similarity(query_tokens, doc_tokens)
            if score > 0:
                scored.append((score, rec))
        scored.sort(key=lambda x: x[0], reverse=True)
        return [rec for _, rec in scored[:limit]]

    @staticmethod
    def _tokenize(text: str) -> Counter:
        """简单分词：按非字母数字字符拆分，转小写，统计词频。"""
        words = re.findall(r'\w+', text.lower())
        return Counter(words)

    @staticmethod
    def _cosine_similarity(a: Counter, b: Counter) -> float:
        """计算两个词频向量的余弦相似度。"""
        if not a or not b:
            return 0.0
        common = set(a.keys()) & set(b.keys())
        if not common:
            return 0.0
        dot = sum(a[k] * b[k] for k in common)
        norm_a = sum(v * v for v in a.values()) ** 0.5
        norm_b = sum(v * v for v in b.values()) ** 0.5
        if norm_a == 0 or norm_b == 0:
            return 0.0
        return dot / (norm_a * norm_b)

    def record_from_task(self, task_id: str, role: str, prompt: str,
                         output: Any, success: bool, verdict: dict | None = None,
                         artifacts: list[str] | None = None,
                         created_at: str = "") -> ExperienceRecord:
        """从任务结果创建经验记录。output 会被截断为前 500 字符作为摘要。"""
        output_str = str(output) if output else ""
        summary = output_str[:500] if len(output_str) > 500 else output_str

        exp_id = f"exp-{task_id.replace('task-', '')}"
        embedding = None
        if self.embedding_model:
            embedding = self.embedding_model.encode(f"{prompt} {summary}")
        rec = ExperienceRecord(
            exp_id=exp_id,
            role=role,
            prompt=prompt,
            output_summary=summary,
            success=success,
            verdict=verdict,
            artifacts=artifacts or [],
            created_at=created_at,
            tags=[],
            embedding=embedding,
        )
        self.write(rec)
        return rec

    def reindex(self) -> int:
        """为所有缺少 embedding 的记录计算向量。返回更新的记录数。"""
        if not self.embedding_model:
            return 0
        count = 0
        for rec in self.list_all():
            if rec.embedding is not None:
                continue
            rec.embedding = self.embedding_model.encode(f"{rec.prompt} {rec.output_summary}")
            self.write(rec)
            count += 1
        return count
