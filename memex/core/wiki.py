"""High-level wiki operations: scanning the docs folder, indexing, syncing.

This is the layer the CLI commands call. It hides the ChromaStore from the CLI.
"""

from __future__ import annotations

import fnmatch
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from memex.backends.chroma_store import ChromaStore, SearchHit
from memex.core.config import Config
from memex.core.document import (
    Document,
    chunk_document,
    ensure_frontmatter,
    load_document,
    save_document,
)
from memex.core.utils import now_iso, parse_since, to_datetime

MARKDOWN_EXTS = {".md", ".markdown"}
DEFAULT_IGNORE = (".git", ".cache", "node_modules", ".obsidian", ".trash")


@dataclass
class IndexResult:
    added: list[Path]
    updated: list[Path]
    skipped: list[Path]
    deleted: list[str]  # doc ids


class Wiki:
    """Coordinates the on-disk docs/ tree and the Chroma index."""

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self._store: ChromaStore | None = None

    @property
    def store(self) -> ChromaStore:
        if self._store is None:
            self._store = ChromaStore(self.cfg)
        return self._store

    # ------------------------------------------------------------------
    # Filesystem scanning
    # ------------------------------------------------------------------

    def iter_doc_paths(self) -> Iterable[Path]:
        if not self.cfg.docs_dir.exists():
            return
        ignore_patterns = list(DEFAULT_IGNORE) + _load_kbignore(self.cfg.kbignore_path)
        for p in sorted(self.cfg.docs_dir.rglob("*")):
            if not p.is_file() or p.suffix.lower() not in MARKDOWN_EXTS:
                continue
            rel = p.relative_to(self.cfg.docs_dir)
            if _is_ignored(rel, ignore_patterns):
                continue
            yield p

    # ------------------------------------------------------------------
    # CRUD
    # ------------------------------------------------------------------

    def add(
        self,
        *,
        source_path: Path | None,
        body: str | None,
        title: str | None,
        tags: list[str] | None,
        target_subdir: str = "inbox",
        source: str = "manual",
    ) -> Document:
        """Add a brand-new document into the wiki.

        Either `source_path` (an external file to copy) or `body` (raw markdown)
        must be provided.
        """
        if source_path is not None:
            content = source_path.read_text(encoding="utf-8")
            base_name = source_path.stem
        else:
            if body is None:
                raise ValueError("must provide either source_path or body")
            content = body
            base_name = title or "note"

        import frontmatter

        post = frontmatter.loads(content)
        meta = dict(post.metadata or {})
        body_text = post.content

        slug = _safe_slug(title or meta.get("title") or base_name)
        dest_dir = self.cfg.docs_dir / target_subdir
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = _unique_path(dest_dir / f"{slug}.md")

        doc = Document(path=dest, meta=meta, body=body_text)
        ensure_frontmatter(
            doc,
            default_title=title,
            default_tags=tags,
            source=source,
        )
        save_document(doc)
        self._index_one(doc)
        return doc

    def update_path(self, path: Path) -> Document | None:
        """Re-index a single file. Returns the updated doc, or None if ignored."""
        if not path.exists() or path.suffix.lower() not in MARKDOWN_EXTS:
            return None
        if not _is_under(path, self.cfg.docs_dir):
            return None
        doc = load_document(path)
        changed = ensure_frontmatter(doc)
        if changed:
            save_document(doc)
        self._index_one(doc)
        return doc

    def remove(self, ident: str, *, delete_file: bool = True) -> tuple[str, Path | None]:
        """Remove a document by id, slug, or path. Returns (doc_id, removed_path)."""
        doc = self._resolve(ident)
        if doc is None:
            removed = self.store.delete_doc(ident)
            if removed:
                self._tombstone(ident, None, reason="explicit-id")
                return ident, None
            raise FileNotFoundError(f"no document matches {ident!r}")
        self.store.delete_doc(doc.id)
        removed_path = doc.path
        if delete_file and doc.path.exists():
            doc.path.unlink()
        self._tombstone(doc.id, removed_path, reason="cli-rm")
        return doc.id, removed_path

    # ------------------------------------------------------------------
    # Sync / reindex
    # ------------------------------------------------------------------

    def reindex(self, *, only_changed: bool = True) -> IndexResult:
        """Walk docs/ and bring the Chroma index in sync.

        only_changed: re-embed only docs whose `content_hash` differs from the
                      currently-stored chunk metadata (cheap, default).
                      Pass False to force-rebuild everything.
        """
        result = IndexResult(added=[], updated=[], skipped=[], deleted=[])
        seen_ids: set[str] = set()
        store_ids = self.store.list_doc_ids()

        for path in self.iter_doc_paths():
            doc = load_document(path)
            mutated = ensure_frontmatter(doc)
            if mutated:
                save_document(doc)
            seen_ids.add(doc.id)

            stored_meta = self.store.get_doc_meta(doc.id)
            stored_hash = ""
            stored_updated = ""
            if stored_meta:
                stored_hash = str(stored_meta.get("content_hash") or "")
                stored_updated = str(stored_meta.get("updated") or "")

            current_hash = doc.content_hash
            if (
                only_changed
                and stored_meta is not None
                and (stored_hash == current_hash or stored_updated == doc.updated)
            ):
                result.skipped.append(path)
                continue

            chunks = chunk_document(doc, self.cfg.chunking)
            # Stamp current content_hash onto every chunk's metadata so reindex
            # diffing works without an extra DB.
            for c in chunks:
                c.metadata["content_hash"] = current_hash
            self.store.replace_doc(doc.id, chunks)
            if stored_meta is None:
                result.added.append(path)
            else:
                result.updated.append(path)

        stale = store_ids - seen_ids
        for doc_id in stale:
            self.store.delete_doc(doc_id)
            self._tombstone(doc_id, None, reason="reindex-missing-file")
            result.deleted.append(doc_id)
        return result

    # ------------------------------------------------------------------
    # Search / listing
    # ------------------------------------------------------------------

    def search(
        self,
        query: str,
        *,
        top_k: int | None = None,
        tag: str | None = None,
        since: str | None = None,
    ) -> list[SearchHit]:
        k = top_k or self.cfg.search.top_k_docs
        cutoff = parse_since(since)

        # tags are joined into a flat string in Chroma metadata, so we filter
        # post-hoc rather than relying on Chroma's `where` clause.
        hits = self.store.search(
            query, top_k=max(k * 2, k), hybrid_alpha=self.cfg.search.hybrid_alpha
        )

        filtered: list[SearchHit] = []
        for h in hits:
            if h.score < self.cfg.search.min_score:
                continue
            if tag and tag not in h.tags:
                continue
            if cutoff is not None:
                u = to_datetime(h.updated)
                if u is None or u < cutoff:
                    continue
            filtered.append(h)
        return filtered[:k]

    def list_docs(
        self,
        *,
        tag: str | None = None,
        since: str | None = None,
    ) -> list[Document]:
        cutoff = parse_since(since)
        out: list[Document] = []
        for path in self.iter_doc_paths():
            doc = load_document(path)
            if tag and tag not in doc.tags:
                continue
            if cutoff is not None:
                u = to_datetime(doc.updated)
                if u is None or u < cutoff:
                    continue
            out.append(doc)
        out.sort(key=lambda d: d.updated, reverse=True)
        return out

    def get(self, ident: str) -> Document | None:
        return self._resolve(ident)

    def graph_mermaid(self) -> str:
        docs = self.list_docs()
        if not docs:
            return "graph TD\n  empty[No documents]"
        by_id = {d.id: d for d in docs}
        lines = ["graph TD"]
        for d in docs:
            label = (d.title or d.path.stem).replace('"', "'")
            lines.append(f'  {d.id}["{label}"]')
        for d in docs:
            for link in d.meta.get("links") or []:
                if link in by_id:
                    lines.append(f"  {d.id} --> {link}")
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _index_one(self, doc: Document) -> None:
        chunks = chunk_document(doc, self.cfg.chunking)
        for c in chunks:
            c.metadata["content_hash"] = doc.content_hash
        self.store.replace_doc(doc.id, chunks)

    def _resolve(self, ident: str) -> Document | None:
        # Try as filesystem path first.
        as_path = Path(ident).expanduser()
        if as_path.exists() and as_path.suffix.lower() in MARKDOWN_EXTS:
            return load_document(as_path)

        # Then scan docs/ for matching id or slug.
        ident_lower = ident.lower()
        for path in self.iter_doc_paths():
            doc = load_document(path)
            if doc.id == ident:
                return doc
            if path.stem.lower() == ident_lower:
                return doc
        return None

    def _tombstone(self, doc_id: str, path: Path | None, *, reason: str) -> None:
        self.cfg.history_dir.mkdir(parents=True, exist_ok=True)
        log = self.cfg.history_dir / "tombstones.log"
        line = f"{now_iso()}\t{doc_id}\t{path or '-'}\t{reason}\n"
        with log.open("a", encoding="utf-8") as f:
            f.write(line)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _safe_slug(text: str) -> str:
    from memex.core.utils import slugify

    s = slugify(text or "note")
    return s or "note"


def _unique_path(p: Path) -> Path:
    if not p.exists():
        return p
    base = p.stem
    parent = p.parent
    i = 2
    while True:
        candidate = parent / f"{base}-{i}{p.suffix}"
        if not candidate.exists():
            return candidate
        i += 1


def _load_kbignore(path: Path) -> list[str]:
    if not path.exists():
        return []
    lines = path.read_text(encoding="utf-8").splitlines()
    return [ln.strip() for ln in lines if ln.strip() and not ln.strip().startswith("#")]


def _is_ignored(rel: Path, patterns: list[str]) -> bool:
    sp = str(rel).replace("\\", "/")
    parts = sp.split("/")
    for pat in patterns:
        if any(fnmatch.fnmatch(part, pat) for part in parts):
            return True
        if fnmatch.fnmatch(sp, pat):
            return True
    return False


def _is_under(p: Path, root: Path) -> bool:
    try:
        p.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False
