# syntax=docker/dockerfile:1.7
# -----------------------------------------------------------------------------
# memex — fully self-contained image.
#
# Everything memex / mem0 / ChromaDB / sentence-transformers / fastembed /
# spaCy / tiktoken may touch at runtime is installed AND pre-warmed at build
# time. The container makes ZERO network calls on start. Image size is the
# explicit, accepted cost — we trade ~1-2 GB on disk for guaranteed offline
# operation.
#
# Layer-cache strategy:
#   A. Install Python deps using a *stub* `memex/__init__.py` so pip's
#      dependency resolution doesn't see (and thus doesn't rebuild from)
#      the actual source tree. Cache key: pyproject.toml + README.md.
#   B. Pre-warm every model. Cache key: same as A.
#   C. COPY the real `memex/` and `templates/`, then `pip install --no-deps`
#      to drop the project into the already-built venv.
#
# Routine source edits invalidate ONLY stage C (a few seconds). torch / HF
# MiniLM / spaCy / fastembed / tiktoken stay cached. pyproject.toml changes
# bust everything from A down — that's the right trade.
#
# BuildKit cache mounts (--mount=type=cache,target=/root/.cache/pip etc.)
# keep pip/apt downloads alive across builds even when the layer rebuilds,
# so cold caches recover quickly too.
#
# Runtime contract:
#   - Data lives in /data (mount a host volume here for persistence).
#   - Config lives in /data/memex.yaml.
#   - Server listens on 0.0.0.0:7963.
#   - Set MEMEX_API_TOKEN to require Authorization: Bearer <token>.
# -----------------------------------------------------------------------------

ARG PYTHON_VERSION=3.11-slim-bookworm
# CPU-only PyTorch wheel index — we don't ship CUDA here, the GPU wheel would
# add ~600 MB for nothing on a typical server.
ARG TORCH_INDEX_URL=https://download.pytorch.org/whl/cpu

# ============================================================================
# Stage 1 — builder
# ============================================================================
FROM python:${PYTHON_VERSION} AS builder

ARG TORCH_INDEX_URL

# Proxy build args. When passed via --build-arg HTTP_PROXY=... BuildKit
# automatically exports these to the env of every RUN command (special-cased
# for the HTTP_PROXY family), so apt / pip / curl pick them up without
# further wiring. Leave empty if your host has direct internet access.
ARG HTTP_PROXY=
ARG HTTPS_PROXY=
ARG NO_PROXY=localhost,127.0.0.1
ARG http_proxy=
ARG https_proxy=
ARG no_proxy=localhost,127.0.0.1

# Bake the same cache locations that the runtime will use so every model
# pre-download lands inside /opt/memex/models and can be COPY'd unchanged.
# PIP_NO_CACHE_DIR is deliberately *not* set — we want pip to use its cache,
# which lives on a BuildKit cache mount (see the pip RUN below) and survives
# across builds.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    HF_HOME=/opt/memex/models/hf \
    FASTEMBED_CACHE_PATH=/opt/memex/models/fastembed \
    TIKTOKEN_CACHE_DIR=/opt/memex/models/tiktoken

# Build tools for native wheels (chromadb pulls in compiled deps on some archs).
# apt caches go on BuildKit cache mounts so we don't re-download every time.
RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt,sharing=locked \
    rm -f /etc/apt/apt.conf.d/docker-clean \
 && apt-get update \
 && apt-get install -y --no-install-recommends build-essential git curl ca-certificates

WORKDIR /build

# -----------------------------------------------------------------------------
# Stage A — Python dependencies (cache key: pyproject.toml + README.md)
#
# `pip install .` needs *some* `memex/__init__.py` to exist or setuptools
# bails. We write a stub, resolve every transitive dep, then uninstall the
# stub. The real package goes in via stage C. Touching memex/*.py in normal
# dev DOES NOT bust this layer.
# -----------------------------------------------------------------------------
COPY pyproject.toml README.md ./

RUN --mount=type=cache,target=/root/.cache/pip \
    mkdir -p memex templates \
 && printf '__version__ = "0.0.0"\n' > memex/__init__.py \
 && touch templates/.keep \
 && python -m venv /opt/venv \
 && /opt/venv/bin/pip install --upgrade pip wheel \
 # CPU-only torch first so sentence-transformers stays on the CPU wheel.
 # Without this, `pip install .` would pull the 800 MB CUDA wheel from PyPI.
 && /opt/venv/bin/pip install --index-url "$TORCH_INDEX_URL" "torch>=2.0,<3" \
 # Now resolve all of memex's transitive deps using the stub package.
 && /opt/venv/bin/pip install . \
 # Drop the stub from site-packages so stage C does a clean install of the
 # real source. Dependencies stay installed because pip uninstall only
 # removes the named distribution, not its requirements.
 && /opt/venv/bin/pip uninstall -y memex \
 && rm -rf memex templates

# -----------------------------------------------------------------------------
# Stage B — Pre-warm every model (cache key: same as Stage A)
#
# Each model is its own RUN so a transient failure can be retried without
# re-running the others. After this stage, /opt/memex/models/ is
# self-sufficient and the runtime makes zero outbound calls.
# -----------------------------------------------------------------------------
RUN mkdir -p /opt/memex/models/hf \
             /opt/memex/models/chroma \
             /opt/memex/models/fastembed \
             /opt/memex/models/tiktoken

# spaCy en_core_web_sm — mem0/utils/spacy_models.py loads this. Installed
# as a Python package into the venv, so it travels with /opt/venv.
RUN /opt/venv/bin/python -m spacy download en_core_web_sm \
 && /opt/venv/bin/python -c "import spacy; spacy.load('en_core_web_sm'); print('spaCy en_core_web_sm OK')"

# ChromaDB ONNX all-MiniLM-L6-v2 — ChromaDB hardcodes
# Path.home() / .cache / chroma / onnx_models / ... for lookup; we warm it
# under /root and shift the directory into /opt/memex/models/chroma so the
# runtime stage can symlink it at a stable location.
RUN /opt/venv/bin/python -c "from chromadb.utils.embedding_functions import DefaultEmbeddingFunction; DefaultEmbeddingFunction()(['warm up']); print('ChromaDB ONNX MiniLM OK')" \
 && cp -r /root/.cache/chroma /opt/memex/models/chroma_src \
 && mv /opt/memex/models/chroma_src/onnx_models /opt/memex/models/chroma/onnx_models \
 && rm -rf /opt/memex/models/chroma_src

# HuggingFace sentence-transformers/all-MiniLM-L6-v2 — full snapshot.
RUN /opt/venv/bin/python -c "from huggingface_hub import snapshot_download; snapshot_download(repo_id='sentence-transformers/all-MiniLM-L6-v2'); print('HF MiniLM full snapshot OK')"

# fastembed Qdrant/bm25 — mem0/vector_stores/qdrant.py lazy-loads this.
RUN /opt/venv/bin/python -c "from fastembed import SparseTextEmbedding; m = SparseTextEmbedding(model_name='Qdrant/bm25'); list(m.embed(['warm up'])); print('fastembed Qdrant/bm25 OK')"

# tiktoken cl100k_base — memex/core/utils.py token counting.
RUN /opt/venv/bin/python -c "import tiktoken; tiktoken.get_encoding('cl100k_base').encode('warm up'); print('tiktoken cl100k_base OK')"

RUN echo ">>> baked model sizes:" && du -sh /opt/memex/models/*

# -----------------------------------------------------------------------------
# Stage C — install the actual project (cache key: memex/ + templates/)
#
# This is the ONLY layer that re-runs for a routine source change. The
# package metadata stayed the same, so deps are unchanged; we just drop
# our code into site-packages via `--no-deps` and we're done.
# -----------------------------------------------------------------------------
COPY memex ./memex
COPY templates ./templates

RUN --mount=type=cache,target=/root/.cache/pip \
    /opt/venv/bin/pip install --no-deps .

# ============================================================================
# Stage 2 — runtime
# ============================================================================
FROM python:${PYTHON_VERSION} AS runtime

# Proxy ARGs — only used by this stage's apt-get below. NOT set as ENV in the
# final image so the running container doesn't accidentally route LLM /
# embedder traffic through the build-time proxy.
ARG HTTP_PROXY=
ARG HTTPS_PROXY=
ARG NO_PROXY=localhost,127.0.0.1
ARG http_proxy=
ARG https_proxy=
ARG no_proxy=localhost,127.0.0.1

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH=/opt/venv/bin:$PATH \
    MEMEX_ROOT=/data \
    # HF cache layout: $HF_HOME/hub/models--<org>--<repo>/...
    # sentence-transformers v2.5+ defaults to $HF_HOME/hub for lookups, so
    # setting HF_HOME alone is enough. We deliberately don't set
    # SENTENCE_TRANSFORMERS_HOME because that would point ST at $ST_HOME/
    # (no /hub/) and miss the baked files.
    HF_HOME=/opt/memex/models/hf \
    # fastembed reads this; without it fastembed caches under a system temp
    # dir, which (a) is wiped on container restart and (b) wouldn't contain
    # the baked model files we pulled in stage 1.
    FASTEMBED_CACHE_PATH=/opt/memex/models/fastembed \
    # tiktoken reads this to locate cached BPE blobs.
    TIKTOKEN_CACHE_DIR=/opt/memex/models/tiktoken \
    # All three "offline" toggles below tell the HuggingFace stack to never
    # phone home. The baked /opt/memex/models/hf/ is the source of truth.
    TRANSFORMERS_OFFLINE=1 \
    HF_HUB_OFFLINE=1 \
    HF_DATASETS_OFFLINE=1 \
    # mem0 defaults its internal dir to ~/.mem0. Inside the container that's
    # /home/memex/.mem0 which is owned by uid 1000; if the container is run
    # with --user $(id -u):$(id -g) and the host uid isn't 1000, mem0 can't
    # write there. Point MEM0_DIR at a sub-path of the persistent volume so
    # it always lands on a writable filesystem regardless of uid.
    MEM0_DIR=/data/.cache/mem0_home \
    # torch._inductor calls getpass.getuser() which calls pwd.getpwuid()
    # which KeyErrors when the container runs as a uid that has no /etc/passwd
    # entry (the common case when --user $(id -u) doesn't match the in-image
    # memex user's uid 1000). getpass falls back to env LOGNAME / USER before
    # hitting pwd, so setting USER=memex is enough to keep torch importable.
    # Also pin TORCHINDUCTOR_CACHE_DIR to a writable path independent of HOME.
    USER=memex \
    LOGNAME=memex \
    TORCHINDUCTOR_CACHE_DIR=/tmp/torch-inductor

# Minimal runtime deps. git for `memex init`'s `git init`; tini for proper PID 1.
RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt,sharing=locked \
    rm -f /etc/apt/apt.conf.d/docker-clean \
 && apt-get update \
 && apt-get install -y --no-install-recommends git tini ca-certificates curl

# Non-root user. UID/GID 1000 matches most dev hosts.
RUN groupadd -g 1000 memex \
 && useradd  -u 1000 -g 1000 -m -d /home/memex -s /bin/bash memex

# Bring over the prepared venv + baked models.
COPY --from=builder /opt/venv         /opt/venv
COPY --from=builder /opt/memex/models /opt/memex/models

# First-start entrypoint: drops a default $MEMEX_ROOT/memex.yaml when the
# bind-mounted volume is empty so the container is usable out of the box
# without a separate `memex init` step. See docker/entrypoint.sh for the
# env vars that customise the written values.
COPY docker/entrypoint.sh /usr/local/bin/memex-entrypoint
RUN chmod +x /usr/local/bin/memex-entrypoint

# ChromaDB hard-codes `Path.home() / ".cache" / "chroma" / "onnx_models"` for
# its ONNX model lookup. Symlink it to the baked image location so no download
# happens at runtime regardless of which uid the container actually runs as.
RUN mkdir -p /home/memex/.cache /data \
 && ln -sfn /opt/memex/models/chroma /home/memex/.cache/chroma \
 && chown -R memex:memex /data /home/memex /opt/memex/models

# NOTE: TRANSFORMERS_OFFLINE / HF_HUB_OFFLINE / HF_DATASETS_OFFLINE are set
# above so the HuggingFace stack refuses network calls and uses the baked
# /opt/memex/models/hf cache instead. If you ever want to fetch a *different*
# HF model at runtime, override them:
#     docker compose run -e HF_HUB_OFFLINE=0 -e TRANSFORMERS_OFFLINE=0 memex …

USER memex
WORKDIR /data

EXPOSE 7963

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -fsS http://localhost:7963/healthz || exit 1

# tini is PID 1; memex-entrypoint drops a default memex.yaml if absent and
# then exec's CMD.
ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/memex-entrypoint"]
CMD ["memex", "serve", "--host", "0.0.0.0", "--port", "7963"]
