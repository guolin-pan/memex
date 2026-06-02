"""End-to-end tests for templates/memex-client.py — the standalone stdlib-only
HTTP client that ships into ~/.cursor/agents/ for Cursor hooks and subagents.

We invoke the script as a subprocess (its filename has a hyphen so it can't be
imported as a normal Python module) and exercise it against the same live
uvicorn fixture used by test_client_cmd.py. The two surfaces should behave
identically as far as the user-visible CLI contract goes.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import httpx
import pytest

from memex.core.config import Config
from memex.core.wiki import Wiki

REPO_ROOT = Path(__file__).resolve().parents[1]
CLIENT_SCRIPT = REPO_ROOT / "templates" / "memex-client.py"


def _seed(cfg: Config) -> None:
    """Drop one wiki doc so search/ctx have something to find."""
    Wiki(cfg).add(
        source_path=None,
        body="# Script note\n\n## body\n\nstdlib client roundtrip test.\n",
        title="Script note",
        tags=["client-script"],
        target_subdir="inbox",
    )


def _run(*args: str, env_extra: dict | None = None, stdin: str | None = None,
         expect_exit: int | None = 0):
    """Invoke memex-client.py with the given args. Returns (stdout, stderr, code)."""
    env = os.environ.copy()
    if env_extra:
        env.update(env_extra)
    proc = subprocess.run(
        [sys.executable, str(CLIENT_SCRIPT), *args],
        env=env,
        input=stdin,
        capture_output=True,
        text=True,
        timeout=20,
    )
    if expect_exit is not None and proc.returncode != expect_exit:
        msg = (
            f"unexpected exit {proc.returncode} for `memex-client.py {' '.join(args)}`\n"
            f"--- stdout ---\n{proc.stdout}\n"
            f"--- stderr ---\n{proc.stderr}\n"
        )
        raise AssertionError(msg)
    return proc.stdout, proc.stderr, proc.returncode


# ---------------------------------------------------------------------------
# Script preconditions
# ---------------------------------------------------------------------------


def test_script_is_executable_and_stdlib_only():
    """Ship as a self-contained, stdlib-only file."""
    assert CLIENT_SCRIPT.exists(), f"missing {CLIENT_SCRIPT}"
    text = CLIENT_SCRIPT.read_text(encoding="utf-8")
    # Shebang first so direct invocation works after chmod +x.
    assert text.startswith("#!/usr/bin/env python3")
    # Nothing from the memex package or httpx — that's the whole point of
    # this script. urllib + argparse + json is the entire toolbox.
    for forbidden in ("from memex", "import memex", "import httpx", "import typer", "import click"):
        assert forbidden not in text, f"client script unexpectedly references {forbidden!r}"


def test_help_subcommands_dont_need_network():
    """`--help` on every subcommand must work without a live server."""
    for argv in (
        ["--help"],
        ["status", "--help"],
        ["ctx", "--help"],
        ["raw", "--help"],
        ["doc", "--help"],
        ["doc", "search", "--help"],
        ["doc", "add", "--help"],
        ["doc", "reindex", "--help"],
        ["mem", "--help"],
        ["mem", "add", "--help"],
        ["mem", "search", "--help"],
        ["mem", "profile", "--help"],
    ):
        out, _, code = _run(*argv, expect_exit=0)
        assert "usage:" in out.lower() or "Usage:" in out


def test_missing_subcommand_exits_2():
    _, _, code = _run(expect_exit=2)
    assert code == 2


def test_unreachable_server_exits_2():
    """Hitting a port nothing is listening on returns the connection-error code."""
    _, err, code = _run(
        "--url",
        "http://127.0.0.1:1",
        "status",
        expect_exit=2,
    )
    assert "cannot reach" in err.lower()


# ---------------------------------------------------------------------------
# Roundtrips against a live FastAPI server
# ---------------------------------------------------------------------------


def test_status_against_live_server(cfg: Config, live_server: str):
    out, _, _ = _run("status")
    # plain text format: key value pairs, root first
    assert "root" in out
    assert "docs_count" in out


def test_status_json_flag(cfg: Config, live_server: str):
    _seed(cfg)
    out, _, _ = _run("status", "--json")
    data = json.loads(out)
    assert data["docs_count"] == 1
    assert data["chunks_count"] >= 1


def test_doc_add_via_stdin_then_search(cfg: Config, live_server: str):
    body = "# via script\n\nadded over HTTP by the standalone script.\n"
    out, _, _ = _run(
        "doc", "add", "-", "--title", "via script", "--tags", "abc,def",
        stdin=body,
    )
    assert "saved" in out
    assert "via script" in out

    out, _, _ = _run("doc", "search", "via script HTTP", "-k", "3")
    assert "via script" in out


def test_doc_ls_json(cfg: Config, live_server: str):
    _seed(cfg)
    out, _, _ = _run("doc", "ls", "--json")
    docs = json.loads(out)
    assert any(d["title"] == "Script note" for d in docs)


def test_ctx_emits_block(cfg: Config, live_server: str):
    _seed(cfg)
    out, _, _ = _run(
        "ctx",
        "stdlib client roundtrip test",
        "--no-profile",
        "--no-memories",
    )
    assert "BEGIN memex-context" in out
    assert "Script note" in out


def test_ctx_write_flag(cfg: Config, live_server: str, tmp_path: Path):
    _seed(cfg)
    out_path = tmp_path / "ctx.md"
    _, err, _ = _run(
        "ctx", "stdlib", "--no-profile", "--no-memories",
        "--write", str(out_path),
    )
    assert out_path.exists()
    assert "BEGIN memex-context" in out_path.read_text()
    assert "wrote ctx" in err  # written to stderr


def test_raw_get_healthz(cfg: Config, live_server: str):
    out, _, _ = _run("raw", "GET", "/healthz")
    assert "HTTP 200" in out


def test_handles_404(cfg: Config, live_server: str):
    _seed(cfg)
    _, err, code = _run("doc", "show", "nonexistent-id", expect_exit=1)
    assert code == 1
    assert "error" in err.lower()


def test_url_flag_overrides_env(cfg: Config, monkeypatch):
    """CLI --url wins over MEMEX_API_URL env."""
    # Point env at a dead port, --url at a different dead port, observe that
    # the error mentions the --url destination (proving CLI flag won).
    monkeypatch.setenv("MEMEX_API_URL", "http://127.0.0.1:2")
    _, err, _ = _run(
        "--url", "http://127.0.0.1:3", "status",
        expect_exit=2,
    )
    assert "127.0.0.1:3" in err


def test_bearer_token_round_trip(cfg: Config, monkeypatch):
    """Spin a token-protected server and check the script sends Authorization."""
    import socket

    import uvicorn

    from memex.server.api import build_app

    monkeypatch.setenv("MEMEX_API_TOKEN", "secret-script-token")

    # Free port
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]

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
        # No token in env → 401.
        _, err, code = _run(
            "--url", base_url, "status",
            env_extra={"MEMEX_API_TOKEN": ""},
            expect_exit=1,
        )
        assert "401" in err

        # Token via env → 200.
        out, _, code = _run(
            "--url", base_url, "status",
            env_extra={"MEMEX_API_TOKEN": "secret-script-token"},
            expect_exit=0,
        )
        assert "root" in out

        # Token via --token flag → 200.
        out, _, code = _run(
            "--url", base_url, "--token", "secret-script-token", "status",
            env_extra={"MEMEX_API_TOKEN": ""},
            expect_exit=0,
        )
        assert "root" in out
    finally:
        server.should_exit = True
        t.join(timeout=5)


@pytest.mark.skipif(
    not os.environ.get("OPENAI_API_KEY"),
    reason="mem0 needs an LLM SDK key on first init (matches test_mem_store / test_server_api)",
)
def test_mem_search_handles_no_results(cfg: Config, live_server: str):
    """Empty result set must produce a clean message, not crash."""
    out, _, code = _run("mem", "search", "definitely-not-a-real-thing")
    assert code == 0
    assert "no memories" in out.lower()


def test_doc_search_empty_query_returns_no_hits(cfg: Config, live_server: str):
    out, _, _ = _run("doc", "search", "definitely-not-anywhere", "-k", "3")
    assert "no hits" in out.lower() or out.strip() == ""


def test_chinese_args_pass_through(cfg: Config, live_server: str):
    """Non-ASCII args should not be mangled by argparse/stdlib URL encoding."""
    body = "# 中文标题\n\n中文正文。\n"
    out, _, _ = _run(
        "doc", "add", "-", "--title", "中文标题", "--tags", "中文",
        stdin=body,
    )
    assert "saved" in out
    out, _, _ = _run("doc", "search", "中文正文", "-k", "3")
    assert "中文标题" in out or "中文" in out


def test_global_flags_must_precede_subcommand(cfg: Config, live_server: str):
    """--url / --token are valid only BEFORE the subcommand (matches `memex client`).

    Putting them after the subcommand must fail — this guards against the
    silent-overwrite bug where a subparser-level `--url` would set args.url
    back to None and shadow the parent's parsed value.
    """
    _, err, code = _run("status", "--url", "http://example.invalid", expect_exit=2)
    assert code == 2
    assert "unrecognized" in err.lower() or "url" in err.lower()
