"""Tests for `memex client …` — boots an in-process uvicorn server, points the
client at it via MEMEX_API_URL, and exercises the subset of commands that don't
require mem0/LLM access.

Skipped automatically when uvicorn isn't installed (it ships with the package
so this should only happen in stripped-down envs).
"""

from __future__ import annotations

import socket
import threading
import time

import httpx
from typer.testing import CliRunner

from memex.cli import app
from memex.core.config import Config
from memex.core.wiki import Wiki
from memex.server.api import build_app

# `live_server` is provided by tests/conftest.py — shared with test_memex_client_script.py
# so both surfaces hit the same FastAPI instance on a real port.

runner = CliRunner()


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _seed(cfg: Config) -> None:
    w = Wiki(cfg)
    w.add(
        source_path=None,
        body="# Test note\n\n## body\n\nclient roundtrip test.\n",
        title="Test note",
        tags=["t1"],
        target_subdir="inbox",
    )


# ---------------------------------------------------------------------------
# end-to-end roundtrips
# ---------------------------------------------------------------------------


def test_client_status(cfg: Config, live_server: str):
    _seed(cfg)
    res = runner.invoke(app, ["client", "status", "--json"])
    assert res.exit_code == 0, res.output
    import json

    data = json.loads(res.output)
    assert data["docs_count"] == 1
    assert data["chunks_count"] >= 1


def test_client_doc_add_and_search(cfg: Config, live_server: str):
    res = runner.invoke(
        app,
        ["client", "doc", "add", "-", "--title", "via client", "--tags", "abc,def"],
        input="# via client\n\nadded over HTTP.\n",
    )
    assert res.exit_code == 0, res.output
    assert "added" in res.output

    res = runner.invoke(app, ["client", "doc", "search", "via client HTTP", "-k", "3"])
    assert res.exit_code == 0, res.output
    assert "via client" in res.output


def test_client_doc_ls(cfg: Config, live_server: str):
    _seed(cfg)
    res = runner.invoke(app, ["client", "doc", "ls", "--json"])
    assert res.exit_code == 0, res.output
    import json

    docs = json.loads(res.output)
    assert any(d["title"] == "Test note" for d in docs)


def test_client_ctx(cfg: Config, live_server: str):
    _seed(cfg)
    res = runner.invoke(
        app,
        ["client", "ctx", "client roundtrip test", "--no-profile", "--no-memories"],
    )
    assert res.exit_code == 0, res.output
    assert "BEGIN memex-context" in res.output
    assert "Test note" in res.output


def test_client_handles_404(cfg: Config, live_server: str):
    _seed(cfg)
    res = runner.invoke(app, ["client", "doc", "show", "nonexistent-id"])
    assert res.exit_code == 1
    assert "error" in res.output.lower()


def test_client_raw_passthrough(cfg: Config, live_server: str):
    res = runner.invoke(app, ["client", "raw", "GET", "/healthz"])
    assert res.exit_code == 0
    assert "200" in res.output


def test_client_respects_bearer_token(cfg: Config, monkeypatch):
    """Spin a token-protected server and verify the client passes the bearer."""
    import uvicorn

    monkeypatch.setenv("MEMEX_API_TOKEN", "alpha")

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
            if httpx.get(f"{base_url}/healthz", timeout=0.5).status_code == 200:
                break
        except httpx.HTTPError:
            time.sleep(0.1)

    try:
        # No token → 401
        monkeypatch.setenv("MEMEX_API_URL", base_url)
        monkeypatch.delenv("MEMEX_API_TOKEN", raising=False)
        res = runner.invoke(app, ["client", "status"])
        assert res.exit_code == 1
        assert "401" in res.output

        # With token → 200
        monkeypatch.setenv("MEMEX_API_TOKEN", "alpha")
        res = runner.invoke(app, ["client", "status"])
        assert res.exit_code == 0, res.output
    finally:
        server.should_exit = True
        t.join(timeout=5)
