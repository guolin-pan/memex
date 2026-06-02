"""Thin wrapper around ChromaDB for the wiki RAG layer.

We keep one collection per memex root, plus a tiny BM25 sidecar index that we
rebuild lazily from Chroma so hybrid retrieval works without a second store.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from rank_bm25 import BM25Okapi

from memex.backends.embeddings import build_embedder
from memex.core.config import Config
from memex.core.document import Chunk

COLLECTION_NAME = "wiki"


@dataclass
class SearchHit:
    chunk_id: str
    doc_id: str
    title: str
    path: str
    heading: str
    text: str
    score: float
    tags: list[str]
    updated: str


def _tokenize(text: str) -> list[str]:
    return [t for t in text.lower().split() if t]


class ChromaStore:
    """ChromaDB-backed chunk store with optional BM25 hybrid scoring."""

    def __init__(self, cfg: Config):
        import chromadb
        from chromadb.config import Settings

        self.cfg = cfg
        self._embedder = build_embedder(cfg.embedder)
        cfg.chroma_dir.mkdir(parents=True, exist_ok=True)
        self._client = chromadb.PersistentClient(
            path=str(cfg.chroma_dir),
            settings=Settings(anonymized_telemetry=False, allow_reset=True),
        )
        self._collection = self._client.get_or_create_collection(
            name=COLLECTION_NAME,
            embedding_function=self._embedder,
            metadata={"hnsw:space": "cosine", "embedder": self._embedder.name()},
        )
        self._bm25_cache: tuple[BM25Okapi, list[str], list[dict], list[str]] | None = None

    # ------------------------------------------------------------------
    # Mutation
    # ------------------------------------------------------------------

    def upsert_chunks(self, chunks: list[Chunk]) -> None:
        if not chunks:
            return
        self._collection.upsert(
            ids=[c.chunk_id for c in chunks],
            documents=[c.text for c in chunks],
            metadatas=[_clean_metadata(c.metadata) for c in chunks],
        )
        self._invalidate_bm25()

    def delete_doc(self, doc_id: str) -> int:
        """Delete every chunk that belongs to `doc_id`. Returns count removed."""
        existing = self._collection.get(where={"doc_id": doc_id})
        ids = existing.get("ids") or []
        if ids:
            self._collection.delete(ids=ids)
            self._invalidate_bm25()
        return len(ids)

    def replace_doc(self, doc_id: str, chunks: list[Chunk]) -> int:
        """Delete then re-insert. Returns count of new chunks written."""
        self.delete_doc(doc_id)
        self.upsert_chunks(chunks)
        return len(chunks)

    def reset(self) -> None:
        self._client.delete_collection(COLLECTION_NAME)
        self._collection = self._client.get_or_create_collection(
            name=COLLECTION_NAME,
            embedding_function=self._embedder,
            metadata={"hnsw:space": "cosine", "embedder": self._embedder.name()},
        )
        self._invalidate_bm25()

    # ------------------------------------------------------------------
    # Read
    # ------------------------------------------------------------------

    def count(self) -> int:
        return self._collection.count()

    def list_doc_ids(self) -> set[str]:
        out = self._collection.get(include=["metadatas"])
        return {m.get("doc_id") for m in (out.get("metadatas") or []) if m.get("doc_id")}

    def get_doc_meta(self, doc_id: str) -> dict | None:
        out = self._collection.get(where={"doc_id": doc_id}, limit=1, include=["metadatas"])
        metas = out.get("metadatas") or []
        return metas[0] if metas else None

    def search(
        self,
        query: str,
        *,
        top_k: int,
        hybrid_alpha: float = 0.5,
        where: dict | None = None,
    ) -> list[SearchHit]:
        if self.count() == 0 or not query.strip():
            return []

        n_vec = max(top_k * 3, top_k)
        vec = self._collection.query(
            query_texts=[query],
            n_results=n_vec,
            where=where or None,
            include=["documents", "metadatas", "distances"],
        )
        ids = (vec.get("ids") or [[]])[0]
        docs = (vec.get("documents") or [[]])[0]
        metas = (vec.get("metadatas") or [[]])[0]
        dists = (vec.get("distances") or [[]])[0]

        vec_hits: dict[str, tuple[float, str, dict]] = {}
        for cid, doc, meta, dist in zip(ids, docs, metas, dists, strict=False):
            score = max(0.0, 1.0 - float(dist))  # cosine distance → similarity
            vec_hits[cid] = (score, doc, meta or {})

        bm25_hits: dict[str, tuple[float, str, dict]] = {}
        if hybrid_alpha < 1.0:
            bm25_hits = self._bm25_search(query, where=where, top_k=n_vec)

        all_ids = set(vec_hits) | set(bm25_hits)
        results: list[SearchHit] = []
        for cid in all_ids:
            v_score = vec_hits[cid][0] if cid in vec_hits else 0.0
            b_score = bm25_hits[cid][0] if cid in bm25_hits else 0.0
            text = vec_hits.get(cid, bm25_hits.get(cid))[1]
            meta = vec_hits.get(cid, bm25_hits.get(cid))[2]
            combined = hybrid_alpha * v_score + (1.0 - hybrid_alpha) * b_score
            results.append(
                SearchHit(
                    chunk_id=cid,
                    doc_id=str(meta.get("doc_id", "")),
                    title=str(meta.get("title", "")),
                    path=str(meta.get("path", "")),
                    heading=str(meta.get("heading", "")),
                    text=text or "",
                    score=combined,
                    tags=_split_tags(meta.get("tags")),
                    updated=str(meta.get("updated", "")),
                )
            )
        results.sort(key=lambda h: h.score, reverse=True)
        return results[:top_k]

    # ------------------------------------------------------------------
    # BM25 sidecar (rebuilt lazily from Chroma)
    # ------------------------------------------------------------------

    def _build_bm25(self) -> tuple[BM25Okapi, list[str], list[dict], list[str]]:
        all_data = self._collection.get(include=["documents", "metadatas"])
        ids = all_data.get("ids") or []
        docs = all_data.get("documents") or []
        metas = all_data.get("metadatas") or []
        tokenized = [_tokenize(d or "") for d in docs]
        bm25 = BM25Okapi(tokenized) if tokenized else BM25Okapi([[""]])
        return bm25, ids, [m or {} for m in metas], docs

    def _bm25_search(
        self, query: str, *, where: dict | None, top_k: int
    ) -> dict[str, tuple[float, str, dict]]:
        if self._bm25_cache is None:
            self._bm25_cache = self._build_bm25()
        bm25, ids, metas, docs = self._bm25_cache
        if not ids:
            return {}
        q = _tokenize(query)
        if not q:
            return {}
        scores = bm25.get_scores(q)
        s_max = max(scores) if len(scores) else 1.0
        if s_max <= 0:
            return {}
        scaled = [s / s_max for s in scores]

        out: dict[str, tuple[float, str, dict]] = {}
        for cid, meta, doc, score in zip(ids, metas, docs, scaled, strict=False):
            if where and not _meta_matches(meta, where):
                continue
            if score <= 0:
                continue
            out[cid] = (float(score), doc or "", meta)
        ranked = sorted(out.items(), key=lambda kv: kv[1][0], reverse=True)[:top_k]
        return dict(ranked)

    def _invalidate_bm25(self) -> None:
        self._bm25_cache = None


def _clean_metadata(meta: dict[str, Any]) -> dict[str, Any]:
    """Chroma metadata values must be str/int/float/bool/None — no lists or dicts."""
    out: dict[str, Any] = {}
    for k, v in meta.items():
        if v is None or isinstance(v, (str, int, float, bool)):
            out[k] = v
        elif isinstance(v, list):
            out[k] = ",".join(str(x) for x in v)
        else:
            out[k] = str(v)
    return out


def _split_tags(value: Any) -> list[str]:
    if not value:
        return []
    if isinstance(value, list):
        return [str(x) for x in value]
    return [t.strip() for t in str(value).split(",") if t.strip()]


def _meta_matches(meta: dict, where: dict) -> bool:
    """Tiny subset of Chroma's `where` operators, for BM25 path filtering."""
    for k, cond in where.items():
        v = meta.get(k)
        if isinstance(cond, dict):
            for op, target in cond.items():
                if op == "$eq" and v != target or op == "$ne" and v == target or op == "$gte" and (v is None or v < target) or op == "$lte" and (v is None or v > target) or op == "$gt" and (v is None or v <= target) or op == "$lt" and (v is None or v >= target) or op == "$in" and v not in target:
                    return False
        else:
            if v != cond:
                return False
    return True


__all__ = ["ChromaStore", "SearchHit"]
