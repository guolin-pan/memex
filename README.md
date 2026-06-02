# memex

> *"Consider a future device for individual use, which is a sort of mechanized private file and library… A memex is a device in which an individual stores all his books, records, and communications, and which is mechanized so that it may be consulted with exceeding speed and flexibility."* — Vannevar Bush, *As We May Think*, 1945

`memex` is a personal assistant + Markdown knowledge base. It pairs two layers:

| Backend  | Stores                                | What it's good for                                |
|----------|---------------------------------------|---------------------------------------------------|
| **mem0** | Short user facts, preferences, decisions | "Know the user" — a few hundred high-signal facts |
| **ChromaDB** | Markdown wiki, chunked + embedded | "Know the world" — your curated notes (RAG)       |

…behind one CLI (`memex`) and one HTTP API (`memex serve`) you can address from anywhere — your shell, a Cursor subagent, or a Docker container.

Built on:
- [mem0](https://github.com/mem0ai/mem0) (OSS, library mode) — long-term personal memory.
- [ChromaDB](https://github.com/chroma-core/chroma) — local vector store.
- [Typer](https://typer.tiangolo.com/) — CLI surface.
- [FastAPI](https://fastapi.tiangolo.com/) + [uvicorn](https://www.uvicorn.org/) — HTTP API.
- Cursor [hooks](https://docs.cursor.com/hooks), [rules](https://docs.cursor.com/rules), and [subagents](https://docs.cursor.com/subagents) — context injection into chat. **No MCP server required.**

---

## Install

### Prerequisites

- Python **3.10+** (3.11 recommended; the test suite runs on 3.11).
- `git` (for `memex init`'s `git init`).
- Optional: `Docker` ≥ 20.10 + `docker compose` v2 for containerized deployment.

### From source — local dev

```bash
git clone <repo>
cd memex

# create + activate a virtualenv
python3 -m venv .venv
source .venv/bin/activate

# core install (editable)
pip install -e .

# optional extras
pip install -e ".[dev]"        # pytest + ruff (for contributing)
pip install -e ".[local]"      # sentence-transformers (offline embeddings; ~800 MB torch)
```

### As a user (no source)

```bash
pipx install .           # isolated env, recommended
# or
pip install .            # straight into your active env
```

After install you have:

```bash
memex --version
memex --help
```

### Quick env requirements per profile

| Profile                | What you need set                                                |
|------------------------|------------------------------------------------------------------|
| **openai** (default)   | `OPENAI_API_KEY` in env                                          |
| **local** (offline)    | Nothing for embeddings (ONNX MiniLM auto-downloaded). For mem0 LLM, point `llm.base_url` at any OpenAI-compatible endpoint (Ollama, vLLM, LM Studio, …) |

---

## Profiles

`memex init` ships two ready-made configs (you can always edit `memex.yaml` later):

- `--profile openai` (default) — OpenAI cloud for both embeddings and the fact-extraction LLM.
- `--profile local` — offline ONNX embeddings + any OpenAI-compatible LLM endpoint. Template points at `http://10.242.29.48:11434/v1` with `qwen3:4b` — change in `memex.yaml`.

```bash
memex init --profile local -u <your-handle>
```

The relevant config fields in `~/memex/memex.yaml`:

```yaml
embedder:
  provider: chroma-default | openai | sentence-transformers
  model: <model name>
  base_url: <only for provider: openai with a custom endpoint>
  api_key:  <only for provider: openai>
llm:
  provider: openai | ollama
  model: <model name>
  base_url: <OpenAI-compatible endpoint, e.g. http://host:11434/v1>
  api_key:  <token; use "no-key" for Ollama's open endpoint>
```

---

## Quick start (local CLI)

```bash
# 1. Initialize the memex root (default: ~/memex). Try --profile local if you have
#    no OpenAI key but do have an OpenAI-compatible LLM endpoint.
memex init

# 2. Add a doc (stdin or a file path)
echo "# Postgres tuning

Work mem matters for analytic queries." | \
  memex doc add - --title "Postgres tuning" --tags db,reference

# 3. Search
memex doc search "postgres work_mem" -k 3

# 4. Tell mem0 something about yourself
memex mem add "Prefers TypeScript over JS for new services" --category pref

# 5. Build a unified context block for a question (this is what Cursor hooks call)
memex ctx "What's our project-x stack?"

# 6. Inspect health
memex status

# 7. Snapshot the wiki (docs/ + memex.yaml). Add --include-cache to also
#    snapshot the Chroma and mem0 stores.
memex backup -o ~/memex-snap.tar.gz
```

---

## Layout

The CLI manages a single root directory (default `~/memex`):

```
~/memex/
├── docs/                      # Git repo of markdown files (your wiki)
│   ├── inbox/                 # `memex doc add` lands here by default
│   ├── projects/
│   ├── people/
│   ├── work/
│   ├── learning/
│   └── reference/
├── .kbignore                  # Like .gitignore, but for indexing
├── memex.yaml                 # Global config (paths, providers, budgets)
└── .cache/                    # gitignored
    ├── chroma/                # ChromaDB persistent store (wiki vectors)
    ├── mem0/                  # mem0 OSS data (qdrant + history)
    └── history/               # tombstones, audit log
```

Every Markdown file has a YAML frontmatter block (auto-filled by `memex`):

```yaml
---
id: 01HZ...           # ULID, stable, never changes
title: Postgres tuning
tags: [db, reference]
created: 2026-01-01T10:00:00
updated: 2026-01-15T09:30:00
source: manual
content_hash: sha256:...
links: []
---
```

---

## HTTP API + remote client (NEW)

`memex` now ships an HTTP API and a thin client subcommand. This is what makes
Docker deployment + LLM/agent shell-tool calls possible.

### Start the server (local)

```bash
memex serve --host 127.0.0.1 --port 8000
# OpenAPI UI:    http://127.0.0.1:8000/docs
# Liveness:      http://127.0.0.1:8000/healthz
# Banner:        http://127.0.0.1:8000/
```

### Use it from anywhere with the client

The `memex client` subcommand is a thin httpx wrapper. LLMs / Cursor subagents
shell out to it; nothing local-only required.

```bash
export MEMEX_API_URL=http://127.0.0.1:8000      # which server to talk to
export MEMEX_API_TOKEN=<token>                  # only if the server set MEMEX_API_TOKEN

memex client status
memex client doc search "postgres tuning" -k 3
echo "# today" | memex client doc add - --title "Today" --tags work
memex client mem add "Prefers pgvector for hybrid search" --category pref
memex client ctx "what's our project-x stack?" --write /tmp/ctx.md --budget 2000
memex client raw GET /healthz                   # debugging escape hatch
```

The client surface intentionally exposes only the operations safe to run over
HTTP — `init`, `watch`, `cursor *`, `backup` stay local-only.

---

## Docker deployment

A multi-stage `Dockerfile` and `docker-compose.yml` ship with the project for a
persistent, networked deployment.

```bash
cp .env.example .env             # set MEMEX_API_TOKEN, OPENAI_API_KEY, …
docker compose up -d --build

curl http://localhost:8000/healthz
open http://localhost:8000/docs   # OpenAPI / Swagger UI

# Or run the full automated build + E2E test in one shot:
bash scripts/docker-build-test.sh
```

- **Persistence** — host bind mount `./data → /data` keeps `docs/`, `memex.yaml`, ChromaDB, mem0, and the HF cache across restarts.
- **Config** — edit `./data/memex.yaml` directly; the container reads it on next request, no rebuild.
- **Auth** — set `MEMEX_API_TOKEN` in `.env` to require `Authorization: Bearer <token>` on every endpoint except `/healthz`.
- **Sidecar Ollama** — uncomment the `ollama` service in `docker-compose.yml` to colocate a local LLM; point `llm.base_url` at `http://ollama:11434/v1`.

Full guide with troubleshooting, endpoint reference, and "wire Cursor
subagents to the remote" recipe: **[DOCKER.md](DOCKER.md)**.

---

## Cursor integration (no MCP)

Three channels, shell-driven, complementary. Use one, two, or all three.

### Channel A — Hooks (passive, always-on context injection)

```bash
memex cursor install-hooks
```

Drops `~/.cursor/hooks.json` wiring:

- `sessionStart` → `memex mem profile --write …` (stable user profile)
- `beforeSubmitPrompt` → `memex ctx "$CURSOR_USER_PROMPT" --write … --budget 2000`
- `sessionEnd` → `memex mem learn --from-cursor-transcript`

### Channel B — Project rule (teach the main agent the read-side surface)

```bash
memex cursor install-rule .
```

The slimmed rule covers two things only: how to use the auto-injected
`<!-- BEGIN memex-context -->` block, and how to do quick read-only
`memex doc search` / `memex mem search` lookups. All writes/maintenance are
delegated to the subagents below.

### Channel C — Subagents (user-invoked, focused)

Three focused subagents. Invoke from chat with `/memex-ask`, `/memex-archive`,
or `/memex-curator`.

```bash
memex cursor install-agents --scope user                          # available in every project
memex cursor install-agents --scope project --project-root .      # committed with the repo
```

| Agent              | `readonly` | Use when…                                                                              |
|--------------------|------------|----------------------------------------------------------------------------------------|
| `/memex-ask`       | true       | Question answered by the user's notes / memories. Cites sources, never invents.        |
| `/memex-archive`   | false      | "save this", "archive that", "remember X". Always previews & confirms before writing.  |
| `/memex-curator`   | false      | "clean duplicates", "stale notes", "health-check the memex". Surveys first, asks per-action. |

Other utility commands:

```bash
memex cursor list-agents
memex cursor print-agent memex-ask
memex cursor print-hooks
memex cursor print-rule
```

Cursor only documents the frontmatter fields `name`, `description`, `model`,
`readonly`, `is_background`; there's no per-subagent shell allow-list. To
restrict what the write-side agents can run, layer your workspace-level
permissions in `~/.cursor/cli-config.json` / `.cursor/cli.json` on top of them.

---

## Privacy

Everything stays on disk. To go fully offline, edit `memex.yaml`:

```yaml
embedder:
  provider: chroma-default          # offline ONNX MiniLM, no API key
  model: all-MiniLM-L6-v2
llm:
  provider: openai                  # speaks OpenAI v1 protocol
  model: qwen3:4b
  base_url: http://localhost:11434/v1
  api_key: no-key
```

The same config works in Docker — just edit `./data/memex.yaml` (or use
`memex init --profile local --force` inside the container).

---

## Command surface

```
memex init [DIR] [-u USER_ID] [-p PROFILE] [--no-git] [-f]   # PROFILE: openai|local

# Wiki
memex doc add [PATH|-] [-t TITLE] [--tags T1,T2] [-d SUBDIR] [--open]
memex doc update PATH
memex doc edit IDENT
memex doc rm IDENT [--keep-file]
memex doc ls [--tag T] [--since DUR] [--json]
memex doc show IDENT [--raw]
memex doc search QUERY [-k N] [--tag T] [--since DUR] [--json] [--snippet-tokens N]
memex doc reindex [--all|--changed]
memex doc watch [--debounce SECS]
memex doc graph                                              # emits mermaid

# Personal memory (mem0)
memex mem add TEXT [-c CATEGORY] [--tag T]
memex mem ls [-c CATEGORY] [--json]
memex mem show ID
memex mem update ID TEXT
memex mem rm ID|all [-y]
memex mem search QUERY [-k N] [-c CATEGORY] [--json]
memex mem profile [--write PATH] [--max-items N]
memex mem learn [--from PATH|-] [--from-cursor-transcript] [-c CATEGORY]

# Unified context block (what hooks call)
memex ctx QUERY [--write PATH] [--budget TOKENS] [-k DOCS] [--top-k-mems N]
                [--no-profile] [--no-memories] [--no-docs]

# Cursor integration
memex cursor install-hooks  [--target PATH] [--merge|--replace] [--force]
memex cursor install-rule   [PROJECT_ROOT] [--force]
memex cursor install-agents [-s user|project] [--project-root DIR] [--only NAME ...] [-f]
memex cursor list-agents
memex cursor print-hooks | print-rule | print-agent NAME

# Operations
memex status [--json]
memex backup [-o PATH] [--include-cache]
memex restore ARCHIVE [--target DIR]

# Server / client (NEW)
memex serve  [--host H] [--port P] [--reload] [--workers N] [-R ROOT]
memex client status [--json]
memex client ctx QUERY [--budget N] [-k K] [--write PATH] [--no-profile|--no-memories|--no-docs]
memex client doc add [PATH|-] [-t TITLE] [--tags T1,T2] [-d SUBDIR]
memex client doc search QUERY [-k N] [--tag T] [--since DUR] [--json]
memex client doc ls [--tag T] [--since DUR] [--json]
memex client doc show IDENT
memex client doc rm IDENT [--keep-file]
memex client mem add TEXT [-c CATEGORY] [--tag T]
memex client mem search QUERY [-k N] [-c CATEGORY] [--json]
memex client mem ls [-c CATEGORY] [--json]
memex client mem show ID
memex client mem rm ID|all
memex client mem profile [--write PATH] [--max-items N]
memex client raw METHOD PATH [--body JSON]                  # debugging escape hatch
```

All commands accept the global `-R/--root` (or `$MEMEX_ROOT`) for the data directory.
All `memex client …` commands read `MEMEX_API_URL` (default `http://127.0.0.1:8000`)
and `MEMEX_API_TOKEN` from the environment.

---

## Testing

```bash
pip install -e ".[dev]"
pytest -q                       # full suite (~70 tests)
ruff check memex/ tests/         # lint
```

Live mem0 tests are skipped unless `OPENAI_API_KEY` is set.

---

## Project plan

See [`personal-memex-mem0-cli`](../.cursor/plans/personal-memex-mem0-cli_792ff940.plan.md).
