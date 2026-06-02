"""Pydantic schemas for the memex HTTP API.

Kept deliberately flat and stable — these are what the `memex client` CLI and
LLM/agent callers depend on. Don't add backend-specific fields without versioning.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# Docs
# ---------------------------------------------------------------------------


class DocAddRequest(BaseModel):
    body: str = Field(..., description="Raw markdown body.")
    title: str | None = Field(None, description="Document title (else inferred from H1 / filename).")
    tags: list[str] = Field(default_factory=list)
    subdir: str = Field("inbox", description="Subdirectory under docs/ to land in.")


class DocOut(BaseModel):
    id: str
    title: str
    path: str
    tags: list[str]
    created: str | None = None
    updated: str | None = None


class DocSearchHitOut(BaseModel):
    chunk_id: str
    doc_id: str
    title: str
    path: str
    heading: str
    text: str
    score: float
    tags: list[str]
    updated: str


class DocSearchResponse(BaseModel):
    query: str
    hits: list[DocSearchHitOut]


class DocListResponse(BaseModel):
    docs: list[DocOut]


# ---------------------------------------------------------------------------
# Memories
# ---------------------------------------------------------------------------


class MemAddRequest(BaseModel):
    text: str
    category: str = "fact"
    tags: list[str] = Field(default_factory=list)
    infer: bool = Field(
        False,
        description=(
            "Run mem0's LLM-driven fact extractor over the input (may split, "
            "merge, or dedupe). Off by default: text stored verbatim."
        ),
    )


class MemOut(BaseModel):
    id: str
    text: str
    category: str
    score: float = 0.0
    metadata: dict[str, Any] = Field(default_factory=dict)


class MemAddResponse(BaseModel):
    ids: list[str]


class MemListResponse(BaseModel):
    memories: list[MemOut]


# ---------------------------------------------------------------------------
# Ctx
# ---------------------------------------------------------------------------


class CtxRequest(BaseModel):
    query: str = ""
    budget: int | None = None
    top_k_docs: int | None = None
    top_k_mems: int | None = None
    include_profile: bool = True
    include_memories: bool = True
    include_docs: bool = True


class CtxResponse(BaseModel):
    block: str
    tokens: int


# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------


class StatusResponse(BaseModel):
    root: str
    user_id: str
    docs_count: int
    chunks_count: int
    embedder: str
    llm: str
    docs_dir_bytes: int
    chroma_dir_bytes: int
    mem0_dir_bytes: int
    history_dir_bytes: int
    version: str


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class ErrorResponse(BaseModel):
    error: str
    detail: str | None = None
