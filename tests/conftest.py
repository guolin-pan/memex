"""Shared fixtures: a fresh memex root per test, wired to the offline embedder.

These tests deliberately avoid mem0 and OpenAI; mem0 is exercised by its own
test module that's skipped when OPENAI_API_KEY is missing.
"""

from __future__ import annotations

import os
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
