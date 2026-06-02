"""Tests for the watchdog handler (without spinning up a real Observer).

We exercise the debouncing/flush path by calling the handler's event methods
directly and shrinking the debounce to zero so the timer fires immediately.
"""

from __future__ import annotations

import time
from pathlib import Path

from memex.core.config import Config
from memex.core.wiki import Wiki
from memex.integrations.watcher import _Handler


def _make_fs_event(path: Path, kind: str):
    """Build a minimal watchdog-compatible event object."""
    from watchdog.events import (
        FileCreatedEvent,
        FileDeletedEvent,
        FileModifiedEvent,
        FileMovedEvent,
    )

    table = {
        "created": FileCreatedEvent,
        "modified": FileModifiedEvent,
        "deleted": FileDeletedEvent,
    }
    if kind == "moved":
        return FileMovedEvent(str(path), str(path) + ".moved")
    return table[kind](str(path))


def _wait_for(condition, timeout: float = 5.0, step: float = 0.05) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        if condition():
            return True
        time.sleep(step)
    return condition()


def test_handler_indexes_new_file(cfg: Config):
    wiki = Wiki(cfg)
    _ = wiki.store  # eager-init Chroma so the lazy import time doesn't race the debounce
    events: list = []
    handler = _Handler(wiki, debounce=0.01, on_event=lambda k, t: events.append((k, t)))

    target = cfg.docs_dir / "inbox" / "auto-added.md"
    target.write_text("# Auto added\n\nBackground watcher should index me.\n")

    handler.on_created(_make_fs_event(target, "created"))
    assert _wait_for(lambda: any(k in {"created", "modified"} for k, _ in events)), events
    hits = wiki.search("background watcher", top_k=2)
    assert any("Auto added" in h.title for h in hits)


def test_handler_ignores_non_markdown(cfg: Config):
    wiki = Wiki(cfg)
    events: list = []
    handler = _Handler(wiki, debounce=0.01, on_event=lambda k, t: events.append((k, t)))

    target = cfg.docs_dir / "ignored.txt"
    target.write_text("not markdown")

    handler.on_created(_make_fs_event(target, "created"))
    time.sleep(0.3)
    assert events == []


def test_handler_handles_deletion(cfg: Config):
    wiki = Wiki(cfg)
    doc = wiki.add(
        source_path=None,
        body="# Throwaway\n\ntemp note\n",
        title="Throwaway",
        tags=["tmp"],
        target_subdir="inbox",
    )

    events: list = []
    handler = _Handler(wiki, debounce=0.01, on_event=lambda k, t: events.append((k, t)))

    doc.path.unlink()
    handler.on_deleted(_make_fs_event(doc.path, "deleted"))
    assert _wait_for(lambda: any(k == "delete" for k, _ in events)), events

    after = wiki.list_docs()
    assert all(d.id != doc.id for d in after)
