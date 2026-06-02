"""Tests for the mem0 wrapper.

Live mem0 tests need OPENAI_API_KEY (for fact extraction) and are skipped
automatically when it's missing. The adapter tests run unconditionally.
"""

from __future__ import annotations

import os

import pytest

from memex.backends.mem_store import (
    ALLOWED_CATEGORIES,
    MemoryItem,
    _extract_ids,
    _normalize_category,
    _to_item,
    _to_items,
)


def test_categories_set():
    assert "profile" in ALLOWED_CATEGORIES
    assert "fact" in ALLOWED_CATEGORIES
    assert _normalize_category("PROFILE") == "profile"
    assert _normalize_category("unknown-cat") == "fact"


def test_extract_ids_dict_results():
    res = {"results": [{"id": "a", "memory": "x"}, {"id": "b"}, {"foo": "bar"}]}
    assert _extract_ids(res) == ["a", "b"]


def test_extract_ids_list_form():
    res = [{"id": "c"}, {"id": "d"}, "not-a-dict"]
    assert _extract_ids(res) == ["c", "d"]


def test_extract_ids_none():
    assert _extract_ids(None) == []
    assert _extract_ids({}) == []


def test_to_item_handles_memory_field_variants():
    item = _to_item({"id": "1", "memory": "hello", "metadata": {"category": "pref"}, "score": 0.7})
    assert isinstance(item, MemoryItem)
    assert item.id == "1"
    assert item.text == "hello"
    assert item.category == "pref"
    assert item.score == pytest.approx(0.7)

    item2 = _to_item({"id": "2", "text": "world", "metadata": {}})
    assert item2.text == "world"
    assert item2.category == "fact"
    assert item2.score == 0.0


def test_to_items_handles_both_envelopes():
    assert _to_items({"results": [{"id": "1", "memory": "a"}]})[0].id == "1"
    assert _to_items([{"id": "2", "memory": "b"}])[0].id == "2"
    assert _to_items(None) == []
    assert _to_items("garbage") == []


# ---------------------------------------------------------------------------
# Live mem0 (skipped when no LLM creds)
# ---------------------------------------------------------------------------


pytestmark_live = pytest.mark.skipif(
    not os.environ.get("OPENAI_API_KEY"),
    reason="OPENAI_API_KEY not set; live mem0 add/search test skipped",
)


@pytestmark_live
def test_mem_add_search_roundtrip(cfg):
    from memex.backends.mem_store import MemStore

    store = MemStore(cfg)
    store.delete_all()
    ids = store.add("Prefers TypeScript over JavaScript for new services", category="pref")
    assert ids, "mem0 should have created at least one memory id"

    hits = store.search("typescript preference", top_k=3)
    assert any("typescript" in h.text.lower() for h in hits)

    listed = store.list(category="pref")
    assert any("typescript" in m.text.lower() for m in listed)
