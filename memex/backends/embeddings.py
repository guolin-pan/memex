"""Embedding provider abstraction.

Three providers:
  - openai: requires OPENAI_API_KEY; default model text-embedding-3-small (1536-dim).
  - sentence-transformers: local; default all-MiniLM-L6-v2 (384-dim).
  - chroma-default: ChromaDB's built-in ONNX MiniLM (~80MB; no API key, no torch).

All wrappers inherit from Chroma's `EmbeddingFunction` Protocol so the
collection's vector-store handshake works without monkey-patching.
"""

from __future__ import annotations

from typing import Any

from chromadb.api.types import Documents, EmbeddingFunction, Embeddings

from memex.core.config import EmbedderConfig


class _OpenAIEmbedder(EmbeddingFunction[Documents]):
    def __init__(self, model: str, base_url: str | None = None, api_key: str | None = None):
        from openai import OpenAI

        kwargs: dict[str, Any] = {}
        if base_url:
            kwargs["base_url"] = base_url
        if api_key:
            kwargs["api_key"] = api_key
        self._client = OpenAI(**kwargs)
        self._model = model
        self._base_url = base_url

    @staticmethod
    def name() -> str:  # type: ignore[override]
        return "memex.openai"

    def __call__(self, input: Documents) -> Embeddings:  # noqa: A002 - chroma's API
        if not input:
            return []
        resp = self._client.embeddings.create(model=self._model, input=list(input))
        return [d.embedding for d in resp.data]

    @staticmethod
    def build_from_config(config: dict[str, Any]) -> _OpenAIEmbedder:
        return _OpenAIEmbedder(
            config.get("model", "text-embedding-3-small"),
            base_url=config.get("base_url"),
            api_key=config.get("api_key"),
        )

    def get_config(self) -> dict[str, Any]:
        cfg: dict[str, Any] = {"model": self._model}
        if self._base_url:
            cfg["base_url"] = self._base_url
        return cfg

    @staticmethod
    def validate_config(config: dict[str, Any]) -> None:
        return


class _STEmbedder(EmbeddingFunction[Documents]):
    def __init__(self, model: str):
        from sentence_transformers import SentenceTransformer

        self._st = SentenceTransformer(model)
        self._model_name = model

    @staticmethod
    def name() -> str:  # type: ignore[override]
        return "memex.sentence_transformers"

    def __call__(self, input: Documents) -> Embeddings:  # noqa: A002
        if not input:
            return []
        return self._st.encode(list(input), convert_to_numpy=True).tolist()

    @staticmethod
    def build_from_config(config: dict[str, Any]) -> _STEmbedder:
        return _STEmbedder(config.get("model", "all-MiniLM-L6-v2"))

    def get_config(self) -> dict[str, Any]:
        return {"model": self._model_name}

    @staticmethod
    def validate_config(config: dict[str, Any]) -> None:
        return


class _ChromaDefaultEmbedder(EmbeddingFunction[Documents]):
    """Wrapper around ChromaDB's built-in DefaultEmbeddingFunction (ONNX MiniLM).

    No API key, no torch — just onnxruntime + a ~80MB model the first time.
    Useful for offline / no-credentials runs and CI smoke tests.
    """

    def __init__(self, model: str = "all-MiniLM-L6-v2"):
        from chromadb.utils import embedding_functions

        self._fn = embedding_functions.DefaultEmbeddingFunction()
        self._model_name = model

    @staticmethod
    def name() -> str:  # type: ignore[override]
        return "memex.chroma_default"

    def __call__(self, input: Documents) -> Embeddings:  # noqa: A002
        if not input:
            return []
        return list(self._fn(list(input)))

    @staticmethod
    def build_from_config(config: dict[str, Any]) -> _ChromaDefaultEmbedder:
        return _ChromaDefaultEmbedder(config.get("model", "all-MiniLM-L6-v2"))

    def get_config(self) -> dict[str, Any]:
        return {"model": self._model_name}

    @staticmethod
    def validate_config(config: dict[str, Any]) -> None:
        return


def build_embedder(cfg: EmbedderConfig) -> EmbeddingFunction[Documents]:
    """Return a Chroma-compatible embedding function with a `.name()` method."""
    provider = (cfg.provider or "openai").lower()
    if provider == "openai":
        return _OpenAIEmbedder(cfg.model, base_url=cfg.base_url, api_key=cfg.api_key)
    if provider in {"sentence-transformers", "st", "local"}:
        return _STEmbedder(cfg.model)
    if provider in {"chroma-default", "chroma", "default", "onnx"}:
        return _ChromaDefaultEmbedder(cfg.model or "all-MiniLM-L6-v2")
    raise ValueError(f"Unknown embedder provider: {cfg.provider!r}")
