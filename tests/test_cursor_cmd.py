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
    assert "sessionStart" in events
    assert "beforeSubmitPrompt" in events
    assert "sessionEnd" in events
    commands = [it["command"] for it in events["beforeSubmitPrompt"]]
    assert any("memex ctx" in c for c in commands)


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
    assert "memex mem add" in txt
    assert "memex doc search" in txt


def test_merge_hooks_deduplicates_by_command():
    existing = {
        "hooks": {
            "beforeSubmitPrompt": [
                {"command": "memex ctx \"$CURSOR_USER_PROMPT\" --write /tmp/cursor-memex-ctx.md --budget 2000"}
            ]
        }
    }
    new = {
        "hooks": {
            "beforeSubmitPrompt": [
                {
                    "name": "memex-ctx",
                    "command": "memex ctx \"$CURSOR_USER_PROMPT\" --write /tmp/cursor-memex-ctx.md --budget 2000",
                }
            ]
        }
    }
    merged = _merge_hooks(existing, new)
    assert len(merged["hooks"]["beforeSubmitPrompt"]) == 1
