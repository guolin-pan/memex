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

### Two build variants

```
+----------------------------+----------+----------------------+--------------------------------+
| variant                    | size     | runtime network?     | when                           |
+----------------------------+----------+----------------------+--------------------------------+
| WITH_LOCAL_MODELS=1        | ~1.1 GB  | none                 | self-hosted, offline, private  |
| (default)                  |          |                      | LLM endpoint, air-gapped       |
+----------------------------+----------+----------------------+--------------------------------+
| WITH_LOCAL_MODELS=0        | ~500 MB  | yes (downloads ONNX  | only the openai cloud profile, |
|                            |          | + HF on first use)   | size-constrained                |
+----------------------------+----------+----------------------+--------------------------------+
```

What gets baked when `WITH_LOCAL_MODELS=1`:

| Bundle | Size | Used by |
|---|---|---|
| CPU-only PyTorch (from PyTorch's CPU wheel index) | ~180 MB | sentence-transformers |
| sentence-transformers package | ~10 MB | mem0's HuggingFace embedder |
| ChromaDB ONNX `all-MiniLM-L6-v2` | ~165 MB | wiki vector layer when `embedder.provider: chroma-default` |
| HF `sentence-transformers/all-MiniLM-L6-v2` (safetensors only, redundant PyTorch/TF/Rust/ONNX/OpenVINO formats skipped) | ~90 MB | mem0's HF embedder |

To build the slim variant:

```bash
WITH_LOCAL_MODELS=0 docker compose build
# or, with plain docker:
docker build --build-arg WITH_LOCAL_MODELS=0 -t memex:slim .
```

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
FAST=1 bash scripts/docker-build-test.sh               # skip rebuild if memex:e2e exists
WITH_LOCAL_MODELS=0 bash scripts/docker-build-test.sh   # build slim variant
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
- **`OSError: ... offline mode` for a HF model** — you built with `WITH_LOCAL_MODELS=0`. Either rebuild with `=1`, or override at runtime with `-e HF_HUB_OFFLINE=0 -e TRANSFORMERS_OFFLINE=0` (first request will then go fetch over the network).
