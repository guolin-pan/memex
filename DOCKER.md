# memex — Docker deployment

This guide covers running `memex` as a long-lived service in a container,
backed by a persistent host volume. The CLI you already know (`memex`) talks
to the same data when it shells into the container; LLMs / Cursor subagents
talk to it via the **HTTP API** (and the `memex client` subcommand).

```
┌────────────────────┐    HTTP (FastAPI)    ┌──────────────────────────────────┐
│  LLM / Cursor      │ ───────────────────▶ │  memex container                  │
│  (memex client …)  │                      │  uvicorn :8000                    │
└────────────────────┘                      │                                   │
                                            │  /opt/memex/models/  ◀── baked    │
                                            │  ├── chroma/onnx_models/          │
                                            │  └── hf/    (sentence-transformers│
                                            │              all-MiniLM-L6-v2)    │
                                            │                                   │
                                            │  /data/             ◀── volume    │
                                            └──────────────────────────────────┘
                                                       │
                                              ./data/  │  on host
                                              ├── docs/                # markdown wiki (git)
                                              ├── memex.yaml           # config
                                              └── .cache/
                                                  ├── chroma/          # vector store DATA
                                                  └── mem0/            # qdrant + history.db
```

> The container starts with **zero network calls** — both embedding models are
> baked into the image at build time. The `/data` volume only holds your actual
> content (docs, vectors, memories).

---

## 1. Build & run

```bash
cd memex/
cp .env.example .env                    # set MEMEX_API_TOKEN, OPENAI_API_KEY, …

docker compose build                    # ~5-10 min on first build (downloads models)
docker compose up -d

# Sanity checks
curl -fsS http://localhost:8000/healthz
curl -fsS http://localhost:8000/        # service banner
open    http://localhost:8000/docs       # OpenAPI UI (Swagger)
```

### Automated build + E2E test

A self-contained script verifies the whole image end-to-end — build, model
baking, offline boot, full API surface, persistence across restart, and
bearer-token auth — in one command:

```bash
bash scripts/docker-build-test.sh                # full build + tests (~10-15 min)
FAST=1     bash scripts/docker-build-test.sh    # skip rebuild if memex:e2e exists
```

Pre-reqs: docker daemon access (you're in the `docker` group **or** running under sudo) plus `curl` and `jq` on the host.

### What's in the image

The Dockerfile is intentionally a single, fully-loaded variant. Image size
(~1.5-2 GB compressed) is the accepted cost of guaranteed offline operation —
**no model fetch ever happens at container start**.

Everything baked at `/opt/memex/models/` (plus spaCy as a venv package):

| Asset                                                   | Used by                                                |
|---------------------------------------------------------|--------------------------------------------------------|
| `chroma/onnx_models/all-MiniLM-L6-v2/`                  | ChromaDB default embedder (`embedder.provider: chroma-default`) |
| `hf/hub/models--sentence-transformers--all-MiniLM-L6-v2/` | mem0 HuggingFace embedder backend (full snapshot)      |
| `fastembed/...Qdrant--bm25...`                           | mem0/qdrant BM25 keyword search                       |
| `tiktoken/...`                                           | memex chunking / token counting (`cl100k_base`)        |
| spaCy `en_core_web_sm` (installed into `/opt/venv`)     | mem0 lemmatization + entity extraction (`mem0ai[nlp]`) |
| CPU-only PyTorch                                        | sentence-transformers / mem0 huggingface backend       |

`HF_HUB_OFFLINE=1`, `TRANSFORMERS_OFFLINE=1`, `HF_DATASETS_OFFLINE=1`,
`FASTEMBED_CACHE_PATH`, and `TIKTOKEN_CACHE_DIR` are all set in the image so
the HF / fastembed / tiktoken stacks refuse network and serve from the baked
caches.

If you ever want to fetch a *different* HF model at runtime, override the
offline flags via env:

```bash
docker compose run --rm \
  -e HF_HUB_OFFLINE=0 \
  -e TRANSFORMERS_OFFLINE=0 \
  memex python -c "from sentence_transformers import SentenceTransformer; SentenceTransformer('bge-small-en-v1.5')"
```

After the first start, `./data/` on the host is populated with:

```
./data/
├── docs/                # empty subdirs (inbox/, projects/, ...)
├── memex.yaml           # default config (OpenAI profile)
└── .cache/              # chroma + mem0 storage (gitignore this!)
```

### Build caching — why source edits rebuild in seconds, not minutes

The Dockerfile is structured into three layered stages inside the builder
so an everyday code change rebuilds in **~40 seconds** instead of repeating
the full ~25 min model-download dance:

| Stage | What it does                                                                                                | Cache key                |
|-------|-------------------------------------------------------------------------------------------------------------|--------------------------|
| **A** | Resolve and install every Python dependency (`torch` CPU, `chromadb`, `sentence-transformers`, `mem0ai[nlp]`, `fastembed`, `tiktoken`, …) using a *stub* `memex/__init__.py` so pip never sees your source tree | `pyproject.toml`, `README.md` |
| **B** | Pre-warm every model: spaCy `en_core_web_sm`, ChromaDB ONNX, HF `all-MiniLM-L6-v2` (full snapshot), fastembed `Qdrant/bm25`, tiktoken `cl100k_base` | same as A             |
| **C** | `COPY memex/` + `COPY templates/` + `pip install --no-deps .` — the only step that touches your actual code   | `memex/`, `templates/`    |

Routine edits invalidate only Stage C. Stage A/B layers stay cached.
`pyproject.toml` changes are the rare event that costs a full rebuild, and
that's correct — adding a dep DOES require re-resolving the venv.

BuildKit cache mounts (`--mount=type=cache,target=/root/.cache/pip`) also
preserve pip's download cache across builds, so even when Stage A *does*
rebuild it skips the slow downloads.

---

## 2. Configure

### 2.1 Picking an LLM / embedder profile

Edit `./data/memex.yaml` directly — it's mounted into the container, no rebuild
needed. The two ready-made profiles are:

**Profile A — OpenAI cloud** (default after first start):
```yaml
embedder: { provider: openai, model: text-embedding-3-small }
llm:      { provider: openai, model: gpt-4o-mini, temperature: 0.1 }
```
Requires `OPENAI_API_KEY` in `.env`.

**Profile B — Fully local** (offline embeddings + Ollama or any OpenAI-compatible LLM):
```yaml
embedder: { provider: chroma-default, model: all-MiniLM-L6-v2 }
llm:
  provider: openai
  model: qwen3:4b
  base_url: http://10.242.29.48:11434/v1
  api_key: no-key
```

To reset the config to profile B from scratch:
```bash
docker compose exec memex memex init --profile local --force
```

### 2.2 Authentication

By default the API is open (suitable for `127.0.0.1` / private networks). To
require a bearer token on every request:

```bash
# .env
MEMEX_API_TOKEN=$(openssl rand -hex 32)
```

```bash
docker compose up -d --force-recreate

curl -fsS -H "Authorization: Bearer $MEMEX_API_TOKEN" \
     http://localhost:8000/status
```

`/healthz` stays open so the container's HEALTHCHECK still works.

### 2.3 Persistence guarantees

- All durable state is under `/data` inside the container, which is bind-mounted
  to `./data/` on the host.
- Stopping / recreating the container is safe; rebuilding the image is safe.
- To back up: snapshot `./data/`. To restore: replace `./data/` with your snapshot.
- Or use the CLI: `docker compose exec memex memex backup -o /data/snapshot.tar.gz`.

### 2.4 Port / host

```bash
# .env
MEMEX_PORT=18000          # publishes container :8000 → host :18000
```

---

## 3. Day-to-day operations

```bash
# Tail logs
docker compose logs -f memex

# Open a shell in the container
docker compose exec memex bash

# Run the full local CLI inside the container (it acts on the same /data)
docker compose exec memex memex status
docker compose exec memex memex doc ls
docker compose exec memex memex doc reindex --changed

# Snapshot to a tarball (lands inside the volume, available on host as ./data/)
docker compose exec memex memex backup -o /data/snapshot-$(date +%F).tar.gz
```

---

## 4. Using the API from LLMs / Cursor subagents

There are two equivalent ways for an LLM / Cursor agent to read & write data:

### Option A — the `memex client` CLI (recommended for shell-tool agents)

Install `memex` on the agent's machine and point it at the container:

```bash
pipx install memex                           # or: pip install -e <path>
export MEMEX_API_URL=http://localhost:8000   # the running container
export MEMEX_API_TOKEN=...                   # only if you set one

memex client status
memex client doc search "postgres tuning" -k 3
echo "# notes from today\n…" | memex client doc add - --title "Today" --tags work
memex client mem add "I prefer pgvector for hybrid search" --category pref
memex client ctx "what's our project-x stack?" --write /tmp/ctx.md --budget 2000
```

The `memex client` surface is intentionally a **subset** of the local CLI — only
operations safe to expose over HTTP (no `init`, no `watch`, no `cursor *`).

### Option B — Raw HTTP (any language)

```bash
curl -fsS -X POST http://localhost:8000/doc/add \
     -H "Content-Type: application/json" \
     -H "Authorization: Bearer $MEMEX_API_TOKEN" \
     -d '{"body":"# Hello\n\nFirst note.\n","title":"Hello","tags":["intro"]}'

curl -fsS "http://localhost:8000/doc/search?q=hello&k=3" \
     -H "Authorization: Bearer $MEMEX_API_TOKEN"
```

OpenAPI / Swagger UI is at `/docs`.

### Wiring Cursor subagents to the remote

The shipped subagents (`/memex-ask`, `/memex-archive`, `/memex-curator`) call
the `memex` CLI directly. To point them at a remote container instead:

1. Install `memex` on the dev machine (same as Option A above).
2. Add the env vars to your shell rc (or a project `.envrc`):
   ```bash
   export MEMEX_API_URL=http://localhost:8000
   export MEMEX_API_TOKEN=...
   ```
3. Edit `~/.cursor/agents/memex-*.md` (the file installed by
   `memex cursor install-agents`) and add an instruction at the top of each:
   > "Always prefix `memex …` invocations with `client`, e.g. `memex client
   > doc search`, because this deployment uses the HTTP backend."

Or, if you only ever use the remote backend, alias `memex` to `memex client`
in your shell and leave the agent files unchanged.

---

## 5. Endpoint reference (the essentials)

| Method | Path                          | What it does                                        |
|--------|-------------------------------|-----------------------------------------------------|
| GET    | `/healthz`                    | Liveness, never authenticated                       |
| GET    | `/`                           | Banner (version, root, whether token is required)   |
| GET    | `/status`                     | Doc count, chunk count, sizes, providers            |
| POST   | `/doc/add`                    | `{body, title?, tags?, subdir?}`                    |
| GET    | `/doc?tag=…&since=…`          | List docs                                           |
| GET    | `/doc/{ident}`                | Show a doc by id, slug, or path                     |
| DELETE | `/doc/{ident}?keep_file=…`    | Remove from index (and disk by default)             |
| GET    | `/doc/search?q=…&k=…`         | Hybrid (vector + BM25) search                       |
| POST   | `/doc/reindex?all=true`       | Reindex changed (or all) docs                       |
| POST   | `/mem/add`                    | `{text, category, tags?}`                           |
| GET    | `/mem?category=…`             | List memories                                       |
| GET    | `/mem/profile`                | "About the user" block                              |
| GET    | `/mem/search?q=…&k=…`         | Semantic search                                     |
| GET    | `/mem/{id}`                   | Show one                                            |
| DELETE | `/mem/{id}`                   | Delete (or `id=all` to wipe)                        |
| POST   | `/ctx`                        | `{query, budget?, top_k_docs?, ...}` → context block |
| GET    | `/docs`                       | OpenAPI UI (Swagger)                                |

---

## 6. Troubleshooting

| Symptom                                | Likely cause                                                    | Fix                                                                                            |
|----------------------------------------|------------------------------------------------------------------|------------------------------------------------------------------------------------------------|
| `OSError: ... offline mode`            | A different HF model than the one baked at build time is being requested | Either bake it into the image too (extend the Dockerfile's pre-warm step) or runtime-override `-e HF_HUB_OFFLINE=0 -e TRANSFORMERS_OFFLINE=0` and provide network access |
| `401 invalid or missing bearer token`  | `MEMEX_API_TOKEN` is set but the request omits the header        | Send `Authorization: Bearer <token>` (or unset the env var to make the API open)               |
| `/mem/*` returns `500 OPENAI_API_KEY`  | LLM profile is `openai` (cloud) but no key in `.env`             | Either set `OPENAI_API_KEY` or switch `memex.yaml` to a local LLM endpoint                     |
| Container restarts in a loop           | Volume `./data/` has wrong ownership                             | `sudo chown -R 1000:1000 ./data` (matches the in-container `memex` user)                       |
| `Failed to download chroma onnx model` | Container has no network AND the baked model dir was clobbered by a bind mount or a manual `rm` | Rebuild the image; do not mount over `/opt/memex/models`                                       |

---

## 7. Single-host best practice

For a single host with multiple users / shared agent:

1. Put `./data/` on a backed-up disk (ZFS / Btrfs snapshots are great).
2. Set `MEMEX_API_TOKEN` even on localhost — agents running in containers may
   leak it less obviously than your shell would, but enabling auth costs nothing.
3. Run `memex backup -o /data/snapshots/...` on a cron / systemd timer.
4. If the LLM profile is OpenAI-compatible self-hosted (Ollama / vLLM /
   LiteLLM), uncomment the Ollama service in `docker-compose.yml` so the API
   can address it as `http://ollama:11434/v1` on the compose network — no host
   exposure needed.
