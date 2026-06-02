"""Tests for `memex ctx` — the context assembler.

We invoke the command via Typer's CliRunner and monkey-patch MemStore so the
test runs without OPENAI_API_KEY.
"""

from __future__ import annotations

from dataclasses import dataclass

from typer.testing import CliRunner

from memex.backends.mem_store import MemoryItem
from memex.cli import app
from memex.core.config import Config
from memex.core.wiki import Wiki


@dataclass
class _FakeMemStore:
    """Stub that satisfies MemStore's surface without touching mem0."""

    profile_items: list[MemoryItem]
    search_items: list[MemoryItem]

    def list(self, *, category=None):
        if category in {"profile", "pref"}:
            return [m for m in self.profile_items if m.category == category]
        return self.profile_items

    def search(self, query, *, top_k=5, category=None):
        return self.search_items[:top_k]


def _install_fake_mem(monkeypatch, profile_items, search_items):
    fake = _FakeMemStore(profile_items=profile_items, search_items=search_items)
    # Patch the symbol where ctx_cmd looks it up.
    monkeypatch.setattr("memex.commands.ctx_cmd.MemStore", lambda cfg: fake)


def _seed_docs(cfg: Config):
    w = Wiki(cfg)
    w.add(
        source_path=None,
        body="# Project Phoenix\n\n## Stack\n\nPython, FastAPI, Postgres pgvector.\n",
        title="Project Phoenix",
        tags=["project-x", "architecture"],
        target_subdir="projects/phoenix",
    )


def test_ctx_assembles_block(monkeypatch, cfg: Config):
    profile = [
        MemoryItem(id="1", text="Prefers TypeScript", category="pref"),
        MemoryItem(id="2", text="Senior backend engineer", category="profile"),
    ]
    mems = [MemoryItem(id="3", text="Chose pgvector over Qdrant", category="decision", score=0.9)]
    _install_fake_mem(monkeypatch, profile, mems)
    _seed_docs(cfg)

    runner = CliRunner()
    res = runner.invoke(app, ["ctx", "project phoenix stack"])
    assert res.exit_code == 0, res.output
    out = res.output
    assert "BEGIN memex-context" in out and "END memex-context" in out
    assert "About the user" in out
    assert "TypeScript" in out
    assert "Project Phoenix" in out
    assert "pgvector" in out


def test_ctx_budget_truncates(monkeypatch, cfg: Config):
    big_text = "very long text " * 500
    profile = [MemoryItem(id="1", text=big_text, category="profile")]
    _install_fake_mem(monkeypatch, profile, [])
    _seed_docs(cfg)

    runner = CliRunner()
    res_small = runner.invoke(app, ["ctx", "phoenix", "--budget", "100"])
    res_big = runner.invoke(app, ["ctx", "phoenix", "--budget", "5000"])
    assert res_small.exit_code == 0
    assert res_big.exit_code == 0
    assert len(res_small.output) < len(res_big.output)


def test_ctx_write_to_file(monkeypatch, cfg: Config, tmp_path):
    _install_fake_mem(monkeypatch, [], [])
    _seed_docs(cfg)

    target = tmp_path / "ctx.md"
    runner = CliRunner()
    res = runner.invoke(app, ["ctx", "phoenix", "--write", str(target)])
    assert res.exit_code == 0
    assert target.exists()
    assert "Project Phoenix" in target.read_text()


def test_ctx_no_query_returns_only_profile(monkeypatch, cfg: Config):
    profile = [MemoryItem(id="1", text="Lives in Shanghai", category="profile")]
    _install_fake_mem(monkeypatch, profile, [])
    _seed_docs(cfg)

    runner = CliRunner()
    res = runner.invoke(app, ["ctx"])
    assert res.exit_code == 0
    assert "Lives in Shanghai" in res.output
    # No query => no doc/memory search section.
    assert "Relevant docs" not in res.output
    assert "Relevant memories" not in res.output
