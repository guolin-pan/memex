"""Tests for `memex cursor install-hooks`, `install-rule`, and `install-client`."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from typer.testing import CliRunner

from memex.cli import app
from memex.commands.cursor_cmd import _merge_hooks

runner = CliRunner()


def _isolated_home(monkeypatch, tmp_path: Path) -> Path:
    """Point HOME at tmp_path so install commands don't touch the real ~."""
    monkeypatch.setenv("HOME", str(tmp_path))
    return tmp_path


def test_install_hooks_into_empty_path(tmp_path: Path, monkeypatch):
    _isolated_home(monkeypatch, tmp_path)
    target = tmp_path / "hooks.json"
    res = runner.invoke(app, ["cursor", "install-hooks", "--target", str(target)])
    assert res.exit_code == 0, res.output
    data = json.loads(target.read_text())
    events = data["hooks"]
    # The default template ships only the two hooks whose work has a HTTP
    # equivalent. mem-learn-from-cursor-transcript reads a file on the host
    # and has no HTTP surface; it's intentionally omitted here so the default
    # install is safe against a Docker-deployed memex.
    assert "sessionStart" in events
    assert "beforeSubmitPrompt" in events
    commands = [it["command"] for it in events["beforeSubmitPrompt"]]
    # Hooks now invoke the standalone script by absolute path; the `memex`
    # package no longer needs to be on PATH.
    assert any("memex-client.py ctx" in c for c in commands)
    assert all("$HOME/.cursor/agents/memex-client.py" in it["command"] for it in events["sessionStart"])
    # And the standalone script itself was dropped at the expected location.
    client = tmp_path / ".cursor" / "agents" / "memex-client.py"
    assert client.exists(), "install-hooks should also drop memex-client.py"
    assert os.access(client, os.X_OK), "memex-client.py should be executable"


def test_install_hooks_merges_into_existing(tmp_path: Path, monkeypatch):
    _isolated_home(monkeypatch, tmp_path)
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


def test_install_hooks_replace_requires_force(tmp_path: Path, monkeypatch):
    _isolated_home(monkeypatch, tmp_path)
    target = tmp_path / "hooks.json"
    target.write_text("{}")
    res = runner.invoke(app, ["cursor", "install-hooks", "--target", str(target), "--replace"])
    assert res.exit_code == 2  # exists, no --force


def test_install_hooks_no_install_client(tmp_path: Path, monkeypatch):
    _isolated_home(monkeypatch, tmp_path)
    target = tmp_path / "hooks.json"
    res = runner.invoke(
        app, ["cursor", "install-hooks", "--target", str(target), "--no-install-client"]
    )
    assert res.exit_code == 0, res.output
    # Script must NOT have been dropped when explicitly opted out.
    assert not (tmp_path / ".cursor" / "agents" / "memex-client.py").exists()


def test_install_rule(tmp_path: Path, monkeypatch):
    _isolated_home(monkeypatch, tmp_path)
    project = tmp_path / "proj"
    res = runner.invoke(app, ["cursor", "install-rule", str(project)])
    assert res.exit_code == 0, res.output
    target = project / ".cursor" / "rules" / "memex.mdc"
    assert target.exists()
    txt = target.read_text()
    # The rule points the main thread at the standalone script (so the
    # template works against a Docker-deployed memex by default).
    assert "memex-client.py doc search" in txt
    assert "memex-client.py mem search" in txt
    # And documents how the script picks the server.
    assert "MEMEX_API_URL" in txt
    # Bare `memex client` (the typer subcommand form) MUST be gone — the
    # whole point of this script is to avoid depending on the `memex` package
    # being on PATH.
    assert "memex client " not in txt
    # The standalone client script gets dropped at user scope (~/.cursor/...)
    # alongside the rule install.
    client = tmp_path / ".cursor" / "agents" / "memex-client.py"
    assert client.exists(), "install-rule should also drop memex-client.py"
    assert os.access(client, os.X_OK)


def test_install_client_writes_executable_script(tmp_path: Path, monkeypatch):
    _isolated_home(monkeypatch, tmp_path)
    res = runner.invoke(app, ["cursor", "install-client"])
    assert res.exit_code == 0, res.output
    client = tmp_path / ".cursor" / "agents" / "memex-client.py"
    assert client.exists()
    assert os.access(client, os.X_OK)
    txt = client.read_text()
    assert txt.startswith("#!/usr/bin/env python3"), "script must start with a shebang"
    assert "def build_parser" in txt
    # Stdlib-only — must not import `memex.*` or external packages like httpx.
    assert "from memex" not in txt
    assert "import memex" not in txt


def test_install_client_skips_existing_without_force(tmp_path: Path, monkeypatch):
    _isolated_home(monkeypatch, tmp_path)
    client_dir = tmp_path / ".cursor" / "agents"
    client_dir.mkdir(parents=True)
    custom = client_dir / "memex-client.py"
    custom.write_text("# custom user script — do not touch\n")

    res = runner.invoke(app, ["cursor", "install-client"])
    assert res.exit_code == 2
    assert custom.read_text().startswith("# custom user script")


def test_install_client_force_overwrites(tmp_path: Path, monkeypatch):
    _isolated_home(monkeypatch, tmp_path)
    client_dir = tmp_path / ".cursor" / "agents"
    client_dir.mkdir(parents=True)
    custom = client_dir / "memex-client.py"
    custom.write_text("stale\n")

    res = runner.invoke(app, ["cursor", "install-client", "--force"])
    assert res.exit_code == 0
    assert custom.read_text().startswith("#!/usr/bin/env python3")


def test_install_client_custom_target(tmp_path: Path, monkeypatch):
    _isolated_home(monkeypatch, tmp_path)
    target = tmp_path / "elsewhere" / "memex-client.py"
    res = runner.invoke(app, ["cursor", "install-client", "--target", str(target)])
    assert res.exit_code == 0, res.output
    assert target.exists()
    assert os.access(target, os.X_OK)


def test_merge_hooks_deduplicates_by_command():
    cmd = '$HOME/.cursor/agents/memex-client.py ctx "$CURSOR_USER_PROMPT" --write /tmp/cursor-memex-ctx.md --budget 2000'
    existing = {"hooks": {"beforeSubmitPrompt": [{"command": cmd}]}}
    new = {
        "hooks": {
            "beforeSubmitPrompt": [{"name": "memex-ctx", "command": cmd}]
        }
    }
    merged = _merge_hooks(existing, new)
    assert len(merged["hooks"]["beforeSubmitPrompt"]) == 1


def test_wheel_ships_all_templates(tmp_path: Path):
    """Regression guard for pyproject.toml [tool.setuptools.package-data].

    If you add a new template (e.g. templates/foo/bar.md) without also listing
    its parent directory in package-data, setuptools silently drops it from
    the wheel and `memex cursor install-*` then crashes for anyone who
    installed memex via `pip install` / `uv tool install`. This test builds
    a wheel and asserts every file in templates/ on disk made it in.
    """
    import shutil
    import subprocess
    import zipfile

    uv = shutil.which("uv")
    if not uv:
        pytest.skip("uv not on PATH; can't drive `uv build`")

    repo_root = Path(__file__).resolve().parents[1]
    res = subprocess.run(
        [uv, "build", "--wheel", "--out-dir", str(tmp_path)],
        cwd=str(repo_root),
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert res.returncode == 0, f"uv build failed:\n{res.stdout}\n{res.stderr}"

    wheels = list(tmp_path.glob("*.whl"))
    assert len(wheels) == 1, f"expected one wheel, got {wheels}"

    with zipfile.ZipFile(wheels[0]) as zf:
        shipped = set(zf.namelist())

    expected: set[str] = set()
    src = repo_root / "templates"
    for p in src.rglob("*"):
        if p.is_file():
            # The wheel layout places templates/ at the top of the zip;
            # `resources.files("memex") / ".." / "templates"` resolves into
            # this directory when the package is installed.
            expected.add("templates/" + str(p.relative_to(src)))

    missing = expected - shipped
    assert not missing, (
        f"wheel is missing template files: {sorted(missing)}.\n"
        f"Update [tool.setuptools.package-data] memex = [...] in pyproject.toml "
        f"to include their parent directory glob."
    )
