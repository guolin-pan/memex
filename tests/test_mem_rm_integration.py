"""End-to-end tests for `memex mem rm` (CLI) and `DELETE /mem/{id}` (HTTP).

The bug these tests guard against:

  $ memex mem ls
  c57ed1036c5a │ fact │ ...        ← table shows the LAST 12 chars
  $ memex mem rm c57ed1036c5a       ← user pastes that
  ValueError: Memory with id c57ed1036c5a not found

`mem ls` prints the 12-char suffix for compactness, but mem0 stores full
ids. Our resolver maps short suffixes back to canonical ids; this file
exercises that flow at the CLI and HTTP layers WITHOUT booting real mem0
(so the tests run on every CI without OPENAI_API_KEY).

We mock at the MemStore level: `mem_store.MemStore` is patched with a tiny
in-memory stand-in that matches mem0's actual error contract.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from memex.cli import app as memex_app
from memex.commands import mem_cmd
from memex.core.config import Config
from memex.server import api as server_api


# Same fake mem0 used by tests/test_mem_store.py.  Duplicated here on purpose
# so this test file is readable in isolation.
class _FakeMemory:
    def __init__(self, memories):
        self._mems = list(memories)
        self.deleted_ids: list[str] = []

    def delete(self, *, memory_id):
        for i, m in enumerate(self._mems):
            if m["id"] == memory_id:
                self._mems.pop(i)
                self.deleted_ids.append(memory_id)
                return {"message": "Memory deleted successfully!"}
        raise ValueError(f"Memory with id {memory_id} not found")

    def get(self, *, memory_id):
        for m in self._mems:
            if m["id"] == memory_id:
                return m
        return None

    def get_all(self, **_):
        return list(self._mems)

    def delete_all(self, **_):
        self._mems.clear()


@pytest.fixture()
def seeded_memstore(cfg: Config, monkeypatch):
    """Patch MemStore so its lazy `.memory` returns a fake with two memories."""
    from memex.backends import mem_store

    full_a = "aaaaaaaa-bbbb-cccc-dddd-c57ed1036c5a"
    full_b = "bbbbbbbb-bbbb-cccc-dddd-1234567890ab"
    items = [
        {"id": full_a, "memory": "first fact", "metadata": {"category": "fact"}},
        {"id": full_b, "memory": "second fact", "metadata": {"category": "fact"}},
    ]
    fake = _FakeMemory(items)

    # Patch the lazy `memory` property at the class level so both the CLI's
    # and the FastAPI app's MemStore instances see the same fake.
    monkeypatch.setattr(
        mem_store.MemStore,
        "memory",
        property(lambda self: fake),
    )
    return fake, full_a, full_b


# ---------------------------------------------------------------------------
# CLI: `memex mem rm`
# ---------------------------------------------------------------------------


runner = CliRunner()


def test_cli_mem_rm_accepts_short_suffix(seeded_memstore, memex_root: Path):
    fake, full_a, _ = seeded_memstore
    res = runner.invoke(memex_app, ["-R", str(memex_root), "mem", "rm", "c57ed1036c5a"])
    assert res.exit_code == 0, res.output
    assert "deleted" in res.output
    assert full_a in fake.deleted_ids


def test_cli_mem_rm_accepts_full_id(seeded_memstore, memex_root: Path):
    fake, full_a, _ = seeded_memstore
    res = runner.invoke(memex_app, ["-R", str(memex_root), "mem", "rm", full_a])
    assert res.exit_code == 0, res.output
    assert full_a in fake.deleted_ids


def test_cli_mem_rm_unknown_id_exits_1_with_hint(seeded_memstore, memex_root: Path):
    res = runner.invoke(memex_app, ["-R", str(memex_root), "mem", "rm", "does-not-exist"])
    assert res.exit_code == 1
    assert "not found" in res.output.lower()
    assert "mem ls --json" in res.output, (
        "error message must include an actionable hint, got:\n" + res.output
    )


def test_cli_mem_rm_ambiguous_suffix_exits_1(cfg: Config, monkeypatch, memex_root: Path):
    """If two stored ids share the same 12-char suffix, fail with 'Ambiguous'."""
    from memex.backends import mem_store

    suffix = "1234567890ab"
    a = "aaaaaaaa-bbbb-cccc-dddd-" + suffix
    b = "bbbbbbbb-bbbb-cccc-dddd-" + suffix
    fake = _FakeMemory(
        [
            {"id": a, "memory": "x", "metadata": {"category": "fact"}},
            {"id": b, "memory": "y", "metadata": {"category": "fact"}},
        ]
    )
    monkeypatch.setattr(
        mem_store.MemStore, "memory", property(lambda self: fake)
    )

    res = runner.invoke(memex_app, ["-R", str(memex_root), "mem", "rm", suffix])
    assert res.exit_code == 1
    assert "ambiguous" in res.output.lower()


# ---------------------------------------------------------------------------
# HTTP: DELETE /mem/{mem_id}, GET /mem/{mem_id}
# ---------------------------------------------------------------------------


@pytest.fixture()
def http_client(seeded_memstore, cfg: Config) -> TestClient:
    app = server_api.build_app(str(cfg.root))
    return TestClient(app)


def test_http_delete_mem_accepts_short_suffix(http_client: TestClient, seeded_memstore):
    fake, full_a, _ = seeded_memstore
    r = http_client.delete("/mem/c57ed1036c5a")
    assert r.status_code == 200, r.text
    assert r.json()["deleted"] == "c57ed1036c5a"
    assert full_a in fake.deleted_ids


def test_http_delete_mem_accepts_full_id(http_client: TestClient, seeded_memstore):
    fake, full_a, _ = seeded_memstore
    r = http_client.delete(f"/mem/{full_a}")
    assert r.status_code == 200, r.text
    assert full_a in fake.deleted_ids


def test_http_delete_mem_unknown_returns_404(http_client: TestClient):
    r = http_client.delete("/mem/does-not-exist")
    assert r.status_code == 404, r.text
    detail = r.json()["detail"]
    assert "not found" in detail.lower()
    assert "mem ls --json" in detail  # actionable hint reaches API consumers


def test_http_delete_mem_ambiguous_returns_409(
    cfg: Config, monkeypatch
):
    """Two memories sharing the same 12-char suffix → 409 Conflict on suffix delete."""
    from memex.backends import mem_store

    suffix = "1234567890ab"
    a = "aaaaaaaa-bbbb-cccc-dddd-" + suffix
    b = "bbbbbbbb-bbbb-cccc-dddd-" + suffix
    fake = _FakeMemory(
        [
            {"id": a, "memory": "x", "metadata": {}},
            {"id": b, "memory": "y", "metadata": {}},
        ]
    )
    monkeypatch.setattr(
        mem_store.MemStore, "memory", property(lambda self: fake)
    )

    client = TestClient(server_api.build_app(str(cfg.root)))
    r = client.delete(f"/mem/{suffix}")
    assert r.status_code == 409, r.text
    assert "ambiguous" in r.json()["detail"].lower()


def test_http_get_mem_accepts_short_suffix(http_client: TestClient, seeded_memstore):
    _fake, full_a, _ = seeded_memstore
    r = http_client.get("/mem/c57ed1036c5a")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["id"] == full_a
    assert body["text"] == "first fact"


def test_http_get_mem_unknown_returns_404(http_client: TestClient):
    r = http_client.get("/mem/does-not-exist")
    assert r.status_code == 404


def test_http_get_mem_ambiguous_returns_409(cfg: Config, monkeypatch):
    from memex.backends import mem_store

    suffix = "1234567890ab"
    a = "aaaaaaaa-bbbb-cccc-dddd-" + suffix
    b = "bbbbbbbb-bbbb-cccc-dddd-" + suffix
    fake = _FakeMemory(
        [
            {"id": a, "memory": "x", "metadata": {}},
            {"id": b, "memory": "y", "metadata": {}},
        ]
    )
    monkeypatch.setattr(
        mem_store.MemStore, "memory", property(lambda self: fake)
    )

    client = TestClient(server_api.build_app(str(cfg.root)))
    r = client.get(f"/mem/{suffix}")
    assert r.status_code == 409


def test_http_delete_all_unaffected(http_client: TestClient, seeded_memstore):
    """`DELETE /mem/all` must still be the explicit wipe path, NOT routed
    through the resolver (which would 404 because 'all' isn't a stored id)."""
    fake, _full_a, _ = seeded_memstore
    r = http_client.delete("/mem/all")
    assert r.status_code == 200
    assert r.json()["deleted"] == "all"
    assert len(fake._mems) == 0


# Silence the unused-import warnings emitted when running the test file
# in isolation; pyflakes via ruff thinks `mem_cmd` isn't used, but it's
# imported for side effects (CLI registration check).
assert mem_cmd is not None
