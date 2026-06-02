"""Tests for the FastAPI server.

Uses the in-process TestClient — no real network, no real uvicorn. mem0-touching
endpoints are skipped when OPENAI_API_KEY is missing, but the wiki/ctx/status
paths exercise the full HTTP layer end-to-end.
"""

from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient

from memex.core.config import Config
from memex.core.wiki import Wiki
from memex.server.api import build_app


@pytest.fixture()
def client(cfg: Config) -> TestClient:
    app = build_app(str(cfg.root))
    return TestClient(app)


def _seed(cfg: Config) -> None:
    w = Wiki(cfg)
    w.add(
        source_path=None,
        body="# Postgres tuning\n\n## work_mem\n\nKeep sorts in memory.\n",
        title="Postgres tuning",
        tags=["db", "reference"],
        target_subdir="reference",
    )
    w.add(
        source_path=None,
        body="# Project Phoenix\n\n## Stack\n\nFastAPI + pgvector.\n",
        title="Project Phoenix",
        tags=["project-x"],
        target_subdir="projects/phoenix",
    )


# ---------------------------------------------------------------------------
# meta
# ---------------------------------------------------------------------------


def test_healthz_is_open(client: TestClient):
    r = client.get("/healthz")
    assert r.status_code == 200
    assert r.json() == {"ok": True}


def test_root_banner(client: TestClient):
    r = client.get("/")
    assert r.status_code == 200
    body = r.json()
    assert body["name"] == "memex"
    assert "version" in body
    assert body["auth_required"] in (True, False)


def test_status_endpoint(client: TestClient, cfg: Config):
    _seed(cfg)
    r = client.get("/status")
    assert r.status_code == 200
    body = r.json()
    assert body["docs_count"] == 2
    assert body["chunks_count"] >= 2
    assert body["embedder"].startswith("chroma-default")


# ---------------------------------------------------------------------------
# docs
# ---------------------------------------------------------------------------


def test_doc_add_search_show_delete_via_http(client: TestClient):
    payload = {
        "body": "# A new doc\n\n## Section\n\nHello via API.\n",
        "title": "A new doc",
        "tags": ["api", "test"],
    }
    r = client.post("/doc/add", json=payload)
    assert r.status_code == 200, r.text
    created = r.json()
    assert created["title"] == "A new doc"
    assert "api" in created["tags"]
    doc_id = created["id"]

    # list includes it
    r = client.get("/doc")
    assert r.status_code == 200
    titles = [d["title"] for d in r.json()["docs"]]
    assert "A new doc" in titles

    # search hits it
    r = client.get("/doc/search", params={"q": "api", "k": 3})
    assert r.status_code == 200
    hit_titles = [h["title"] for h in r.json()["hits"]]
    assert "A new doc" in hit_titles

    # show by id
    r = client.get(f"/doc/{doc_id}")
    assert r.status_code == 200
    assert r.json()["id"] == doc_id

    # delete (file + index)
    r = client.delete(f"/doc/{doc_id}")
    assert r.status_code == 200
    assert r.json()["id"] == doc_id

    # gone
    r = client.get(f"/doc/{doc_id}")
    assert r.status_code == 404


def test_doc_add_rejects_empty_body(client: TestClient):
    r = client.post("/doc/add", json={"body": "  \n\n"})
    assert r.status_code == 400


def test_doc_reindex_endpoint(client: TestClient, cfg: Config):
    _seed(cfg)
    r = client.post("/doc/reindex")
    assert r.status_code == 200
    body = r.json()
    assert "added" in body and "updated" in body and "skipped" in body


# ---------------------------------------------------------------------------
# ctx
# ---------------------------------------------------------------------------


def test_ctx_endpoint_without_mem(client: TestClient, cfg: Config):
    """ctx must still work when mem0 isn't reachable: skip profile/memories."""
    _seed(cfg)
    r = client.post(
        "/ctx",
        json={
            "query": "project phoenix stack",
            "include_profile": False,
            "include_memories": False,
            "budget": 1000,
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    block = body["block"]
    assert "BEGIN memex-context" in block
    assert "END memex-context" in block
    assert "Project Phoenix" in block
    assert body["tokens"] > 0


def test_ctx_respects_budget(client: TestClient, cfg: Config):
    _seed(cfg)
    small = client.post(
        "/ctx",
        json={"query": "phoenix", "budget": 50, "include_profile": False, "include_memories": False},
    ).json()
    big = client.post(
        "/ctx",
        json={"query": "phoenix", "budget": 5000, "include_profile": False, "include_memories": False},
    ).json()
    assert small["tokens"] < big["tokens"]


# ---------------------------------------------------------------------------
# auth
# ---------------------------------------------------------------------------


def test_bearer_token_enforced(cfg: Config, monkeypatch):
    monkeypatch.setenv("MEMEX_API_TOKEN", "sekrit")
    app = build_app(str(cfg.root))
    c = TestClient(app)

    # /healthz stays open even with a token configured
    assert c.get("/healthz").status_code == 200

    # /status requires the token
    assert c.get("/status").status_code == 401
    assert c.get("/status", headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert c.get("/status", headers={"Authorization": "Bearer sekrit"}).status_code == 200


# ---------------------------------------------------------------------------
# memories — only when OPENAI_API_KEY is set (mem0 needs an LLM)
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    not os.environ.get("OPENAI_API_KEY"),
    reason="mem0 fact extraction needs an LLM",
)
def test_mem_add_search_roundtrip_http(client: TestClient):
    r = client.post("/mem/add", json={"text": "Prefers TypeScript for services", "category": "pref"})
    assert r.status_code == 200, r.text
    ids = r.json()["ids"]
    assert ids

    r = client.get("/mem/search", params={"q": "typescript preference", "k": 3})
    assert r.status_code == 200
    texts = [m["text"].lower() for m in r.json()["memories"]]
    assert any("typescript" in t for t in texts)

    r = client.get("/mem/profile")
    assert r.status_code == 200
    assert "About the user" in r.json()["block"]
