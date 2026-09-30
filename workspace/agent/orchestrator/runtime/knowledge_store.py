"""文档知识库 v1：给 agent「喂文档」的通道（记忆四层之外的参考资料层）。

与经验库（experience）的分工：经验是「agent 自己干活的成败总结」，知识库是
**人工上传的确定性资料**——项目规范、API 文档、运维手册、领域知识。派发时
自动检索与 prompt 相关的片段注入 context_memory（复用 clamp 限幅通道）。

存储：每文档一个 JSON 文件（<knowledge_dir>/<doc_id>.json）：
- chunks：按段落切分的 ~600 字符块（检索粒度，避免长文档稀释相关信号）
- chunk_embeddings：embedding_model 可用时预计算（brain 启动时创建一次）
检索：关键词词频余弦（与 experience_store 同款简单分词）+ 语义向量 RRF 融合；
embedding 缺失时降级纯关键词。
"""
import json
import logging
import os
import re
import threading
import time
import uuid
from collections import Counter
from pathlib import Path

from embedding_model import cosine_similarity_vec

log = logging.getLogger(__name__)

# 注入预算：top_k 片段、每片段截断、总块限幅（context_memory 另有总限幅）
DEFAULT_TOP_K = 3
_SNIPPET_CHARS = 800
_CONTEXT_CHARS = 2400


_CJK_RUN = re.compile(r"[\u4e00-\u9fff]")


def _tokenize(text: str) -> Counter:
    """中英混合分词：ASCII 按词；中文（无空格分隔）按二元组切，保证召回。

    经验库同款简单分词对整句中文只会产出单个巨 token（查询与文档几乎不可能
    恰好相同），知识库面向中文文档，改用 CJK bigram 折中召回与精度。
    """
    tokens: Counter = Counter()
    for word in re.findall(r"\w+", text.lower()):
        if _CJK_RUN.search(word):
            if len(word) == 1:
                tokens[word] += 1
            else:
                for i in range(len(word) - 1):
                    tokens[word[i:i + 2]] += 1
        else:
            tokens[word] += 1
    return tokens


def _cosine(a: Counter, b: Counter) -> float:
    common = set(a.keys()) & set(b.keys())
    if not common:
        return 0.0
    dot = sum(a[k] * b[k] for k in common)
    na = sum(v * v for v in a.values()) ** 0.5
    nb = sum(v * v for v in b.values()) ** 0.5
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


def _chunk_text(content: str, chunk_chars: int = 600) -> list[str]:
    """按段落聚合切块：优先在换行处断开，超长段落硬切。"""
    chunks: list[str] = []
    for para in re.split(r"\n\s*\n", content):
        para = para.strip()
        if not para:
            continue
        while len(para) > chunk_chars:
            cut = para.rfind("\n", 0, chunk_chars)
            cut = cut if cut > chunk_chars // 2 else chunk_chars
            chunks.append(para[:cut].strip())
            para = para[cut:].strip()
        if para:
            chunks.append(para)
    return chunks or ([content[:chunk_chars]] if content else [])


class KnowledgeStore:
    """线程安全的文档知识库（JSON 持久化，原子写；检索片段注入派发上下文）。"""

    def __init__(self, knowledge_dir: str, embedding_model=None,
                 chunk_chars: int = 600):
        self._dir = Path(knowledge_dir)
        self._dir.mkdir(parents=True, exist_ok=True)
        self.embedding_model = embedding_model
        self._chunk_chars = chunk_chars
        self._lock = threading.Lock()
        self._query_cache: dict[str, list[float]] = {}

    # ---- 存储 ----

    def _path(self, doc_id: str) -> Path:
        if not re.fullmatch(r"doc-[0-9a-f]{12}", doc_id):
            raise KeyError(f"doc_id 非法: {doc_id!r}")
        return self._dir / f"{doc_id}.json"

    def _dump(self, doc: dict) -> None:
        p = self._path(doc["doc_id"])
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, p)

    # ---- CRUD ----

    def add(self, title: str, content: str) -> dict:
        """上传文档：切块 + 预计算向量，返回摘要视图。"""
        title = (title or "").strip() or "未命名文档"
        content = content or ""
        doc = {
            "doc_id": f"doc-{uuid.uuid4().hex[:12]}",
            "title": title,
            "content": content,
            "chunks": _chunk_text(content, self._chunk_chars),
            "chunk_embeddings": None,
            "created_at": time.time(),
            "updated_at": time.time(),
        }
        self._embed_chunks(doc)
        with self._lock:
            self._dump(doc)
        return self._summary(doc)

    def update(self, doc_id: str, title: str | None = None,
               content: str | None = None) -> dict:
        doc = self.get(doc_id, full=True)
        if doc is None:
            raise KeyError(f"知识文档不存在: {doc_id}")
        if title is not None:
            doc["title"] = title.strip() or doc["title"]
        if content is not None and content != doc["content"]:
            doc["content"] = content
            doc["chunks"] = _chunk_text(content, self._chunk_chars)
            doc["chunk_embeddings"] = None
            self._embed_chunks(doc)
        doc["updated_at"] = time.time()
        with self._lock:
            self._dump(doc)
        return self._summary(doc)

    def delete(self, doc_id: str) -> bool:
        try:
            p = self._path(doc_id)
        except KeyError:
            return False
        if not p.exists():
            return False
        p.unlink()
        return True

    def get(self, doc_id: str, full: bool = False) -> dict | None:
        try:
            p = self._path(doc_id)
        except KeyError:
            return None
        if not p.exists():
            return None
        try:
            doc = json.loads(p.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError, OSError) as e:
            log.warning("知识文档 %s 解析失败: %s", p, e)
            return None
        return doc if full else self._summary(doc)

    def list_all(self) -> list[dict]:
        """文档摘要列表（不含正文，按更新时间倒序）。"""
        out = []
        for p in sorted(self._dir.glob("doc-*.json")):
            try:
                doc = json.loads(p.read_text(encoding="utf-8"))
            except Exception:  # noqa: BLE001 - 损坏文件跳过
                continue
            if isinstance(doc, dict):
                out.append(self._summary(doc))
        return sorted(out, key=lambda d: d.get("updated_at") or 0, reverse=True)

    def count(self) -> int:
        return len(list(self._dir.glob("doc-*.json")))

    # ---- 检索与注入 ----

    def search(self, query: str, top_k: int = DEFAULT_TOP_K) -> list[dict]:
        """检索相关片段：关键词余弦（+ 语义向量 RRF 融合，embedding 可用时）。

        返回 [{doc_id, title, snippet, score}]，score 仅排序用不承诺口径。
        """
        query = (query or "").strip()
        if not query:
            return []
        q_tokens = _tokenize(query)
        if not q_tokens:
            return []
        q_vec = self._query_vec(query)

        kw_scored: list[tuple[float, dict]] = []
        vec_scored: list[tuple[float, dict]] = []
        for p in sorted(self._dir.glob("doc-*.json")):
            try:
                doc = json.loads(p.read_text(encoding="utf-8"))
            except Exception:  # noqa: BLE001
                continue
            if not isinstance(doc, dict):
                continue
            title = doc.get("title") or ""
            chunks = doc.get("chunks") or []
            embeddings = doc.get("chunk_embeddings") or []
            for i, chunk in enumerate(chunks):
                text = f"{title} {chunk}"
                item = {"doc_id": doc.get("doc_id", p.stem), "title": title,
                        "snippet": chunk[:_SNIPPET_CHARS]}
                score = _cosine(q_tokens, _tokenize(text))
                if score > 0:
                    kw_scored.append((score, item))
                if q_vec is not None and i < len(embeddings) \
                        and embeddings[i] is not None:
                    vscore = cosine_similarity_vec(q_vec, embeddings[i])
                    if vscore > 0:
                        vec_scored.append((vscore, item))

        def _fuse(scored_lists):
            # RRF：对分数尺度不敏感的排名融合（与 experience_store 同策略）
            k = 60
            rank: dict[str, tuple[float, dict]] = {}
            for ranked in scored_lists:
                ranked.sort(key=lambda x: x[0], reverse=True)
                for r, (_, item) in enumerate(ranked):
                    key = f"{item['doc_id']}#{item['snippet'][:32]}"
                    old = rank.get(key, (0.0, item))[0]
                    rank[key] = (old + 1.0 / (k + r + 1), item)
            fused = sorted(rank.values(), key=lambda x: x[0], reverse=True)
            return [item for _, item in fused]

        if vec_scored and kw_scored:
            results = _fuse([kw_scored, vec_scored])
        elif kw_scored:
            kw_scored.sort(key=lambda x: x[0], reverse=True)
            results = [item for _, item in kw_scored]
        else:
            results = [item for _, item in vec_scored]
        return results[:max(1, top_k)]

    def context_block(self, query: str, top_k: int = DEFAULT_TOP_K) -> str:
        """把检索片段收敛为注入 context_memory 的文本块（空库/无命中返回空）。"""
        hits = self.search(query, top_k=top_k)
        if not hits:
            return ""
        parts = [f"[{h['title']}] {h['snippet']}" for h in hits]
        block = "\n\n".join(parts)
        if len(block) > _CONTEXT_CHARS:
            block = block[:_CONTEXT_CHARS] + "…"
        return f"【知识库参考（自动检索，按需取用）】\n{block}"

    # ---- 内部 ----

    def _query_vec(self, query: str) -> list[float] | None:
        if self.embedding_model is None:
            return None
        vec = self._query_cache.get(query)
        if vec is None:
            vec = self.embedding_model.encode(query)
            self._query_cache[query] = vec
        return vec

    def _embed_chunks(self, doc: dict) -> None:
        if self.embedding_model is None:
            return
        try:
            doc["chunk_embeddings"] = [
                self.embedding_model.encode(f"{doc['title']} {c}")
                for c in doc["chunks"]]
        except Exception as e:  # noqa: BLE001 - 向量失败降级纯关键词
            log.warning("知识文档 %s 向量计算失败（降级关键词检索）: %s",
                        doc.get("doc_id"), e)
            doc["chunk_embeddings"] = None

    @staticmethod
    def _summary(doc: dict) -> dict:
        return {
            "doc_id": doc.get("doc_id"),
            "title": doc.get("title") or "",
            "chunk_count": len(doc.get("chunks") or []),
            "content_chars": len(doc.get("content") or ""),
            "created_at": doc.get("created_at"),
            "updated_at": doc.get("updated_at"),
        }
