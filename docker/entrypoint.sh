#!/usr/bin/env bash
# memex container entrypoint.
#
# Responsibilities (small on purpose):
#   1. If $MEMEX_ROOT/memex.yaml is absent, drop a sensible "local profile"
#      default in place so a fresh `docker compose up` works without any
#      manual `memex init` step. Values come from env vars so operators can
#      override per deployment without baking a new image; the defaults
#      themselves match the project's "offline embedder + Ollama-compatible
#      LLM" reference setup.
#   2. exec the supplied command (typically `memex serve ...` from CMD).
#
# The script never overwrites an existing memex.yaml — once written, the
# user's edits win. Re-create or wipe ./data/memex.yaml on the host if you
# want the defaults regenerated.

set -euo pipefail

ROOT="${MEMEX_ROOT:-/data}"
CFG="$ROOT/memex.yaml"

# Defaults are the values we hand-picked for the project (local Ollama at
# 10.242.29.48 + the offline ONNX MiniLM that's already baked into the image).
# Every value is env-overridable so an operator can deploy without rebuilding.
DEFAULT_USER_ID="${MEMEX_DEFAULT_USER_ID:-guolin}"
DEFAULT_LLM_PROVIDER="${MEMEX_DEFAULT_LLM_PROVIDER:-openai}"
DEFAULT_LLM_MODEL="${MEMEX_DEFAULT_LLM_MODEL:-qwen3:4b}"
DEFAULT_LLM_TEMPERATURE="${MEMEX_DEFAULT_LLM_TEMPERATURE:-0.1}"
DEFAULT_LLM_BASE_URL="${MEMEX_DEFAULT_LLM_BASE_URL:-http://10.242.29.48:11434/v1}"
DEFAULT_LLM_API_KEY="${MEMEX_DEFAULT_LLM_API_KEY:-no-key}"
DEFAULT_EMBEDDER_PROVIDER="${MEMEX_DEFAULT_EMBEDDER_PROVIDER:-chroma-default}"
DEFAULT_EMBEDDER_MODEL="${MEMEX_DEFAULT_EMBEDDER_MODEL:-all-MiniLM-L6-v2}"

mkdir -p "$ROOT"

if [[ ! -e "$CFG" ]]; then
  cat > "$CFG" <<YAML
user_id: ${DEFAULT_USER_ID}

embedder:
  provider: ${DEFAULT_EMBEDDER_PROVIDER}
  model: ${DEFAULT_EMBEDDER_MODEL}

llm:
  provider: ${DEFAULT_LLM_PROVIDER}
  model: ${DEFAULT_LLM_MODEL}
  temperature: ${DEFAULT_LLM_TEMPERATURE}
  base_url: ${DEFAULT_LLM_BASE_URL}
  api_key: ${DEFAULT_LLM_API_KEY}
YAML
  echo "memex-entrypoint: wrote default config -> $CFG" >&2
  echo "memex-entrypoint:   user_id=${DEFAULT_USER_ID} embedder=${DEFAULT_EMBEDDER_PROVIDER}:${DEFAULT_EMBEDDER_MODEL}" >&2
  echo "memex-entrypoint:   llm=${DEFAULT_LLM_PROVIDER}:${DEFAULT_LLM_MODEL} @ ${DEFAULT_LLM_BASE_URL}" >&2
else
  echo "memex-entrypoint: $CFG already exists, leaving it alone." >&2
fi

exec "$@"
