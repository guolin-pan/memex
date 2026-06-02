"""Tests for `memex status`, `memex backup`, `memex restore`, `memex doc graph`, `memex doc reindex --changed`."""

from __future__ import annotations

import tarfile
from pathlib import Path

from typer.testing import CliRunner

from memex.cli import app
from memex.core.config import Config
from memex.core.wiki import Wiki

runner = CliRunner()


def _seed(cfg: Config) -> Wiki:
    w = Wiki(cfg)
    w.add(
        source_path=None,
        body="# A\n\n## one\nalpha content\n",
        title="A",
        tags=["x"],
        target_subdir="inbox",
    )
    w.add(
        source_path=None,
        body="# B\n\n## two\nbeta content\n",
        title="B",
        tags=["y"],
        target_subdir="inbox",
    )
    return w


def test_status_reports_doc_count(cfg: Config):
    _seed(cfg)
    res = runner.invoke(app, ["status"])
    assert res.exit_code == 0, res.output
    assert "docs" in res.output
    assert "2" in res.output  # two docs

    res_json = runner.invoke(app, ["status", "--json"])
    assert res_json.exit_code == 0
    import json

    data = json.loads(res_json.output)
    assert data["docs_count"] == 2
    assert data["chunks_count"] >= 2


def test_backup_creates_archive_with_docs(cfg: Config, tmp_path: Path):
    _seed(cfg)
    out = tmp_path / "snap.tar.gz"
    res = runner.invoke(app, ["backup", "-o", str(out)])
    assert res.exit_code == 0, res.output
    assert out.exists() and out.stat().st_size > 0

    with tarfile.open(out, "r:gz") as tar:
        names = tar.getnames()
    # docs/ and memex.yaml should be inside; .cache/ excluded by default.
    assert any("memex.yaml" in n for n in names)
    assert any("docs/" in n for n in names)
    assert not any("/.cache/" in n or n.endswith("/.cache") for n in names)


def test_restore_extracts_into_fresh_dir(cfg: Config, tmp_path: Path):
    _seed(cfg)
    archive = tmp_path / "snap.tar.gz"
    runner.invoke(app, ["backup", "-o", str(archive)])

    target = tmp_path / "restored"
    res = runner.invoke(app, ["restore", str(archive), "--target", str(target)])
    assert res.exit_code == 0, res.output
    assert (target / "memex.yaml").exists()
    assert (target / "docs").is_dir()


def test_doc_graph_emits_mermaid(cfg: Config):
    _seed(cfg)
    res = runner.invoke(app, ["doc", "graph"])
    assert res.exit_code == 0
    assert "graph TD" in res.output


def test_doc_reindex_changed_flag(cfg: Config):
    _seed(cfg)
    # `reindex --changed` is the default behavior; should skip everything on second run.
    first = runner.invoke(app, ["doc", "reindex"])
    assert first.exit_code == 0 and "added=" in first.output
    second = runner.invoke(app, ["doc", "reindex", "--changed"])
    assert second.exit_code == 0
    assert "added=0" in second.output
    assert "updated=0" in second.output


def test_doc_reindex_all_and_changed_conflict(cfg: Config):
    res = runner.invoke(app, ["doc", "reindex", "--all", "--changed"])
    assert res.exit_code == 2
