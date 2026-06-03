"""Wrapper around mem0 OSS (library mode).

mem0's defaults already give us a local Qdrant + SQLite history under
~/.mem0; we override paths to keep everything inside the memex root for clean
per-root isolation and backup.

Categories live in metadata as `category`. We expose them in the CLI but
mem0 itself remains free to organize/dedupe memories its own way.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from memex.core.config import Config

ALLOWED_CATEGORIES = {"profile", "pref", "project", "decision", "learning", "fact"}
PROFILE_CATEGORIES = ("profile", "pref")

# `mem ls` / `mem search` tables show only the last 12 characters of each id
# for compactness; users paste that suffix into `mem rm` / `mem show`. mem0
# expects the canonical id, so we map suffix → full id when unambiguous.
_MIN_SUFFIX_LEN = 8

_NOT_FOUND_HINT = (
    "Run `memex mem ls --json` (or `memex client mem ls --json` against a "
    "remote server) to see full ids."
)


def resolve_memory_ref(ref: str, memory_ids: list[str]) -> str:
    """Resolve a user-supplied memory reference to the canonical id mem0 stores.

    Accepts:
      - the full id (exact match against ``memory_ids``); or
      - a unique suffix of at least :data:`_MIN_SUFFIX_LEN` characters in
        the hex/dash form printed by ``mem ls`` (e.g. ``c57ed1036c5a``).

    Raises ``KeyError`` when no candidate matches and ``ValueError`` when a
    suffix matches more than one stored id.
    """
    ref = (ref or "").strip()
    if not ref:
        raise KeyError(f"Empty memory id. {_NOT_FOUND_HINT}")
    if ref in memory_ids:
        return ref
    if len(ref) < _MIN_SUFFIX_LEN:
        raise KeyError(f"Memory with id {ref!r} not found. {_NOT_FOUND_HINT}")
    ref_norm = ref.replace("-", "")
    candidates = [
        mid
        for mid in memory_ids
        if mid.endswith(ref) or mid.replace("-", "").endswith(ref_norm)
    ]
    if len(candidates) == 1:
        return candidates[0]
    if not candidates:
        raise KeyError(f"Memory with id {ref!r} not found. {_NOT_FOUND_HINT}")
    raise ValueError(
        f"Ambiguous memory id {ref!r}: {len(candidates)} matches. "
        "Use a longer suffix or the full id from `mem ls --json`."
    )


@dataclass
class MemoryItem:
    id: str
    text: str
    category: str
    score: float = 0.0
    metadata: dict[str, Any] | None = None


class MemStore:
    """mem0 OSS library wrapper, scoped to one user_id and one memex root."""

    def __init__(self, cfg: Config):
        import threading

        self.cfg = cfg
        self.cfg.mem0_dir.mkdir(parents=True, exist_ok=True)
        self._memory = None  # lazy
        self._memory_lock = threading.Lock()
        self._atexit_registered = False

    @property
    def memory(self):
        # `memex ctx` calls `mem.list()` and `mem.search()` from a ThreadPool. If
        # both threads find `self._memory is None`, they each call _build()
        # and each tries to open the same local qdrant folder → the second
        # one raises "Storage folder ... is already accessed". Lock the
        # lazy-build path so we end up with exactly one mem0 instance per
        # MemStore.
        if self._memory is None:
            with self._memory_lock:
                if self._memory is None:
                    self._memory = self._build()
                    # qdrant-client's __del__ runs at interpreter shutdown, by
                    # which time sys.meta_path is None and the lock can't be
                    # released. Hook an explicit close earlier so the next
                    # process in the same shell can re-open the same local
                    # qdrant.
                    if not self._atexit_registered:
                        import atexit

                        atexit.register(self.close)
                        self._atexit_registered = True
        return self._memory

    def close(self) -> None:
        """Release mem0 / qdrant resources (in particular: the local file lock).

        Safe to call multiple times. Logs are swallowed so a partial-init
        leftover never crashes the host process at exit.
        """
        if self._memory is None:
            return
        try:
            inner = getattr(self._memory, "vector_store", None) or getattr(
                self._memory, "_vector_store", None
            )
            client = getattr(inner, "client", None) if inner is not None else None
            if client is not None and hasattr(client, "close"):
                client.close()
        except Exception:
            pass
        self._memory = None

    def _expected_embedding_dims(self) -> int:
        """Return the dimensionality the configured embedder will emit.

        Priority:
          1. explicit `embedder.dims` in memex.yaml
          2. provider+model lookup (covers OpenAI + the common HF small models)
          3. safe fallbacks: openai → 1536, anything HF-ish → 384
        """
        if self.cfg.embedder.dims:
            return int(self.cfg.embedder.dims)
        provider = (self.cfg.embedder.provider or "openai").lower()
        model = (self.cfg.embedder.model or "").lower()

        if provider == "openai":
            if "text-embedding-3-large" in model:
                return 3072
            if "text-embedding-3-small" in model or "ada-002" in model or not model:
                return 1536
            return 1536  # safe OpenAI fallback

        # HF / sentence-transformers / chromadb default — all small models we'd
        # realistically default to are 384 or 768 dim.
        if "mpnet" in model:
            return 768
        if "bge-large" in model:
            return 1024
        if "bge-base" in model:
            return 768
        if "bge-small" in model:
            return 384
        # all-MiniLM-L6-v2 (the default for chroma-default + our HF fallback)
        # and all-MiniLM-L12-v2 both produce 384-dim vectors.
        return 384

    def _build(self):
        """Lazy import + configure mem0. We let mem0 use its built-in defaults
        for LLM + embedder + vector store and only override storage paths."""
        import os

        # mem0 spins up a SECOND qdrant collection (~/.mem0/migrations_qdrant)
        # purely for telemetry/migration bookkeeping. That extra collection
        # holds its own file lock, so two MemStore instances in the same
        # process (parallel threads, sequential pytest cases, etc.) collide on
        # it even when their `mem0_dir` is different. Disable telemetry by
        # default — it's the right privacy posture for a personal tool, and
        # removes the contention entirely. Users who want it on can set
        # MEM0_TELEMETRY=True in their shell.
        os.environ.setdefault("MEM0_TELEMETRY", "False")

        from mem0 import Memory

        # mem0's Qdrant collection is created with a fixed dimensionality on
        # first use. If we don't tell it which dim to expect, it defaults to
        # 1536 (OpenAI text-embedding-3-small), which silently breaks the
        # moment a HF MiniLM (384-dim) embedding is written. Compute the right
        # dim here and pin it.
        embed_dims = self._expected_embedding_dims()

        config: dict[str, Any] = {
            "vector_store": {
                "provider": "qdrant",
                "config": {
                    "collection_name": "kb_mem",
                    "path": str(self.cfg.mem0_dir / "qdrant"),
                    "on_disk": True,
                    "embedding_model_dims": embed_dims,
                },
            },
            "history_db_path": str(self.cfg.mem0_dir / "history.db"),
        }
        ep = (self.cfg.embedder.provider or "openai").lower()
        if ep == "openai":
            emb_cfg: dict[str, Any] = {
                "model": self.cfg.embedder.model,
                "embedding_dims": embed_dims,
            }
            if self.cfg.embedder.base_url:
                emb_cfg["openai_base_url"] = self.cfg.embedder.base_url
            if self.cfg.embedder.api_key:
                emb_cfg["api_key"] = self.cfg.embedder.api_key
            config["embedder"] = {"provider": "openai", "config": emb_cfg}
        elif ep in {"sentence-transformers", "st", "local"}:
            config["embedder"] = {
                "provider": "huggingface",
                "config": {
                    "model": self.cfg.embedder.model,
                    "embedding_dims": embed_dims,
                },
            }
        elif ep in {"chroma-default", "chroma", "default", "onnx"}:
            # mem0 has no chroma-default backend; HF MiniLM is the closest
            # offline-friendly equivalent and matches dim with chromadb default.
            config["embedder"] = {
                "provider": "huggingface",
                "config": {
                    "model": self.cfg.embedder.model or "all-MiniLM-L6-v2",
                    "embedding_dims": embed_dims,
                },
            }

        if self.cfg.llm.provider == "openai":
            llm_cfg: dict[str, Any] = {
                "model": self.cfg.llm.model,
                "temperature": self.cfg.llm.temperature,
            }
            if self.cfg.llm.base_url:
                llm_cfg["openai_base_url"] = self.cfg.llm.base_url
            if self.cfg.llm.api_key:
                llm_cfg["api_key"] = self.cfg.llm.api_key
            config["llm"] = {"provider": "openai", "config": llm_cfg}
        elif self.cfg.llm.provider == "ollama":
            ollama_cfg: dict[str, Any] = {
                "model": self.cfg.llm.model,
                "temperature": self.cfg.llm.temperature,
            }
            if self.cfg.llm.base_url:
                ollama_cfg["ollama_base_url"] = self.cfg.llm.base_url
            config["llm"] = {"provider": "ollama", "config": ollama_cfg}

        return Memory.from_config(config)

    # ------------------------------------------------------------------
    # CRUD
    # ------------------------------------------------------------------

    def add(
        self,
        text: str,
        *,
        category: str = "fact",
        metadata: dict | None = None,
        infer: bool = False,
    ) -> list[str]:
        """Store a fact.

        `infer=False` (default): the text goes in verbatim — one input → one
        memory, no LLM call, category metadata preserved exactly as supplied.
        This matches the user's expectation when they explicitly say
        `mem add "X" --category pref`.

        `infer=True`: hand the text to mem0's LLM-driven fact extractor; it may
        split, merge, or dedupe across existing memories. Use for transcripts /
        free-form notes — see `MemStore.learn()` which forces `infer=True`.
        """
        category = _normalize_category(category)
        meta = dict(metadata or {})
        meta["category"] = category
        res = self.memory.add(
            text, user_id=self.cfg.user_id, metadata=meta, infer=infer
        )
        return _extract_ids(res)

    def learn(self, text: str, *, category: str = "learning") -> list[str]:
        """Run mem0's LLM-driven fact extraction over a chunk of text.

        Unlike `add()`, this always uses `infer=True` so mem0 will split the
        input into individual facts, dedupe against existing memories, and may
        update/merge older entries. Suitable for chat transcripts, meeting
        notes, or any free-form input where you want the LLM to decide what's
        worth keeping.
        """
        return self.add(text, category=category, infer=True)

    def update(self, mem_id: str, text: str) -> None:
        self.memory.update(memory_id=mem_id, data=text)

    def delete(self, mem_id: str) -> None:
        # Fast path: if `mem_id` is already the canonical id mem0 stores, let
        # mem0 handle it directly. We only pay the O(N) cost of listing every
        # memory when the fast path fails, i.e. the user pasted a short
        # suffix from `mem ls` and we need to resolve it.
        if self._try_delete_direct(mem_id):
            return
        ids = self._all_memory_ids()
        canon = resolve_memory_ref(mem_id, ids)
        self.memory.delete(memory_id=canon)

    def delete_all(self) -> None:
        self.memory.delete_all(user_id=self.cfg.user_id)

    def get(self, mem_id: str) -> MemoryItem | None:
        # Fast path: try the canonical lookup first. Only fall back to the
        # suffix-resolution path (which costs an O(N) listing) when mem0
        # returns nothing.
        obj = self._try_get_direct(mem_id)
        if obj is not None:
            return obj
        try:
            ids = self._all_memory_ids()
            canon = resolve_memory_ref(mem_id, ids)
        except KeyError:
            return None
        # ValueError (ambiguous suffix) deliberately propagates: the API and
        # CLI layers map it to 409 Conflict / exit-1 with an actionable hint.
        obj = self._try_get_direct(canon)
        return obj

    def _all_memory_ids(self) -> list[str]:
        return [m.id for m in self.list(category=None)]

    def _try_delete_direct(self, mem_id: str) -> bool:
        """Best-effort canonical delete. Returns True on success.

        mem0's `Memory.delete(memory_id=X)` raises
        ``ValueError("Memory with id X not found")`` when the id is unknown.
        We swallow only that case and let the caller fall back to suffix
        resolution. Any other exception (qdrant down, malformed config,
        etc.) propagates — the fallback path would just hit the same
        failure and obscure the real cause.
        """
        try:
            self.memory.delete(memory_id=mem_id)
            return True
        except ValueError:
            return False

    def _try_get_direct(self, mem_id: str) -> MemoryItem | None:
        # mem0.get returns None (no raise) for unknown ids; broad except
        # only matters for transient backend errors, in which case returning
        # None lets the resolution path retry against the listing.
        try:
            obj = self.memory.get(memory_id=mem_id)
        except Exception:
            return None
        if not obj or not isinstance(obj, dict):
            return None
        return _to_item(obj)

    def list(self, *, category: str | None = None) -> list[MemoryItem]:
        # mem0 ≥ 0.1.130 requires filters={...}; earlier versions accepted
        # top-level user_id=. Try the new API first and fall back gracefully.
        try:
            res = self.memory.get_all(filters={"user_id": self.cfg.user_id}, top_k=200)
        except TypeError:
            res = self.memory.get_all(user_id=self.cfg.user_id)
        items = _to_items(res)
        if category:
            cat = _normalize_category(category)
            items = [m for m in items if (m.metadata or {}).get("category") == cat]
        return items

    def search(
        self, query: str, *, top_k: int = 5, category: str | None = None
    ) -> list[MemoryItem]:
        try:
            res = self.memory.search(
                query=query,
                filters={"user_id": self.cfg.user_id},
                top_k=top_k * 3,
            )
        except TypeError:
            res = self.memory.search(query=query, user_id=self.cfg.user_id, limit=top_k * 3)
        items = _to_items(res)
        if category:
            cat = _normalize_category(category)
            items = [m for m in items if (m.metadata or {}).get("category") == cat]
        return items[:top_k]


# ---------------------------------------------------------------------------
# mem0 result shape adapters: the SDK has changed shape across versions
# ---------------------------------------------------------------------------


def _normalize_category(c: str) -> str:
    c = (c or "fact").lower().strip()
    if c not in ALLOWED_CATEGORIES:
        return "fact"
    return c


def _extract_ids(res: Any) -> list[str]:
    """mem0 .add() returns either {'results': [...]} or a list of dicts."""
    if res is None:
        return []
    if isinstance(res, dict) and "results" in res:
        return [str(r.get("id")) for r in res["results"] if r.get("id")]
    if isinstance(res, list):
        return [str(r.get("id")) for r in res if isinstance(r, dict) and r.get("id")]
    return []


def _to_items(res: Any) -> list[MemoryItem]:
    if res is None:
        return []
    rows: list[dict]
    if isinstance(res, dict) and "results" in res:
        rows = res["results"]
    elif isinstance(res, list):
        rows = res
    else:
        return []
    return [_to_item(r) for r in rows if isinstance(r, dict)]


def _to_item(row: dict) -> MemoryItem:
    text = row.get("memory") or row.get("text") or row.get("data") or ""
    metadata = row.get("metadata") or {}
    category = str(metadata.get("category", "fact"))
    score = float(row.get("score", 0.0) or 0.0)
    return MemoryItem(
        id=str(row.get("id", "")),
        text=str(text),
        category=category,
        score=score,
        metadata=metadata,
    )
