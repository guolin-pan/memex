"""Tiny shared helpers: tokens, slugify, hashing, date parsing."""

from __future__ import annotations

import hashlib
import re
from datetime import datetime, timedelta, timezone
from functools import lru_cache

from dateutil import parser as dateparser

_SLUG_RE = re.compile(r"[^a-z0-9]+")


def slugify(text: str, max_len: int = 64) -> str:
    s = _SLUG_RE.sub("-", text.lower()).strip("-")
    return s[:max_len] if max_len else s


def sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def content_hash(text: str) -> str:
    return "sha256:" + sha256_hex(text)


def now_iso() -> str:
    """UTC ISO 8601 with seconds resolution (no microseconds)."""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


@lru_cache(maxsize=4)
def _tokenizer(model: str = "cl100k_base"):
    try:
        import tiktoken

        try:
            return tiktoken.encoding_for_model(model)
        except KeyError:
            return tiktoken.get_encoding("cl100k_base")
    except Exception:
        return None


def count_tokens(text: str, model: str = "cl100k_base") -> int:
    enc = _tokenizer(model)
    if enc is None:
        return max(1, len(text) // 4)
    return len(enc.encode(text))


def truncate_to_tokens(text: str, max_tokens: int, model: str = "cl100k_base") -> str:
    if max_tokens <= 0:
        return ""
    enc = _tokenizer(model)
    if enc is None:
        max_chars = max_tokens * 4
        return text if len(text) <= max_chars else text[:max_chars] + "…"
    toks = enc.encode(text)
    if len(toks) <= max_tokens:
        return text
    return enc.decode(toks[:max_tokens]) + "…"


_SINCE_RE = re.compile(r"^\s*(\d+)\s*([smhdwMy])\s*$")
_UNIT_SECONDS = {
    "s": 1,
    "m": 60,
    "h": 3600,
    "d": 86400,
    "w": 7 * 86400,
    "M": 30 * 86400,
    "y": 365 * 86400,
}


def parse_since(value: str | None) -> datetime | None:
    """Accept either an ISO timestamp or a duration like '30d', '2w', '6h'."""
    if not value:
        return None
    m = _SINCE_RE.match(value)
    if m:
        n, unit = int(m.group(1)), m.group(2)
        seconds = n * _UNIT_SECONDS[unit]
        return datetime.now(timezone.utc) - timedelta(seconds=seconds)
    try:
        dt = dateparser.parse(value)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except (ValueError, TypeError):
        return None


def to_datetime(value: str | datetime | None) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    try:
        dt = dateparser.parse(str(value))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except (ValueError, TypeError):
        return None
