"""Tests for `memex init --profile {openai,local}` and the new LLM/embedder fields."""

from __future__ import annotations

from pathlib import Path

import yaml
from typer.testing import CliRunner

from memex.cli import app
from memex.core.config import load_config

runner = CliRunner()


def test_init_openai_profile_is_default(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("MEMEX_ROOT", raising=False)
    root = tmp_path / "memex"
    res = runner.invoke(app, ["init", str(root), "-u", "alice"])
    assert res.exit_code == 0, res.output
    raw = yaml.safe_load((root / "memex.yaml").read_text())
    assert raw["embedder"]["provider"] == "openai"
    assert raw["llm"]["provider"] == "openai"
    assert "base_url" not in raw["llm"] or raw["llm"].get("base_url") is None


def test_init_local_profile_writes_offline_config(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("MEMEX_ROOT", raising=False)
    root = tmp_path / "memex"
    res = runner.invoke(app, ["init", str(root), "-u", "bob", "--profile", "local"])
    assert res.exit_code == 0, res.output
    raw = yaml.safe_load((root / "memex.yaml").read_text())
    assert raw["embedder"]["provider"] == "chroma-default"
    assert raw["embedder"]["model"] == "all-MiniLM-L6-v2"
    assert raw["llm"]["provider"] == "openai"
    assert raw["llm"]["model"] == "qwen3:4b"
    assert raw["llm"]["base_url"].startswith("http://")
    assert raw["llm"]["api_key"]  # non-empty placeholder

    # And the loaded Config materializes those fields correctly.
    cfg = load_config(root)
    assert cfg.user_id == "bob"
    assert cfg.embedder.provider == "chroma-default"
    assert cfg.llm.base_url == "http://10.242.29.48:11434/v1"
    assert cfg.llm.api_key == "no-key"


def test_init_unknown_profile_errors(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("MEMEX_ROOT", raising=False)
    root = tmp_path / "memex"
    res = runner.invoke(app, ["init", str(root), "--profile", "nonsense"])
    assert res.exit_code == 2
    assert "unknown profile" in res.output.lower()


def test_load_config_supports_custom_base_url(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("MEMEX_ROOT", raising=False)
    root = tmp_path / "memex"
    root.mkdir()
    (root / "docs").mkdir()
    (root / "memex.yaml").write_text(
        "user_id: x\n"
        "embedder:\n"
        "  provider: openai\n"
        "  model: text-embedding-3-small\n"
        "  base_url: http://emb.example/v1\n"
        "  api_key: tok-emb\n"
        "llm:\n"
        "  provider: openai\n"
        "  model: qwen3:4b\n"
        "  base_url: http://llm.example/v1\n"
        "  api_key: tok-llm\n"
    )
    cfg = load_config(root)
    assert cfg.embedder.base_url == "http://emb.example/v1"
    assert cfg.embedder.api_key == "tok-emb"
    assert cfg.llm.base_url == "http://llm.example/v1"
    assert cfg.llm.api_key == "tok-llm"


def test_mem_store_passes_endpoint_to_mem0(tmp_path: Path, monkeypatch):
    """Don't call mem0 — just snapshot the config dict that would be passed."""
    monkeypatch.delenv("MEMEX_ROOT", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    root = tmp_path / "memex"
    root.mkdir()
    (root / "docs").mkdir()
    (root / "memex.yaml").write_text(
        "user_id: x\n"
        "embedder:\n"
        "  provider: chroma-default\n"
        "  model: all-MiniLM-L6-v2\n"
        "llm:\n"
        "  provider: openai\n"
        "  model: qwen3:4b\n"
        "  base_url: http://10.242.29.48:11434/v1\n"
        "  api_key: no-key\n"
    )
    cfg = load_config(root)

    captured: dict = {}

    def _fake_from_config(config):  # noqa: ARG001
        captured.update(config)

        class _Stub:
            pass

        return _Stub()

    import sys
    from types import SimpleNamespace

    fake_mem0 = SimpleNamespace(Memory=SimpleNamespace(from_config=_fake_from_config))
    monkeypatch.setitem(sys.modules, "mem0", fake_mem0)

    from memex.backends.mem_store import MemStore

    store = MemStore(cfg)
    _ = store.memory  # forces _build()

    assert captured["llm"]["provider"] == "openai"
    assert captured["llm"]["config"]["model"] == "qwen3:4b"
    assert captured["llm"]["config"]["openai_base_url"] == "http://10.242.29.48:11434/v1"
    assert captured["llm"]["config"]["api_key"] == "no-key"
    assert captured["embedder"]["provider"] == "huggingface"
    assert captured["embedder"]["config"]["model"] == "all-MiniLM-L6-v2"
