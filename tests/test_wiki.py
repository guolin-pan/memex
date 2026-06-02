"""End-to-end tests for the Wiki layer using the offline chroma-default embedder."""

from __future__ import annotations

from pathlib import Path

import pytest

from memex.core.config import Config
from memex.core.wiki import Wiki

pytestmark = pytest.mark.usefixtures("memex_root")


def _seed_wiki(cfg: Config) -> Wiki:
    wiki = Wiki(cfg)
    wiki.add(
        source_path=None,
        body="# Postgres tuning\n\n## work_mem\n\nKeeps sort operations in memory.\n",
        title="Postgres tuning",
        tags=["db", "reference"],
        target_subdir="reference",
    )
    wiki.add(
        source_path=None,
        body=(
            "# Project X stack\n\n"
            "## Backend\n\nPython, FastAPI, Postgres with pgvector.\n\n"
            "## Frontend\n\nReact + TypeScript.\n"
        ),
        title="Project X stack",
        tags=["project-x", "architecture"],
        target_subdir="projects/project-x",
    )
    return wiki


def test_add_search_and_remove(cfg: Config):
    wiki = _seed_wiki(cfg)

    hits = wiki.search("postgres work memory", top_k=3)
    assert hits, "expected at least one hit"
    titles = [h.title for h in hits]
    assert any("Postgres" in t for t in titles)

    project_hits = wiki.search("frontend react typescript", top_k=3)
    assert any("Project X" in h.title for h in project_hits)

    docs = wiki.list_docs()
    assert len(docs) == 2

    target = next(d for d in docs if "Postgres" in d.title)
    doc_id, removed_path = wiki.remove(target.id)
    assert doc_id == target.id
    assert removed_path is not None
    assert not Path(removed_path).exists()

    after = wiki.list_docs()
    assert len(after) == 1


def test_tag_and_since_filters(cfg: Config):
    wiki = _seed_wiki(cfg)

    only_db = wiki.list_docs(tag="db")
    assert len(only_db) == 1 and only_db[0].title == "Postgres tuning"

    no_match = wiki.list_docs(tag="nope")
    assert no_match == []

    hits = wiki.search("memory", top_k=5, tag="db")
    assert all("db" in h.tags for h in hits)


def test_update_path_reindexes(cfg: Config):
    wiki = _seed_wiki(cfg)
    doc = wiki.list_docs()[0]
    p = doc.path
    p.write_text(
        "---\n"
        f"id: {doc.id}\n"
        f"title: {doc.title}\n"
        "tags: [db, reference]\n"
        f"created: {doc.meta['created']}\n"
        f"updated: {doc.meta['updated']}\n"
        "source: manual\n"
        "content_hash: ''\n"
        "links: []\n"
        "---\n\n"
        "# New body about caching layers\n",
        encoding="utf-8",
    )
    updated = wiki.update_path(p)
    assert updated is not None
    hits = wiki.search("caching layers", top_k=3)
    assert any("caching" in h.text.lower() for h in hits)


def test_reindex_picks_up_external_changes(cfg: Config):
    wiki = _seed_wiki(cfg)
    new_doc = cfg.docs_dir / "learning" / "fastapi.md"
    new_doc.write_text("# FastAPI gotchas\n\nDependency injection scopes matter.\n")
    res = wiki.reindex(only_changed=True)
    assert any(p == new_doc for p in res.added)
    hits = wiki.search("dependency injection scopes", top_k=3)
    assert any("FastAPI" in h.title for h in hits)
