"""Unit tests for document loading/saving/chunking — no external services."""

from __future__ import annotations

from pathlib import Path

from memex.core.config import ChunkingConfig
from memex.core.document import (
    chunk_document,
    ensure_frontmatter,
    load_document,
    save_document,
)


def test_ensure_frontmatter_fills_required_fields(tmp_path: Path):
    p = tmp_path / "note.md"
    p.write_text("# Hello\n\nA paragraph.\n")
    doc = load_document(p)
    changed = ensure_frontmatter(doc)
    assert changed is True
    assert doc.id  # ULID generated
    assert doc.title == "Hello"
    assert doc.meta["content_hash"].startswith("sha256:")
    assert doc.tags == []
    save_document(doc)

    # Loading the saved file gives back the same id and hash.
    doc2 = load_document(p)
    assert doc2.id == doc.id
    assert doc2.content_hash == doc.content_hash

    # ensure_frontmatter is idempotent.
    changed2 = ensure_frontmatter(doc2)
    assert changed2 is False


def test_ensure_frontmatter_bumps_updated_on_body_change(tmp_path: Path):
    p = tmp_path / "n.md"
    p.write_text("# T\n\nx\n")
    doc = load_document(p)
    ensure_frontmatter(doc)
    first_hash = doc.content_hash
    first_updated = doc.updated

    doc.body = "# T\n\nx and more\n"
    changed = ensure_frontmatter(doc)
    assert changed is True
    assert doc.content_hash != first_hash
    assert doc.updated >= first_updated


def test_chunking_respects_headings_and_code_fences(tmp_path: Path):
    body = """\
# Top

intro

## Section A

aaaa

```python
# this looks like a heading inside a fence
def x():
    return 1
```

## Section B

bbbb

### B.1

cccc
"""
    p = tmp_path / "d.md"
    p.write_text(body)
    doc = load_document(p)
    ensure_frontmatter(doc)
    cfg = ChunkingConfig(target_tokens=10000, overlap_tokens=0, min_chunk_tokens=1)
    chunks = chunk_document(doc, cfg)
    assert len(chunks) >= 1
    big_text = "\n".join(c.text for c in chunks)
    assert "def x():" in big_text
    assert "Section A" in big_text
    assert "Section B" in big_text


def test_chunking_splits_when_over_budget(tmp_path: Path):
    sections = [f"## H{i}\n\n" + ("token " * 200) for i in range(5)]
    body = "# Top\n\nintro\n\n" + "\n\n".join(sections)
    p = tmp_path / "big.md"
    p.write_text(body)
    doc = load_document(p)
    ensure_frontmatter(doc)
    cfg = ChunkingConfig(target_tokens=150, overlap_tokens=20, min_chunk_tokens=20)
    chunks = chunk_document(doc, cfg)
    assert len(chunks) >= 3
    # Every chunk must carry a stable id with the doc id prefix.
    for c in chunks:
        assert c.chunk_id.startswith(f"{doc.id}#")
        assert c.doc_id == doc.id
