"""Tests for the Cursor subagent integration.

Covers:
  - `memex cursor install-agents --scope project|user`
  - `memex cursor list-agents`
  - `memex cursor print-agent <name>`
  - Frontmatter shape of each shipped agent file (matches Cursor's docs).
  - The slimmed `memex.mdc` rule keeps the read-side guidance but drops write-side
    instructions (those moved into the memex-archive / memex-curator agents).
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml
from typer.testing import CliRunner

from memex.cli import app
from memex.commands.cursor_cmd import AGENT_NAMES

runner = CliRunner()

ALLOWED_FRONTMATTER_KEYS = {"name", "description", "model", "readonly", "is_background"}


def _split_frontmatter(text: str) -> tuple[dict, str]:
    m = re.match(r"^---\s*\n(.*?)\n---\s*\n?(.*)$", text, re.DOTALL)
    assert m, "missing frontmatter delimiters"
    meta = yaml.safe_load(m.group(1)) or {}
    body = m.group(2)
    return meta, body


# ---------------------------------------------------------------------------
# Frontmatter validity (per cursor.com/docs/subagents)
# ---------------------------------------------------------------------------


def test_all_agent_templates_have_valid_frontmatter():
    runner_local = CliRunner()
    for name in AGENT_NAMES:
        res = runner_local.invoke(app, ["cursor", "print-agent", name])
        assert res.exit_code == 0, res.output
        meta, body = _split_frontmatter(res.output)

        # Required fields
        assert meta.get("name") == name, f"name field must equal {name!r}"
        assert isinstance(meta.get("description"), str) and meta["description"].strip()
        assert meta.get("model") in {"inherit"} or isinstance(meta.get("model"), str)
        assert meta.get("readonly") in (True, False)
        assert meta.get("is_background") in (True, False)

        # No undocumented keys (Cursor will silently ignore them today, but
        # keep the surface minimal to avoid drift).
        extra = set(meta.keys()) - ALLOWED_FRONTMATTER_KEYS
        assert not extra, f"{name}: unexpected frontmatter keys {extra}"

        # Non-empty body
        assert body.strip(), f"{name}: empty body"


def test_kb_ask_is_readonly_others_are_not():
    """memex-ask reads; memex-archive and memex-curator write — readonly flag must reflect that."""
    runner_local = CliRunner()
    by_name = {}
    for name in AGENT_NAMES:
        meta, _ = _split_frontmatter(
            runner_local.invoke(app, ["cursor", "print-agent", name]).output
        )
        by_name[name] = meta
    assert by_name["memex-ask"]["readonly"] is True
    assert by_name["memex-archive"]["readonly"] is False
    assert by_name["memex-curator"]["readonly"] is False


# ---------------------------------------------------------------------------
# install-agents
# ---------------------------------------------------------------------------


def test_install_agents_project_scope(tmp_path: Path):
    res = runner.invoke(
        app,
        ["cursor", "install-agents", "--scope", "project", "--project-root", str(tmp_path)],
    )
    assert res.exit_code == 0, res.output
    agents_dir = tmp_path / ".cursor" / "agents"
    assert agents_dir.is_dir()
    for name in AGENT_NAMES:
        f = agents_dir / f"{name}.md"
        assert f.exists(), f"missing {f}"
        meta, _ = _split_frontmatter(f.read_text())
        assert meta["name"] == name


def test_install_agents_user_scope(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    res = runner.invoke(app, ["cursor", "install-agents", "--scope", "user"])
    assert res.exit_code == 0, res.output
    for name in AGENT_NAMES:
        f = tmp_path / ".cursor" / "agents" / f"{name}.md"
        assert f.exists(), f"missing user-scope agent {f}"


def test_install_agents_only_filter(tmp_path: Path):
    res = runner.invoke(
        app,
        [
            "cursor",
            "install-agents",
            "--scope",
            "project",
            "--project-root",
            str(tmp_path),
            "--only",
            "memex-ask",
        ],
    )
    assert res.exit_code == 0, res.output
    files = sorted(p.name for p in (tmp_path / ".cursor" / "agents").iterdir())
    assert files == ["memex-ask.md"]


def test_install_agents_skips_existing_without_force(tmp_path: Path):
    agents_dir = tmp_path / ".cursor" / "agents"
    agents_dir.mkdir(parents=True)
    (agents_dir / "memex-ask.md").write_text("# custom user agent — do not touch\n")

    res = runner.invoke(
        app,
        ["cursor", "install-agents", "--scope", "project", "--project-root", str(tmp_path)],
    )
    assert res.exit_code == 0
    assert "skip" in res.output
    assert (agents_dir / "memex-ask.md").read_text().startswith("# custom user agent")
    # The other two still get installed.
    assert (agents_dir / "memex-archive.md").exists()
    assert (agents_dir / "memex-curator.md").exists()


def test_install_agents_force_overwrites(tmp_path: Path):
    agents_dir = tmp_path / ".cursor" / "agents"
    agents_dir.mkdir(parents=True)
    (agents_dir / "memex-ask.md").write_text("stale\n")

    res = runner.invoke(
        app,
        [
            "cursor",
            "install-agents",
            "--scope",
            "project",
            "--project-root",
            str(tmp_path),
            "--force",
        ],
    )
    assert res.exit_code == 0
    content = (agents_dir / "memex-ask.md").read_text()
    assert content.startswith("---")
    assert "name: memex-ask" in content


def test_install_agents_rejects_unknown_name(tmp_path: Path):
    res = runner.invoke(
        app,
        [
            "cursor",
            "install-agents",
            "--scope",
            "project",
            "--project-root",
            str(tmp_path),
            "--only",
            "memex-nonsense",
        ],
    )
    assert res.exit_code == 2
    assert "unknown agent" in res.output.lower()


# ---------------------------------------------------------------------------
# list-agents / print-agent
# ---------------------------------------------------------------------------


def test_list_agents_table():
    res = runner.invoke(app, ["cursor", "list-agents"])
    assert res.exit_code == 0
    for name in AGENT_NAMES:
        assert name in res.output


def test_print_agent_unknown_name():
    res = runner.invoke(app, ["cursor", "print-agent", "memex-nope"])
    assert res.exit_code == 2


# ---------------------------------------------------------------------------
# Slimmed rule: still teaches read-side, no longer prescribes writes
# ---------------------------------------------------------------------------


def test_kb_mdc_rule_delegates_writes_to_subagents():
    res = runner.invoke(app, ["cursor", "print-rule"])
    assert res.exit_code == 0
    rule = res.output

    # Still references the subagents and the read-side flow.
    assert "/memex-ask" in rule
    assert "/memex-archive" in rule
    assert "/memex-curator" in rule
    assert "memex-context" in rule  # uses pre-injected ctx
    assert "memex doc search" in rule
    assert "memex mem search" in rule

    # Must NOT prescribe writes from the main thread anymore (those moved to
    # the memex-archive / memex-curator agents).
    forbidden = ["memex doc add", "memex mem add", "memex doc rm", "memex mem rm", "memex mem update"]
    for token in forbidden:
        # Allowed only inside an explicit "don't" / negative context.
        # Cheap heuristic: count occurrences and require they all sit after a
        # "Don't" / "NOT" marker. Easier: just assert the token doesn't appear
        # as a positive instruction (no "run" / "use" prefix on the same line).
        for line in rule.splitlines():
            if token in line:
                low = line.lower()
                assert any(
                    marker in low for marker in ("don't", "do not", "not do", "redirect", "switch")
                ), f"main-thread rule still tells the model to run {token!r}: {line!r}"
