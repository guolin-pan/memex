"""Frontmatter + chunking for markdown wiki documents.

Each document has a stable ULID id stored in its frontmatter. That id is the
primary key in ChromaDB and in cross-document links. The on-disk path is just a
human-friendly location and can change freely.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import frontmatter
from ulid import ULID

from memex.core.config import ChunkingConfig
from memex.core.utils import content_hash, count_tokens, now_iso, slugify

FRONTMATTER_DEFAULTS: dict[str, Any] = {
    "id": "",
    "title": "",
    "tags": [],
    "created": "",
    "updated": "",
    "source": "manual",
    "content_hash": "",
    "links": [],
}


@dataclass
class Chunk:
    """A retrievable unit of a document."""

    chunk_id: str  # "{doc_id}#{heading_slug}#{ord}"
    doc_id: str
    text: str
    heading: str  # human-readable heading path: "Section / Subsection"
    ord: int
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class Document:
    """A parsed markdown document."""

    path: Path
    meta: dict[str, Any]
    body: str  # body without frontmatter

    @property
    def id(self) -> str:
        return str(self.meta.get("id", ""))

    @property
    def title(self) -> str:
        return str(self.meta.get("title") or self.path.stem)

    @property
    def tags(self) -> list[str]:
        t = self.meta.get("tags") or []
        return [str(x) for x in t]

    @property
    def updated(self) -> str:
        return str(self.meta.get("updated") or "")

    @property
    def content_hash(self) -> str:
        return str(self.meta.get("content_hash") or "")

    @property
    def source(self) -> str:
        return str(self.meta.get("source") or "manual")

    def to_text(self) -> str:
        """Serialize back to a frontmatter+body string."""
        post = frontmatter.Post(self.body, **self.meta)
        return frontmatter.dumps(post) + "\n"


# ---------------------------------------------------------------------------
# Loading / saving
# ---------------------------------------------------------------------------


def load_document(path: Path) -> Document:
    text = path.read_text(encoding="utf-8")
    post = frontmatter.loads(text)
    meta = dict(post.metadata or {})
    return Document(path=path, meta=meta, body=post.content)


def save_document(doc: Document) -> None:
    doc.path.parent.mkdir(parents=True, exist_ok=True)
    doc.path.write_text(doc.to_text(), encoding="utf-8")


def ensure_frontmatter(
    doc: Document,
    *,
    default_title: str | None = None,
    default_tags: list[str] | None = None,
    source: str | None = None,
) -> bool:
    """Fill in missing required frontmatter fields. Returns True if changed."""
    changed = False
    if not doc.meta.get("id"):
        doc.meta["id"] = str(ULID())
        changed = True
    if not doc.meta.get("title"):
        doc.meta["title"] = default_title or _infer_title(doc.body, doc.path)
        changed = True
    if "tags" not in doc.meta or doc.meta["tags"] is None:
        doc.meta["tags"] = list(default_tags or [])
        changed = True
    if not doc.meta.get("created"):
        doc.meta["created"] = now_iso()
        changed = True
    if not doc.meta.get("updated"):
        doc.meta["updated"] = doc.meta["created"]
        changed = True
    if not doc.meta.get("source") and source:
        doc.meta["source"] = source
        changed = True
    elif "source" not in doc.meta:
        doc.meta["source"] = "manual"
        changed = True
    if "links" not in doc.meta or doc.meta["links"] is None:
        doc.meta["links"] = []
        changed = True

    new_hash = content_hash(doc.body)
    if doc.meta.get("content_hash") != new_hash:
        doc.meta["content_hash"] = new_hash
        doc.meta["updated"] = now_iso()
        changed = True

    return changed


_H1_RE = re.compile(r"^\s*#\s+(.+?)\s*$", re.MULTILINE)


def _infer_title(body: str, path: Path) -> str:
    m = _H1_RE.search(body)
    if m:
        return m.group(1).strip()
    return path.stem.replace("-", " ").replace("_", " ").title()


# ---------------------------------------------------------------------------
# Chunking
# ---------------------------------------------------------------------------

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$", re.MULTILINE)
_CODE_FENCE_RE = re.compile(r"^```", re.MULTILINE)


def _strip_code_fence_markers(s: str) -> bool:
    """Return True if the count of code fences is odd (we're inside one)."""
    return len(_CODE_FENCE_RE.findall(s)) % 2 == 1


def _split_on_headings(body: str, max_level: int = 3) -> list[tuple[str, str]]:
    """Split markdown into [(heading_path, section_text), ...] respecting fences.

    Code fences are treated as atomic; headings inside fenced blocks are ignored.
    """
    sections: list[tuple[str, str]] = []
    lines = body.splitlines(keepends=True)
    current_heading_stack: list[tuple[int, str]] = []
    buf: list[str] = []
    in_fence = False
    section_heading = ""

    for line in lines:
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
            buf.append(line)
            continue
        if in_fence:
            buf.append(line)
            continue

        m = _HEADING_RE.match(line)
        if m and len(m.group(1)) <= max_level:
            if buf:
                sections.append((section_heading, "".join(buf).strip()))
                buf = []
            level = len(m.group(1))
            title = m.group(2).strip()
            current_heading_stack = [h for h in current_heading_stack if h[0] < level]
            current_heading_stack.append((level, title))
            section_heading = " / ".join(t for _, t in current_heading_stack)
            buf.append(line)
        else:
            buf.append(line)

    if buf:
        sections.append((section_heading, "".join(buf).strip()))

    return [(h, t) for h, t in sections if t.strip()]


def _pack_chunks(
    sections: list[tuple[str, str]],
    target_tokens: int,
    overlap_tokens: int,
    min_chunk_tokens: int,
) -> list[tuple[str, str]]:
    """Greedily pack sections into chunks <= target_tokens, never splitting code blocks."""
    packed: list[tuple[str, str]] = []
    buf_heading = ""
    buf_text = ""
    buf_tokens = 0

    def flush():
        nonlocal buf_text, buf_tokens, buf_heading
        if buf_text.strip():
            packed.append((buf_heading or "(root)", buf_text.strip()))
        buf_text = ""
        buf_tokens = 0
        buf_heading = ""

    for heading, text in sections:
        n = count_tokens(text)

        if n > target_tokens:
            if buf_text:
                flush()
            packed.extend(_hard_split(heading, text, target_tokens, overlap_tokens))
            continue

        if buf_tokens + n > target_tokens and buf_tokens >= min_chunk_tokens:
            flush()

        if not buf_heading:
            buf_heading = heading or "(root)"
        elif heading and heading != buf_heading:
            buf_heading = f"{buf_heading}; {heading}"

        buf_text = (buf_text + "\n\n" + text).strip() if buf_text else text
        buf_tokens += n

    flush()
    return packed


def _hard_split(
    heading: str, text: str, target_tokens: int, overlap_tokens: int
) -> list[tuple[str, str]]:
    """Last-resort splitter for oversized sections: split on blank lines, never inside fences."""
    paragraphs = _split_paragraphs(text)
    out: list[tuple[str, str]] = []
    buf: list[str] = []
    buf_tokens = 0
    for para in paragraphs:
        ptoks = count_tokens(para)
        if buf_tokens + ptoks > target_tokens and buf:
            out.append((heading or "(root)", "\n\n".join(buf).strip()))
            tail = buf[-1] if overlap_tokens > 0 else ""
            buf = [tail] if tail else []
            buf_tokens = count_tokens(tail) if tail else 0
        buf.append(para)
        buf_tokens += ptoks
    if buf:
        out.append((heading or "(root)", "\n\n".join(buf).strip()))
    return out


def _split_paragraphs(text: str) -> list[str]:
    """Split on blank lines, but keep fenced code blocks atomic."""
    paragraphs: list[str] = []
    cur: list[str] = []
    in_fence = False
    for line in text.splitlines():
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
            cur.append(line)
            continue
        if not in_fence and not line.strip():
            if cur:
                paragraphs.append("\n".join(cur).strip())
                cur = []
        else:
            cur.append(line)
    if cur:
        paragraphs.append("\n".join(cur).strip())
    return [p for p in paragraphs if p]


def chunk_document(doc: Document, cfg: ChunkingConfig) -> list[Chunk]:
    """Split a document into retrievable chunks."""
    max_level = 3
    if cfg.split_by_headings:
        max_level = max(int(h.lstrip("h")) for h in cfg.split_by_headings if h.startswith("h"))

    sections = _split_on_headings(doc.body, max_level=max_level)
    if not sections:
        sections = [("(root)", doc.body.strip())]

    packed = _pack_chunks(
        sections,
        target_tokens=cfg.target_tokens,
        overlap_tokens=cfg.overlap_tokens,
        min_chunk_tokens=cfg.min_chunk_tokens,
    )

    chunks: list[Chunk] = []
    for i, (heading, text) in enumerate(packed):
        h_slug = slugify(heading) or "root"
        chunk_id = f"{doc.id}#{h_slug}#{i}"
        chunks.append(
            Chunk(
                chunk_id=chunk_id,
                doc_id=doc.id,
                text=text,
                heading=heading,
                ord=i,
                metadata={
                    "doc_id": doc.id,
                    "title": doc.title,
                    "tags": ",".join(doc.tags),
                    "path": str(doc.path),
                    "heading": heading,
                    "updated": doc.updated,
                    "source": doc.source,
                    "ord": i,
                },
            )
        )
    return chunks
