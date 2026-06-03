# Docker deployment

This page is the short, opinionated version. For the full operations runbook (multi-host, troubleshooting matrix, Ollama sidecar wiring, endpoint reference) see [`../DOCKER.md`](../DOCKER.md).

## What you get

```
+-------------------+         +-------------------------------------------+
|  caller (LLM,     |  HTTP   |  memex container                          |
|  Cursor agent,    | ------> |  uvicorn :8000                            |
|  CI bot, curl)    |         |                                           |
+-------------------+         |  /opt/memex/models/   <-- BAKED at build  |
                              |     chroma/onnx_models/...                |
                              |     hf/hub/...sentence-transformers...    |
                              |                                           |
                              |  /data/             <-- HOST VOLUME       |
                              |     docs/                                 |
                              |     memex.yaml                            |
                              |     .cache/chroma/   (your vectors)       |
                              |     .cache/mem0/     (qdrant + history)   |
                              +-------------------------------------------+
                                              |
                                              v
                                        host: ./data/
```

Two things move:

- **Image** (immutable): code + venv + offline models. Rebuilt only when you change the build.
- **Volume** (`./data` on the host): everything you create through the running container.

## Build

```bash
cd memex/
cp .env.example .env                          # set MEMEX_API_TOKEN, OPENAI_API_KEY, ...
docker compose build                          # ~5-10 min first time
```

### What's baked into the image

There is a **single variant** by design — every model the runtime might
load is included, so a started container never reaches out to the network
for model files. Image size (~1.5-2 GB) is the explicit, accepted cost.

| Bundle | Used by |
|---|---|
| CPU-only PyTorch (from PyTorch's CPU wheel index)                                            | sentence-transformers, mem0 HF embedder |
| sentence-transformers package                                                                  | mem0's HuggingFace embedder |
| ChromaDB ONNX `all-MiniLM-L6-v2` at `/opt/memex/models/chroma/onnx_models/`                     | wiki vector layer when `embedder.provider: chroma-default` |
| HF `sentence-transformers/all-MiniLM-L6-v2` (full snapshot) at `/opt/memex/models/hf/`         | mem0's HuggingFace embedder backend |
| fastembed `Qdrant/bm25` at `/opt/memex/models/fastembed/`                                       | mem0 / qdrant BM25 keyword search |
| spaCy `en_core_web_sm` (installed into `/opt/venv` via `python -m spacy download`)              | mem0 lemmatization + entity extraction (`mem0ai[nlp]`) |
| tiktoken `cl100k_base` BPE blob at `/opt/memex/models/tiktoken/`                                | memex chunking / token counting |

### Incremental builds

The Dockerfile is split so a **routine source edit rebuilds in ~40 s, not 25 min**.

| Stage | Job                                            | Cache key                | When it re-runs       |
|-------|------------------------------------------------|--------------------------|------------------------|
| A     | install every Python dep (using a stub memex package so source edits don't bust this layer) | `pyproject.toml`, `README.md` | dep changes            |
| B     | pre-warm every model (HF MiniLM, spaCy, ChromaDB ONNX, fastembed, tiktoken) | same as A                | dep changes            |
| C     | `COPY memex/` + `templates/`, `pip install --no-deps .` | `memex/`, `templates/`   | every source edit (~5s) |

BuildKit cache mounts also keep pip's `~/.cache/pip` and apt's `/var/cache/apt`
alive across builds, so even a full rebuild after `pyproject.toml` changes
skips the slow downloads.

## Run

```bash
docker compose up -d
curl -fsS http://localhost:8000/healthz       # liveness
open http://localhost:8000/docs               # OpenAPI / Swagger UI
```

Default exposed port is `8000`. Change with `MEMEX_PORT` in `.env`:

```bash
MEMEX_PORT=18000 docker compose up -d
```

## Configure

The container starts with the `openai` profile if no `memex.yaml` is on the volume; just edit `./data/memex.yaml` to flip to the local profile:

```yaml
embedder:
  provider: chroma-default
  model: all-MiniLM-L6-v2
llm:
  provider: openai
  model: qwen3:4b
  base_url: http://10.242.29.48:11434/v1
  api_key: no-key
```

Or reset from inside:

```bash
docker compose exec memex memex init --profile local --force
```

## Persistence

Everything that needs to survive a restart lives in `./data` on the host:

```
./data/
   docs/                    <-- your markdown wiki (gitignore .cache/)
   memex.yaml               <-- your config
   .cache/
      chroma/               <-- vector index (~MB-GB scale)
      mem0/                 <-- qdrant + history.db
      history/              <-- tombstones, audit log
```

Back up:

```bash
docker compose exec memex memex backup -o /data/snap-$(date +%F).tar.gz
# or, on the host:
tar czf memex-backup.tar.gz -C ./data .
```

Restore (into a fresh container):

```bash
docker compose down
rm -rf ./data && mkdir ./data && tar xzf memex-backup.tar.gz -C ./data
docker compose up -d
```

## Authentication

```bash
# .env
MEMEX_API_TOKEN=$(openssl rand -hex 32)
```

Forces every endpoint except `/healthz` to require `Authorization: Bearer <token>`. The container's healthcheck stays functional because it hits `/healthz`.

## Automated build + E2E test

One script. Builds, model-introspects, boots, runs the full API surface, restarts, verifies persistence:

```bash
bash scripts/docker-build-test.sh                       # full build + tests
FAST=1 bash scripts/docker-build-test.sh                # skip rebuild if memex:e2e exists
```

Output ends with a `PASS / FAIL` count. See the source for the full checklist.

## Common ops

```bash
# logs
docker compose logs -f memex

# shell in
docker compose exec memex bash

# run the local CLI inside the container (acts on the same /data)
docker compose exec memex memex status
docker compose exec memex memex doc ls
docker compose exec memex memex doc reindex --changed

# stop / start / restart
docker compose stop
docker compose start
docker compose restart memex

# nuke (keeps ./data; just kills the container)
docker compose down

# nuke + wipe data (DESTRUCTIVE)
docker compose down -v && rm -rf ./data
```

## Wiring Cursor subagents to a Docker deployment

If you keep your KB in Docker (shared between dev box and laptop, or behind a token on a server), point your local Cursor at it:

```bash
# on your dev box, where Cursor runs:
pipx install <path-to-memex-repo>          # or `pip install -e .` from the repo

# point at the deployment
export MEMEX_API_URL=http://<host>:8000
export MEMEX_API_TOKEN=...                 # if you set one

# verify
memex client status

# then either alias memex to memex client for the agents:
alias memex='memex client'
# ... or add a line at the top of each ~/.cursor/agents/memex-*.md:
#     "All `memex ...` invocations in this agent run through `memex client ...`."
```

## Sidecar Ollama (optional)

`docker-compose.yml` ships with a commented-out Ollama service. Uncomment it and the `memex` service will be able to reach it as `http://ollama:11434/v1` on the compose network — no host exposure needed.

```yaml
# uncomment in docker-compose.yml
ollama:
  image: ollama/ollama:latest
  container_name: ollama
  restart: unless-stopped
  ports:
    - "11434:11434"
  volumes:
    - ./ollama:/root/.ollama
```

And in `./data/memex.yaml`:

```yaml
llm:
  provider: openai
  model: qwen3:4b
  base_url: http://ollama:11434/v1
  api_key: no-key
```

## Troubleshooting

See the table at the bottom of [`../DOCKER.md`](../DOCKER.md). The two most common gotchas:

- **Permission denied on /data** — the in-container user is uid 1000. If your host directory is owned by a different uid, run `chown -R 1000:1000 ./data` once, or build the image with a different uid via `--build-arg`.
- **`OSError: ... offline mode` for a HF model** — the model isn't the one baked into the image. Either extend the Dockerfile's pre-warm step to also snapshot that repo, or override at runtime with `-e HF_HUB_OFFLINE=0 -e TRANSFORMERS_OFFLINE=0` (first request will then go fetch over the network).
