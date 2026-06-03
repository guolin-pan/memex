"""FastAPI app for the memex HTTP service.

Endpoint surface mirrors the CLI's core operations:

  GET    /                       — service banner
  GET    /healthz                — liveness
  GET    /status                 — memex health (counts, sizes, providers, version)

  POST   /doc/add                — add a markdown doc (body in JSON)
  GET    /doc                    — list docs (?tag=, ?since=)
  GET    /doc/{ident}            — show a doc by id / slug / path
  DELETE /doc/{ident}            — remove a doc (?keep_file=true to drop from index only)
  GET    /doc/search             — hybrid search (?q=, ?k=, ?tag=, ?since=)
  POST   /doc/reindex            — reindex (?all=true to force-rebuild)

  POST   /mem/add                — add a memory
  GET    /mem                    — list memories (?category=)
  GET    /mem/profile            — render profile block
  GET    /mem/{mem_id}           — show one memory
  DELETE /mem/{mem_id}           — delete by id, or 'all' to wipe
  GET    /mem/search             — semantic search (?q=, ?k=, ?category=)

  POST   /ctx                    — build the unified context block (what hooks do)

Auth: optional bearer token via MEMEX_API_TOKEN env var. If unset, the server
runs unauthenticated (fine for localhost / private Docker networks).
"""

from __future__ import annotations

import os
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException, Query
from fastapi.responses import JSONResponse

from memex import __version__
from memex.backends.mem_store import (
    ALLOWED_CATEGORIES,
    PROFILE_CATEGORIES,
    MemStore,
)
from memex.commands.ctx_cmd import _assemble  # reuse the budget-aware assembler
from memex.commands.status_cmd import _dir_size
from memex.core.config import Config, load_config
from memex.core.utils import count_tokens
from memex.core.wiki import Wiki
from memex.server.schemas import (
    CtxRequest,
    CtxResponse,
    DocAddRequest,
    DocListResponse,
    DocOut,
    DocSearchHitOut,
    DocSearchResponse,
    MemAddRequest,
    MemAddResponse,
    MemListResponse,
    MemOut,
    StatusResponse,
)

# ---------------------------------------------------------------------------
# State container — one Wiki + MemStore per process, keyed by memex root.
# ---------------------------------------------------------------------------


class _State:
    """Process-wide handles. Built once on app startup, reused per request."""

    def __init__(self, root: str | None):
        self.cfg: Config = load_config(root)
        self.wiki: Wiki = Wiki(self.cfg)
        self._mem: MemStore | None = None  # lazy: only needed for /mem endpoints

    @property
    def mem(self) -> MemStore:
        if self._mem is None:
            self._mem = MemStore(self.cfg)
        return self._mem


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------


def build_app(root: str | None = None) -> FastAPI:
    state = _State(root)
    api_token = os.environ.get("MEMEX_API_TOKEN") or ""

    app = FastAPI(
        title="memex",
        version=__version__,
        summary="Personal assistant + knowledge base (mem0 OSS + ChromaDB).",
    )

    # -------------- auth dependency --------------

    def _require_token(authorization: str | None = Header(default=None)):
        if not api_token:
            return  # no token configured → open
        expected = f"Bearer {api_token}"
        if authorization != expected:
            raise HTTPException(status_code=401, detail="invalid or missing bearer token")

    # -------------- meta --------------

    @app.get("/")
    def root_banner():
        return {
            "name": "memex",
            "version": __version__,
            "root": str(state.cfg.root),
            "docs": "/docs",
            "openapi": "/openapi.json",
            "auth_required": bool(api_token),
        }

    @app.get("/healthz")
    def healthz():
        return {"ok": True}

    @app.get("/status", response_model=StatusResponse, dependencies=[Depends(_require_token)])
    def status_endpoint():
        cfg = state.cfg
        try:
            chunks = state.wiki.store.count()
        except Exception:
            chunks = 0
        docs = list(state.wiki.iter_doc_paths())
        return StatusResponse(
            root=str(cfg.root),
            user_id=cfg.user_id,
            docs_count=len(docs),
            chunks_count=chunks,
            embedder=f"{cfg.embedder.provider}:{cfg.embedder.model}",
            llm=f"{cfg.llm.provider}:{cfg.llm.model}",
            docs_dir_bytes=_dir_size(cfg.docs_dir),
            chroma_dir_bytes=_dir_size(cfg.chroma_dir),
            mem0_dir_bytes=_dir_size(cfg.mem0_dir),
            history_dir_bytes=_dir_size(cfg.history_dir),
            version=__version__,
        )

    # -------------- docs --------------

    @app.post("/doc/add", response_model=DocOut, dependencies=[Depends(_require_token)])
    def doc_add(req: DocAddRequest):
        if not req.body.strip():
            raise HTTPException(400, "empty body")
        doc = state.wiki.add(
            source_path=None,
            body=req.body,
            title=req.title,
            tags=req.tags,
            target_subdir=req.subdir,
            source="api",
        )
        return _doc_to_out(doc)

    @app.get("/doc", response_model=DocListResponse, dependencies=[Depends(_require_token)])
    def doc_list(
        tag: str | None = Query(None),
        since: str | None = Query(None),
    ):
        docs = state.wiki.list_docs(tag=tag, since=since)
        return DocListResponse(docs=[_doc_to_out(d) for d in docs])

    @app.get("/doc/search", response_model=DocSearchResponse, dependencies=[Depends(_require_token)])
    def doc_search(
        q: str = Query(..., min_length=1),
        k: int | None = Query(None, ge=1, le=100),
        tag: str | None = Query(None),
        since: str | None = Query(None),
    ):
        hits = state.wiki.search(q, top_k=k, tag=tag, since=since)
        return DocSearchResponse(
            query=q,
            hits=[
                DocSearchHitOut(
                    chunk_id=h.chunk_id,
                    doc_id=h.doc_id,
                    title=h.title,
                    path=h.path,
                    heading=h.heading,
                    text=h.text,
                    score=h.score,
                    tags=h.tags,
                    updated=h.updated,
                )
                for h in hits
            ],
        )

    @app.get("/doc/{ident}", response_model=DocOut, dependencies=[Depends(_require_token)])
    def doc_show(ident: str):
        doc = state.wiki.get(ident)
        if doc is None:
            raise HTTPException(404, f"no doc matches {ident!r}")
        return _doc_to_out(doc)

    @app.delete("/doc/{ident}", dependencies=[Depends(_require_token)])
    def doc_rm(ident: str, keep_file: bool = Query(False)):
        try:
            doc_id, removed_path = state.wiki.remove(ident, delete_file=not keep_file)
        except FileNotFoundError as e:
            raise HTTPException(404, str(e)) from e
        return {"id": doc_id, "removed_path": str(removed_path) if removed_path else None}

    @app.post("/doc/reindex", dependencies=[Depends(_require_token)])
    def doc_reindex(all_: bool = Query(False, alias="all")):
        res = state.wiki.reindex(only_changed=not all_)
        return {
            "added": [str(p) for p in res.added],
            "updated": [str(p) for p in res.updated],
            "skipped": [str(p) for p in res.skipped],
            "deleted": list(res.deleted),
        }

    # -------------- memories --------------

    @app.post("/mem/add", response_model=MemAddResponse, dependencies=[Depends(_require_token)])
    def mem_add(req: MemAddRequest):
        if req.category not in ALLOWED_CATEGORIES:
            raise HTTPException(
                400, f"unknown category {req.category!r}; allowed: {sorted(ALLOWED_CATEGORIES)}"
            )
        metadata: dict[str, Any] = {"tags": list(req.tags)} if req.tags else {}
        ids = state.mem.add(
            req.text,
            category=req.category,
            metadata=metadata or None,
            infer=req.infer,
        )
        return MemAddResponse(ids=ids)

    @app.get("/mem", response_model=MemListResponse, dependencies=[Depends(_require_token)])
    def mem_list(category: str | None = Query(None)):
        items = state.mem.list(category=category)
        return MemListResponse(memories=[_mem_to_out(m) for m in items])

    @app.get("/mem/profile", dependencies=[Depends(_require_token)])
    def mem_profile(max_items: int = Query(20, ge=1, le=200)):
        items = []
        for cat in PROFILE_CATEGORIES:
            items.extend(state.mem.list(category=cat))
        items = items[:max_items]
        if not items:
            block = (
                "## About the user\n\n"
                "_(no profile memories yet — use POST /mem/add with category='profile')_\n"
            )
        else:
            lines = ["## About the user", ""]
            for m in items:
                lines.append(f"- ({m.category}) {m.text}")
            block = "\n".join(lines) + "\n"
        return {"block": block, "count": len(items)}

    @app.get("/mem/search", response_model=MemListResponse, dependencies=[Depends(_require_token)])
    def mem_search(
        q: str = Query(..., min_length=1),
        k: int = Query(5, ge=1, le=100),
        category: str | None = Query(None),
    ):
        items = state.mem.search(q, top_k=k, category=category)
        return MemListResponse(memories=[_mem_to_out(m) for m in items])

    @app.get("/mem/{mem_id}", response_model=MemOut, dependencies=[Depends(_require_token)])
    def mem_show(mem_id: str):
        # `state.mem.get(...)` returns None for unknown ids (mem0's contract)
        # and propagates ValueError only when a short suffix matches multiple
        # stored memories. KeyError is mapped to None inside `get()` so we
        # don't need to catch it here.
        try:
            item = state.mem.get(mem_id)
        except ValueError as e:
            raise HTTPException(409, str(e)) from e
        if item is None:
            raise HTTPException(404, f"no memory {mem_id!r}")
        return _mem_to_out(item)

    @app.delete("/mem/{mem_id}", dependencies=[Depends(_require_token)])
    def mem_rm(mem_id: str):
        if mem_id == "all":
            state.mem.delete_all()
            return {"deleted": "all"}
        try:
            state.mem.delete(mem_id)
        except KeyError as e:
            raise HTTPException(404, str(e)) from e
        except ValueError as e:
            raise HTTPException(409, str(e)) from e
        return {"deleted": mem_id}

    # -------------- ctx --------------

    @app.post("/ctx", response_model=CtxResponse, dependencies=[Depends(_require_token)])
    def ctx_endpoint(req: CtxRequest):
        cfg = state.cfg
        budget = req.budget if req.budget is not None else cfg.ctx.budget_tokens
        k_docs = req.top_k_docs if req.top_k_docs is not None else cfg.search.top_k_docs
        k_mems = req.top_k_mems if req.top_k_mems is not None else cfg.search.top_k_mems

        # profile
        profile_md = ""
        if req.include_profile:
            items: list = []
            for cat in PROFILE_CATEGORIES:
                items.extend(state.mem.list(category=cat))
            if items:
                lines = ["## About the user", ""]
                for it in items[:20]:
                    lines.append(f"- ({it.category}) {it.text}")
                profile_md = "\n".join(lines) + "\n"

        # mem search
        memories_md = ""
        if req.include_memories and req.query.strip():
            hits = state.mem.search(req.query, top_k=k_mems)
            hits = [h for h in hits if h.category not in PROFILE_CATEGORIES]
            if hits:
                lines = ["## Relevant memories", ""]
                for h in hits:
                    lines.append(f"- ({h.category}) {h.text}")
                memories_md = "\n".join(lines) + "\n"

        # doc search
        docs_md = ""
        if req.include_docs and req.query.strip():
            try:
                hits = state.wiki.search(req.query, top_k=k_docs)
            except Exception:
                hits = []
            if hits:
                lines = ["## Relevant docs", ""]
                for h in hits:
                    heading = h.heading if h.heading and h.heading != "(root)" else ""
                    heading_str = f" — {heading}" if heading else ""
                    lines.append(
                        f"### [{h.title}]({h.path}){heading_str}  (score {h.score:.2f})"
                    )
                    for ln in h.text.strip().splitlines():
                        lines.append(f"> {ln}")
                    lines.append("")
                docs_md = "\n".join(lines)

        body = _assemble(profile_md, memories_md, docs_md, budget=budget)
        BEGIN = "<!-- BEGIN memex-context (auto-generated by /ctx) -->"
        END = "<!-- END memex-context -->"
        block = (
            f"{BEGIN}\n{body}\n{END}\n"
            if body.strip()
            else f"{BEGIN}\n_(no context)_\n{END}\n"
        )
        return CtxResponse(block=block, tokens=count_tokens(block))

    # -------------- error handler --------------

    @app.exception_handler(Exception)
    def _unhandled(_req, exc: Exception):  # noqa: ARG001
        return JSONResponse(
            status_code=500,
            content={"error": type(exc).__name__, "detail": str(exc)},
        )

    return app


# ---------------------------------------------------------------------------
# Converters
# ---------------------------------------------------------------------------


def _doc_to_out(doc) -> DocOut:
    return DocOut(
        id=doc.id,
        title=doc.title,
        path=str(doc.path),
        tags=doc.tags,
        created=str(doc.meta.get("created") or ""),
        updated=doc.updated,
    )


def _mem_to_out(m) -> MemOut:
    return MemOut(
        id=m.id,
        text=m.text,
        category=m.category,
        score=m.score,
        metadata=m.metadata or {},
    )
