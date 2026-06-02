"""Tests for `memex cursor install-hooks` and `install-rule`."""

from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from memex.cli import app
from memex.commands.cursor_cmd import _merge_hooks

runner = CliRunner()


def test_install_hooks_into_empty_path(tmp_path: Path):
    target = tmp_path / "hooks.json"
    res = runner.invoke(app, ["cursor", "install-hooks", "--target", str(target)])
    assert res.exit_code == 0, res.output
    data = json.loads(target.read_text())
    events = data["hooks"]
    # The default template ships only the two hooks whose work has a `memex
    # client` (HTTP) equivalent. mem-learn-from-cursor-transcript reads a file
    # on the host and has no HTTP surface; it's intentionally omitted here so
    # the default install is safe against a Docker-deployed memex.
    assert "sessionStart" in events
    assert "beforeSubmitPrompt" in events
    commands = [it["command"] for it in events["beforeSubmitPrompt"]]
    assert any("memex client ctx" in c for c in commands)
    assert all("memex client" in it["command"] for it in events["sessionStart"])


def test_install_hooks_merges_into_existing(tmp_path: Path):
    target = tmp_path / "hooks.json"
    target.write_text(
        json.dumps(
            {"hooks": {"sessionStart": [{"name": "existing", "command": "echo hi"}]}}
        )
    )
    res = runner.invoke(app, ["cursor", "install-hooks", "--target", str(target), "--merge"])
    assert res.exit_code == 0, res.output
    data = json.loads(target.read_text())
    session_start_names = [it["name"] for it in data["hooks"]["sessionStart"]]
    assert "existing" in session_start_names
    assert "memex-profile" in session_start_names


def test_install_hooks_replace_requires_force(tmp_path: Path):
    target = tmp_path / "hooks.json"
    target.write_text("{}")
    res = runner.invoke(app, ["cursor", "install-hooks", "--target", str(target), "--replace"])
    assert res.exit_code == 2  # exists, no --force


def test_install_rule(tmp_path: Path):
    res = runner.invoke(app, ["cursor", "install-rule", str(tmp_path)])
    assert res.exit_code == 0, res.output
    target = tmp_path / ".cursor" / "rules" / "memex.mdc"
    assert target.exists()
    txt = target.read_text()
    # The rule points the main thread at the `memex client` HTTP CLI (so the
    # template works against a Docker-deployed memex by default).
    assert "memex client mem add" in txt
    assert "memex client doc search" in txt
    # And documents how the client picks the server.
    assert "MEMEX_API_URL" in txt


def test_merge_hooks_deduplicates_by_command():
    cmd = 'memex client ctx "$CURSOR_USER_PROMPT" --write /tmp/cursor-memex-ctx.md --budget 2000'
    existing = {"hooks": {"beforeSubmitPrompt": [{"command": cmd}]}}
    new = {
        "hooks": {
            "beforeSubmitPrompt": [{"name": "memex-ctx", "command": cmd}]
        }
    }
    merged = _merge_hooks(existing, new)
    assert len(merged["hooks"]["beforeSubmitPrompt"]) == 1
