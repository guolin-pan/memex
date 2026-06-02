# syntax=docker/dockerfile:1.7
# -----------------------------------------------------------------------------
# memex — personal assistant + knowledge base.
#
# Two-stage build:
#   1. builder  — installs deps into a venv AND pre-downloads offline models.
#   2. runtime  — copies the venv + models + non-root user, runs uvicorn.
#
# Build arguments:
#   WITH_LOCAL_MODELS=1  (default) — bake offline models into the image so
#                                     the container needs NO network calls at
#                                     start time. Image is ~1.0-1.2 GB.
#                                     Includes:
#                                       - ChromaDB ONNX all-MiniLM-L6-v2 (~165 MB)
#                                       - sentence-transformers/all-MiniLM-L6-v2
#                                         (safetensors only, ~90 MB; PyTorch /
#                                         TF / ONNX / Rust / OpenVINO formats
#                                         are deliberately skipped)
#                                       - CPU-only PyTorch (~180 MB) needed by mem0's HF embedder
#   WITH_LOCAL_MODELS=0             — skip offline models. Image is ~500 MB.
#                                     Suitable when the deployment only uses
#                                     the openai cloud profile.
#
# Runtime contract:
#   - Data lives in /data (mount a host volume here for persistence).
#   - Config lives in /data/memex.yaml.
#   - Server listens on 0.0.0.0:8000.
#   - Set MEMEX_API_TOKEN to require Authorization: Bearer <token>.
# -----------------------------------------------------------------------------

ARG PYTHON_VERSION=3.11-slim-bookworm
ARG WITH_LOCAL_MODELS=1
# CPU-only PyTorch wheel index — ~180 MB instead of ~800 MB for the CUDA one.
ARG TORCH_INDEX_URL=https://download.pytorch.org/whl/cpu

# ============================================================================
# Stage 1 — builder
# ============================================================================
FROM python:${PYTHON_VERSION} AS builder

ARG WITH_LOCAL_MODELS
ARG TORCH_INDEX_URL

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# Build tools for native wheels (chromadb pulls in compiled deps on some archs).
RUN apt-get update \
 && apt-get install -y --no-install-recommends build-essential git curl ca-certificates \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /build

# Layer the install: copy only the project metadata first so requirement changes
# don't bust the cache when source files change.
COPY pyproject.toml README.md ./
COPY memex ./memex
COPY templates ./templates

RUN python -m venv /opt/venv \
 && /opt/venv/bin/pip install --upgrade pip wheel \
 && /opt/venv/bin/pip install .

# -----------------------------------------------------------------------------
# Offline models — controlled by WITH_LOCAL_MODELS.
#
# We bake them into /opt/memex/models/ so the runtime stage can COPY them
# unconditionally. When WITH_LOCAL_MODELS=0 the directory is just empty stubs.
# -----------------------------------------------------------------------------

RUN mkdir -p /opt/memex/models/hf /opt/memex/models/chroma

# Install CPU-only torch + sentence-transformers only when needed.
# Order matters: torch from the CPU wheel index FIRST, then sentence-transformers.
# Pip sees torch is already satisfied and won't pull the ~800 MB CUDA wheel.
RUN if [ "$WITH_LOCAL_MODELS" = "1" ]; then \
      echo ">>> installing CPU-only torch from $TORCH_INDEX_URL ..." && \
      /opt/venv/bin/pip install --index-url "$TORCH_INDEX_URL" "torch>=2.0,<3" && \
      echo ">>> installing sentence-transformers (torch already satisfied) ..." && \
      /opt/venv/bin/pip install "sentence-transformers>=2.7,<6" ; \
    else \
      echo ">>> WITH_LOCAL_MODELS=0, skipping torch + sentence-transformers"; \
    fi

# Pre-download the actual model files.
# 1) ChromaDB ONNX MiniLM goes to ${HOME}/.cache/chroma/onnx_models/all-MiniLM-L6-v2/
#    (hardcoded path; we copy it into our image-baked dir after).
# 2) HuggingFace sentence-transformers/all-MiniLM-L6-v2 goes to $HF_HOME.
ENV HF_HOME=/opt/memex/models/hf
RUN if [ "$WITH_LOCAL_MODELS" = "1" ]; then \
      echo ">>> warming up ChromaDB ONNX MiniLM ..." && \
      /opt/venv/bin/python -c "from chromadb.utils.embedding_functions import DefaultEmbeddingFunction; DefaultEmbeddingFunction()(['warm up'])" && \
      cp -r /root/.cache/chroma /opt/memex/models/chroma_src && \
      mv /opt/memex/models/chroma_src/onnx_models /opt/memex/models/chroma/onnx_models && \
      rm -rf /opt/memex/models/chroma_src && \
      echo ">>> downloading sentence-transformers/all-MiniLM-L6-v2 (safetensors only) ..." && \
      /opt/venv/bin/python -c "from huggingface_hub import snapshot_download; snapshot_download(repo_id='sentence-transformers/all-MiniLM-L6-v2', allow_patterns=['*.json', '*.txt', 'model.safetensors', '1_Pooling/*'])" && \
      echo ">>> baked model sizes:" && du -sh /opt/memex/models/* ; \
    fi

# ============================================================================
# Stage 2 — runtime
# ============================================================================
FROM python:${PYTHON_VERSION} AS runtime

ARG WITH_LOCAL_MODELS

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
    TRANSFORMERS_OFFLINE=1 \
    HF_HUB_OFFLINE=1 \
    HF_DATASETS_OFFLINE=1

# Minimal runtime deps. git for `memex init`'s `git init`; tini for proper PID 1.
RUN apt-get update \
 && apt-get install -y --no-install-recommends git tini ca-certificates curl \
 && rm -rf /var/lib/apt/lists/*

# Non-root user. UID/GID 1000 matches most dev hosts.
RUN groupadd -g 1000 memex \
 && useradd  -u 1000 -g 1000 -m -d /home/memex -s /bin/bash memex

# Bring over the prepared venv + baked models.
COPY --from=builder /opt/venv         /opt/venv
COPY --from=builder /opt/memex/models /opt/memex/models

# ChromaDB hard-codes `Path.home() / ".cache" / "chroma" / "onnx_models"` for
# its ONNX model lookup. Symlink it to the baked image location so no download
# happens at runtime.
RUN mkdir -p /home/memex/.cache /data \
 && if [ -d /opt/memex/models/chroma/onnx_models ]; then \
      ln -sfn /opt/memex/models/chroma /home/memex/.cache/chroma; \
    fi \
 && chown -R memex:memex /data /home/memex /opt/memex/models

# NOTE: TRANSFORMERS_OFFLINE / HF_HUB_OFFLINE / HF_DATASETS_OFFLINE are set
# above so the HuggingFace stack refuses network calls and uses the baked
# /opt/memex/models/hf cache instead. If you built with WITH_LOCAL_MODELS=0
# or want to fetch a *different* HF model at runtime, override them:
#     docker compose run -e HF_HUB_OFFLINE=0 -e TRANSFORMERS_OFFLINE=0 memex …

USER memex
WORKDIR /data

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -fsS http://localhost:8000/healthz || exit 1

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["memex", "serve", "--host", "0.0.0.0", "--port", "8000"]
