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
    resolve_memory_ref,
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


def test_resolve_memory_ref_exact_and_suffix():
    full = "aaaaaaaa-bbbb-cccc-dddd-c57ed1036c5a"
    ids = [full, "11111111-2222-3333-4444-555555555555"]
    assert resolve_memory_ref(full, ids) == full
    assert resolve_memory_ref("c57ed1036c5a", ids) == full


def test_resolve_memory_ref_too_short_without_exact_match():
    with pytest.raises(KeyError, match="not found"):
        resolve_memory_ref("nope", ["aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"])


def test_resolve_memory_ref_ambiguous_suffix():
    a = "aaaaaaaa-bbbb-cccc-dddd-111111111111"
    b = "bbbbbbbb-bbbb-cccc-dddd-111111111111"
    with pytest.raises(ValueError, match="Ambiguous"):
        resolve_memory_ref("111111111111", [a, b])


def test_resolve_memory_ref_empty_and_whitespace():
    with pytest.raises(KeyError, match="[Ee]mpty"):
        resolve_memory_ref("", ["aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"])
    with pytest.raises(KeyError, match="[Ee]mpty"):
        resolve_memory_ref("   ", ["aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"])


def test_resolve_memory_ref_normalises_dashes():
    """User can paste with or without dashes; resolver normalises both sides."""
    full = "aaaaaaaa-bbbb-cccc-dddd-c57ed1036c5a"
    assert resolve_memory_ref("dddd-c57ed1036c5a", [full]) == full
    assert resolve_memory_ref("ddddc57ed1036c5a", [full]) == full


def test_resolve_memory_ref_error_messages_include_actionable_hint():
    """Error strings must mention `mem ls --json` so users know how to recover."""
    try:
        resolve_memory_ref("nope", ["aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"])
    except KeyError as e:
        assert "mem ls --json" in str(e)
    try:
        resolve_memory_ref(
            "111111111111",
            [
                "aaaaaaaa-bbbb-cccc-dddd-111111111111",
                "bbbbbbbb-bbbb-cccc-dddd-111111111111",
            ],
        )
    except ValueError as e:
        assert "mem ls --json" in str(e)


# ---------------------------------------------------------------------------
# MemStore.delete / .get fast-path → suffix fallback (mem0 mocked)
# ---------------------------------------------------------------------------


class _FakeMemory:
    """In-memory stand-in for mem0.Memory that matches the bits MemStore uses.

    Mirrors mem0's actual error contract: `delete(memory_id=X)` raises
    `ValueError(f"Memory with id {X} not found")` for unknown ids; `get`
    returns None for unknown ids; `get_all` returns a list of dicts.
    """

    def __init__(self, memories):
        # memories: list of dicts with at least 'id', 'memory', 'metadata'
        self._mems = list(memories)
        self.deleted_ids: list[str] = []

    def delete(self, *, memory_id):
        for i, m in enumerate(self._mems):
            if m["id"] == memory_id:
                self._mems.pop(i)
                self.deleted_ids.append(memory_id)
                return {"message": "Memory deleted successfully!"}
        raise ValueError(f"Memory with id {memory_id} not found")

    def get(self, *, memory_id):
        for m in self._mems:
            if m["id"] == memory_id:
                return m
        return None

    def get_all(self, **kwargs):
        return list(self._mems)


def _store_with_fake_memory(cfg, fake):
    """Return a MemStore whose lazy `.memory` property is replaced by `fake`."""
    from memex.backends.mem_store import MemStore

    store = MemStore(cfg)
    store._memory = fake  # bypass the real mem0.Memory init
    return store


def test_delete_uses_fast_path_when_id_is_canonical(cfg):
    """If the user already passes a canonical id, we should NOT list memories.

    The fix turns each delete from O(N) listing+match into O(1) — verified by
    counting how many times the fake's `get_all` is hit.
    """
    full = "aaaaaaaa-bbbb-cccc-dddd-111111111111"
    fake = _FakeMemory(
        [{"id": full, "memory": "x", "metadata": {"category": "fact"}}]
    )
    store = _store_with_fake_memory(cfg, fake)

    listings = {"count": 0}
    orig_get_all = fake.get_all

    def counting_get_all(**kw):
        listings["count"] += 1
        return orig_get_all(**kw)

    fake.get_all = counting_get_all

    store.delete(full)
    assert fake.deleted_ids == [full]
    assert listings["count"] == 0, "fast path should not enumerate memories"


def test_delete_falls_back_to_suffix_resolution(cfg):
    """If mem0 raises 'not found' for a short id, we list and resolve."""
    full = "aaaaaaaa-bbbb-cccc-dddd-c57ed1036c5a"
    fake = _FakeMemory(
        [{"id": full, "memory": "x", "metadata": {"category": "fact"}}]
    )
    store = _store_with_fake_memory(cfg, fake)

    store.delete("c57ed1036c5a")
    assert fake.deleted_ids == [full]


def test_delete_unknown_id_raises_keyerror(cfg):
    fake = _FakeMemory(
        [{"id": "aaaaaaaa-bbbb-cccc-dddd-111111111111", "memory": "x", "metadata": {}}]
    )
    store = _store_with_fake_memory(cfg, fake)

    with pytest.raises(KeyError, match="not found"):
        store.delete("does-not-exist-anywhere")


def test_delete_ambiguous_suffix_raises_valueerror(cfg):
    a = "aaaaaaaa-bbbb-cccc-dddd-111111111111"
    b = "bbbbbbbb-bbbb-cccc-dddd-111111111111"
    fake = _FakeMemory(
        [
            {"id": a, "memory": "x", "metadata": {}},
            {"id": b, "memory": "y", "metadata": {}},
        ]
    )
    store = _store_with_fake_memory(cfg, fake)
    with pytest.raises(ValueError, match="Ambiguous"):
        store.delete("111111111111")


def test_delete_propagates_backend_errors(cfg):
    """Non-'not found' ValueErrors / Exceptions from mem0 must NOT be swallowed."""
    fake = _FakeMemory([])

    def boom(**kw):
        raise RuntimeError("qdrant gone")

    fake.delete = boom  # type: ignore[assignment]
    store = _store_with_fake_memory(cfg, fake)

    with pytest.raises(RuntimeError, match="qdrant gone"):
        store.delete("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")


def test_get_fast_path_then_suffix_fallback(cfg):
    full = "aaaaaaaa-bbbb-cccc-dddd-c57ed1036c5a"
    fake = _FakeMemory(
        [{"id": full, "memory": "x", "metadata": {"category": "fact"}}]
    )
    store = _store_with_fake_memory(cfg, fake)

    # Canonical: hit immediately, no listing.
    listings = {"count": 0}
    orig_get_all = fake.get_all
    fake.get_all = lambda **kw: (listings.__setitem__("count", listings["count"] + 1) or orig_get_all(**kw))

    got = store.get(full)
    assert got is not None and got.id == full
    assert listings["count"] == 0

    # Suffix: resolves via listing.
    got = store.get("c57ed1036c5a")
    assert got is not None and got.id == full
    assert listings["count"] == 1


def test_get_unknown_returns_none(cfg):
    fake = _FakeMemory([])
    store = _store_with_fake_memory(cfg, fake)
    assert store.get("nope") is None


def test_get_ambiguous_suffix_raises(cfg):
    """get propagates ValueError so the API can map it to 409."""
    a = "aaaaaaaa-bbbb-cccc-dddd-111111111111"
    b = "bbbbbbbb-bbbb-cccc-dddd-111111111111"
    fake = _FakeMemory(
        [
            {"id": a, "memory": "x", "metadata": {}},
            {"id": b, "memory": "y", "metadata": {}},
        ]
    )
    store = _store_with_fake_memory(cfg, fake)
    with pytest.raises(ValueError, match="Ambiguous"):
        store.get("111111111111")


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
