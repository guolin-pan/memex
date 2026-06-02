from __future__ import annotations

from datetime import datetime, timedelta, timezone

from memex.core.utils import (
    content_hash,
    count_tokens,
    parse_since,
    slugify,
    truncate_to_tokens,
)


def test_slugify():
    assert slugify("Hello World!") == "hello-world"
    assert slugify("  ---- ") == ""
    assert slugify("中文 test 123") == "test-123" or "test" in slugify("中文 test 123")


def test_content_hash_stable():
    a = content_hash("hello")
    b = content_hash("hello")
    c = content_hash("hello!")
    assert a == b
    assert a != c
    assert a.startswith("sha256:")


def test_count_tokens_nonzero():
    assert count_tokens("hello world") >= 1


def test_truncate_to_tokens_short_text_unchanged():
    s = "hello"
    assert truncate_to_tokens(s, 100) == s


def test_truncate_to_tokens_zero_budget():
    assert truncate_to_tokens("hello", 0) == ""


def test_parse_since_duration():
    now = datetime.now(timezone.utc)
    dt = parse_since("30d")
    assert dt is not None
    assert (now - dt) >= timedelta(days=29)
    assert (now - dt) <= timedelta(days=31)


def test_parse_since_iso():
    dt = parse_since("2026-01-01")
    assert dt is not None
    assert dt.year == 2026 and dt.month == 1 and dt.day == 1


def test_parse_since_none():
    assert parse_since(None) is None
    assert parse_since("") is None
