"""Shared fixtures: a fresh memex root per test, wired to the offline embedder.

These tests deliberately avoid mem0 and OpenAI; mem0 is exercised by its own
test module that's skipped when OPENAI_API_KEY is missing.
"""

from __future__ import annotations

import os
import socket
import threading
import time
from pathlib import Path

import pytest

from memex.core.config import Config, load_config, write_default_config


@pytest.fixture()
def memex_root(tmp_path: Path) -> Path:
    root = tmp_path / "memex"
    docs = root / "docs"
    docs.mkdir(parents=True)
    for sub in ("inbox", "projects", "people", "work", "learning", "reference"):
        (docs / sub).mkdir()
    write_default_config(root, user_id="test")
    # Force the offline embedder so no API key is required.
    cfg_text = (root / "memex.yaml").read_text()
    cfg_text = cfg_text.replace(
        "provider: openai            # openai | sentence-transformers | chroma-default",
        "provider: chroma-default",
    ).replace(
        "model: text-embedding-3-small",
        "model: all-MiniLM-L6-v2",
    )
    (root / "memex.yaml").write_text(cfg_text)
    os.environ["MEMEX_ROOT"] = str(root)
    return root


@pytest.fixture()
def cfg(memex_root: Path) -> Config:
    return load_config(memex_root)


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture()
def live_server(cfg: Config, monkeypatch):
    """Boot uvicorn in a background thread, yield its base URL, shut it down.

    Used by both `test_client_cmd.py` (the Typer client) and
    `test_memex_client_script.py` (the standalone stdlib script) so the two
    surfaces are exercised against the same FastAPI app on a real socket.
    """
    import httpx
    import uvicorn

    from memex.server.api import build_app

    port = _free_port()
    api = build_app(str(cfg.root))
    config = uvicorn.Config(api, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)

    t = threading.Thread(target=server.run, daemon=True)
    t.start()

    base_url = f"http://127.0.0.1:{port}"
    deadline = time.time() + 10
    while time.time() < deadline:
        try:
            r = httpx.get(f"{base_url}/healthz", timeout=0.5)
            if r.status_code == 200:
                break
        except httpx.HTTPError:
            time.sleep(0.1)
    else:
        server.should_exit = True
        raise RuntimeError("uvicorn did not start in time")

    monkeypatch.setenv("MEMEX_API_URL", base_url)
    monkeypatch.delenv("MEMEX_API_TOKEN", raising=False)

    try:
        yield base_url
    finally:
        server.should_exit = True
        t.join(timeout=5)
