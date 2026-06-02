"""watchdog-based file watcher for `memex doc watch`.

Coalesces rapid bursts of events with a small debounce so editors that do
atomic-rename saves (vim, VSCode) don't trigger many reindexes.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from pathlib import Path

from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer

from memex.core.wiki import MARKDOWN_EXTS, Wiki

EventCallback = Callable[[str, str], None]


class _Handler(FileSystemEventHandler):
    def __init__(self, wiki: Wiki, debounce: float, on_event: EventCallback | None):
        self.wiki = wiki
        self.debounce = debounce
        self.on_event = on_event or (lambda _k, _t: None)
        self._pending: dict[Path, str] = {}
        self._lock = threading.Lock()
        self._timer: threading.Timer | None = None

    def _enqueue(self, path: Path, kind: str) -> None:
        if path.suffix.lower() not in MARKDOWN_EXTS:
            return
        with self._lock:
            # 'deleted' is sticky; later events for the same path stay 'deleted'
            # unless we see a new create/modify which means the file is back.
            cur = self._pending.get(path)
            if cur == "deleted" and kind in {"created", "modified"}:
                self._pending[path] = "modified"
            else:
                self._pending[path] = kind
            if self._timer is not None:
                self._timer.cancel()
            self._timer = threading.Timer(self.debounce, self._flush)
            self._timer.daemon = True
            self._timer.start()

    def _flush(self) -> None:
        with self._lock:
            pending = self._pending
            self._pending = {}
            self._timer = None
        for path, kind in pending.items():
            try:
                self._process(path, kind)
            except Exception as e:  # noqa: BLE001 - never let a single bad file kill the loop
                self.on_event("error", f"{path}: {e}")

    def _process(self, path: Path, kind: str) -> None:
        if kind == "deleted":
            # We don't know the doc id without reading frontmatter, and the file
            # is gone — fall back to a scan: any chunk whose `path` matches gets
            # cleaned up by reindex on next opportunity. For real-time, the
            # cheapest correct thing is a focused reindex.
            self.wiki.reindex(only_changed=True)
            self.on_event("delete", str(path))
            return
        doc = self.wiki.update_path(path)
        if doc is not None:
            self.on_event(kind, str(path))

    # watchdog overrides
    def on_created(self, event: FileSystemEvent) -> None:
        if event.is_directory:
            return
        self._enqueue(Path(event.src_path), "created")

    def on_modified(self, event: FileSystemEvent) -> None:
        if event.is_directory:
            return
        self._enqueue(Path(event.src_path), "modified")

    def on_deleted(self, event: FileSystemEvent) -> None:
        if event.is_directory:
            return
        self._enqueue(Path(event.src_path), "deleted")

    def on_moved(self, event: FileSystemEvent) -> None:
        if event.is_directory:
            return
        src = Path(event.src_path)
        dst = Path(event.dest_path)
        self._enqueue(src, "deleted")
        self._enqueue(dst, "modified")


def run_watcher(
    wiki: Wiki, debounce_seconds: float = 1.0, on_event: EventCallback | None = None
) -> None:
    """Block forever, watching docs/. Raises KeyboardInterrupt on Ctrl-C."""
    wiki.cfg.docs_dir.mkdir(parents=True, exist_ok=True)
    handler = _Handler(wiki, debounce_seconds, on_event)
    observer = Observer()
    observer.schedule(handler, str(wiki.cfg.docs_dir), recursive=True)
    observer.start()
    try:
        while True:
            time.sleep(1)
    finally:
        observer.stop()
        observer.join()
