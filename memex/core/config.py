"""Configuration loader and defaults.

The memex root directory holds:
  - memex.yaml         (this config)
  - docs/           (the markdown wiki, a git repo)
  - .cache/chroma/  (ChromaDB persistent dir)
  - .cache/mem0/    (mem0 OSS data: qdrant + history.db)
  - .cache/history/ (tombstones, audit log)

Resolution order for the memex root, highest priority first:
  1. --root / -R CLI flag (handled in the CLI layer; passed in here)
  2. MEMEX_ROOT env var
  3. ~/memex (default)
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

DEFAULT_MEMEX_ROOT = Path.home() / "memex"
CONFIG_FILENAME = "memex.yaml"


@dataclass
class EmbedderConfig:
    provider: str = "openai"  # openai | sentence-transformers | chroma-default
    model: str = "text-embedding-3-small"
    dims: int | None = None  # auto-detected when None
    # OpenAI-compatible endpoint overrides. Useful for self-hosted gateways
    # (Ollama, vLLM, LM Studio, LiteLLM, Together-proxy, ...).
    base_url: str | None = None
    api_key: str | None = None


@dataclass
class LLMConfig:
    provider: str = "openai"  # openai | ollama
    model: str = "gpt-4o-mini"
    temperature: float = 0.1
    # OpenAI-compatible endpoint overrides (Ollama OpenAI API, vLLM, etc.).
    base_url: str | None = None
    api_key: str | None = None


@dataclass
class ChunkingConfig:
    target_tokens: int = 800
    overlap_tokens: int = 100
    min_chunk_tokens: int = 50
    split_by_headings: list[str] = field(default_factory=lambda: ["h2", "h3"])


@dataclass
class SearchConfig:
    top_k_docs: int = 5
    top_k_mems: int = 5
    hybrid_alpha: float = 0.5  # vector vs BM25 mix, 1.0 = pure vector
    min_score: float = 0.0


@dataclass
class CtxConfig:
    budget_tokens: int = 2000
    include_profile: bool = True
    include_memories: bool = True
    include_docs: bool = True


@dataclass
class Config:
    """Top-level resolved config."""

    root: Path
    user_id: str = "default"
    embedder: EmbedderConfig = field(default_factory=EmbedderConfig)
    llm: LLMConfig = field(default_factory=LLMConfig)
    chunking: ChunkingConfig = field(default_factory=ChunkingConfig)
    search: SearchConfig = field(default_factory=SearchConfig)
    ctx: CtxConfig = field(default_factory=CtxConfig)

    @property
    def docs_dir(self) -> Path:
        return self.root / "docs"

    @property
    def cache_dir(self) -> Path:
        return self.root / ".cache"

    @property
    def chroma_dir(self) -> Path:
        return self.cache_dir / "chroma"

    @property
    def mem0_dir(self) -> Path:
        return self.cache_dir / "mem0"

    @property
    def history_dir(self) -> Path:
        return self.cache_dir / "history"

    @property
    def kbignore_path(self) -> Path:
        return self.root / ".kbignore"

    @property
    def config_path(self) -> Path:
        return self.root / CONFIG_FILENAME

    def to_dict(self) -> dict[str, Any]:
        return {
            "user_id": self.user_id,
            "embedder": self.embedder.__dict__,
            "llm": self.llm.__dict__,
            "chunking": self.chunking.__dict__,
            "search": self.search.__dict__,
            "ctx": self.ctx.__dict__,
        }


def resolve_root(explicit: Path | str | None = None) -> Path:
    if explicit:
        return Path(explicit).expanduser().resolve()
    env = os.environ.get("MEMEX_ROOT")
    if env:
        return Path(env).expanduser().resolve()
    return DEFAULT_MEMEX_ROOT.resolve()


def _merge(base: dict, override: dict) -> dict:
    out = dict(base)
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def load_config(root: Path | str | None = None) -> Config:
    """Load (or fall back to defaults for) the config at <root>/memex.yaml.

    Missing keys fall back to the dataclass defaults; unknown keys are ignored.
    """
    resolved_root = resolve_root(root)
    cfg_path = resolved_root / CONFIG_FILENAME

    raw: dict[str, Any] = {}
    if cfg_path.exists():
        raw = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}

    defaults = Config(root=resolved_root).to_dict()
    merged = _merge(defaults, raw)

    return Config(
        root=resolved_root,
        user_id=merged.get("user_id", "default"),
        embedder=EmbedderConfig(**merged.get("embedder", {})),
        llm=LLMConfig(**merged.get("llm", {})),
        chunking=ChunkingConfig(**merged.get("chunking", {})),
        search=SearchConfig(**merged.get("search", {})),
        ctx=CtxConfig(**merged.get("ctx", {})),
    )


def write_default_config(root: Path, user_id: str = "default") -> Path:
    """Write a commented default memex.yaml at <root>/memex.yaml.

    Returns the written path. Does not overwrite an existing file.
    """
    cfg_path = root / CONFIG_FILENAME
    if cfg_path.exists():
        return cfg_path

    content = f"""# memex config. Generated by `memex init`.
# Override any field below; missing fields fall back to package defaults.

user_id: {user_id}

embedder:
  provider: openai            # openai | sentence-transformers | chroma-default
  model: text-embedding-3-small
  # dims: 1536                # auto-detected when omitted

llm:
  provider: openai            # openai | ollama
  model: gpt-4o-mini
  temperature: 0.1

chunking:
  target_tokens: 800
  overlap_tokens: 100
  min_chunk_tokens: 50
  split_by_headings: [h2, h3]

search:
  top_k_docs: 5
  top_k_mems: 5
  hybrid_alpha: 0.5           # 1.0 = pure vector, 0.0 = pure BM25
  min_score: 0.0

ctx:
  budget_tokens: 2000
  include_profile: true
  include_memories: true
  include_docs: true
"""
    cfg_path.write_text(content, encoding="utf-8")
    return cfg_path
