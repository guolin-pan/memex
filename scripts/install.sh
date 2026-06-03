#!/usr/bin/env bash
# scripts/install.sh
# ----------------------------------------------------------------------------
# Install memex from this repo. Picks `uv` if it's on PATH, falls back to pip.
# Safe to run repeatedly: re-running is a no-op when the venv is already wired
# and memex --version works.
#
# Usage:
#   bash scripts/install.sh                          # default: uv > pip, dev install into .venv/
#   bash scripts/install.sh --pip                    # force pip even when uv is available
#   bash scripts/install.sh --uv                     # force uv (errors if missing)
#   bash scripts/install.sh --tool                   # `uv tool install .` (isolated CLI, like pipx)
#   bash scripts/install.sh --system                 # install into the active Python env (no .venv/)
#   bash scripts/install.sh --extras dev,local       # add extras (comma-separated)
#   bash scripts/install.sh --venv /path/to/.venv    # override venv location
#   bash scripts/install.sh --python 3.11            # force a Python version (uv only)
#   bash scripts/install.sh --quiet                  # only output pass/fail
#
# Exit codes:
#   0 = installed and verified  (or already installed, no work needed)
#   1 = install failed
#   2 = bad arguments / missing prerequisites
#
# Designed so an LLM agent can shell-call it without surprises:
#   - idempotent
#   - never prompts
#   - always prints the final `memex --version` line
# ----------------------------------------------------------------------------

set -euo pipefail

# ----- defaults -------------------------------------------------------------
MODE=auto         # auto | pip | uv | tool | system
EXTRAS="dev"
VENV_DIR=".venv"
PYTHON_VERSION=""
QUIET=0

# ----- arg parse ------------------------------------------------------------
while [[ $# -gt 0 ]]; do
  case "$1" in
    --pip)         MODE=pip;     shift ;;
    --uv)          MODE=uv;      shift ;;
    --tool)        MODE=tool;    shift ;;
    --system)      MODE=system;  shift ;;
    --extras)      EXTRAS="$2";  shift 2 ;;
    --venv)        VENV_DIR="$2"; shift 2 ;;
    --python)      PYTHON_VERSION="$2"; shift 2 ;;
    --quiet|-q)    QUIET=1;      shift ;;
    -h|--help)
      # Print the leading comment block (everything up to the first non-# line).
      awk 'NR>1 && /^[^#]/ { exit } NR>1 { sub(/^# ?/, ""); print }' "$0"
      exit 0
      ;;
    *)             echo "error: unknown arg: $1" >&2; exit 2 ;;
  esac
done

# ----- colour helpers -------------------------------------------------------
if [[ -t 1 ]]; then
  G=$'\e[32m'; R=$'\e[31m'; Y=$'\e[33m'; B=$'\e[34m'; D=$'\e[2m'; X=$'\e[0m'
else
  G=""; R=""; Y=""; B=""; D=""; X=""
fi
log()  { [[ $QUIET -eq 1 ]] || echo "${B}==${X} $*"; }
note() { [[ $QUIET -eq 1 ]] || echo "  ${D}$*${X}"; }
ok()   { echo "${G}OK${X}   $*"; }
warn() { echo "${Y}warn${X} $*" >&2; }
err()  { echo "${R}error${X} $*" >&2; }

# ----- locate project root --------------------------------------------------
SCRIPT_DIR=$(cd "$(dirname "$0")" && pwd)
PROJECT_ROOT=$(cd "$SCRIPT_DIR/.." && pwd)
cd "$PROJECT_ROOT"

if [[ ! -f pyproject.toml ]] || ! grep -q '^name = "memex"' pyproject.toml; then
  err "$PROJECT_ROOT does not look like the memex repo (no pyproject.toml with name=\"memex\")"
  exit 2
fi

# ----- pick installer -------------------------------------------------------
HAVE_UV=0
command -v uv >/dev/null 2>&1 && HAVE_UV=1

case "$MODE" in
  auto)
    if [[ $HAVE_UV -eq 1 ]]; then
      INSTALLER=uv
    elif command -v python3 >/dev/null 2>&1; then
      INSTALLER=pip
    else
      err "neither uv nor python3 found on PATH"
      exit 2
    fi
    ;;
  uv|tool)
    if [[ $HAVE_UV -eq 0 ]]; then
      err "--${MODE} requested but uv is not on PATH. Install it first: https://docs.astral.sh/uv/"
      exit 2
    fi
    INSTALLER=$MODE
    ;;
  pip|system)
    INSTALLER=$MODE
    ;;
esac

# Resolve VENV_DIR to an absolute path so we don't accidentally
# concatenate PROJECT_ROOT to an already-absolute --venv argument.
case "$VENV_DIR" in
  /*) VENV_ABS="$VENV_DIR" ;;          # already absolute
  *)  VENV_ABS="$PROJECT_ROOT/$VENV_DIR" ;;
esac

log "memex install: mode=${MODE} (using ${INSTALLER}), extras=${EXTRAS:-<none>}, venv=${VENV_ABS}"

# ----- normalise extras into "[a,b,c]" form for pip / uv pip ----------------
EXTRAS_SUFFIX=""
if [[ -n "$EXTRAS" ]]; then
  EXTRAS_SUFFIX=".[${EXTRAS}]"
else
  EXTRAS_SUFFIX="."
fi

# ----- idempotency check ----------------------------------------------------
# If memex is already importable AND points at this repo's source, we're done.
already_installed() {
  local py="$1"
  "$py" - <<'PY' 2>/dev/null
import importlib, sys, json
try:
    import memex
    from memex import __version__
except Exception:
    sys.exit(1)
import os
path = os.path.dirname(memex.__file__)
project_root = os.environ.get("PROJECT_ROOT", "")
# If the import resolved inside this repo's source (editable install) or via a
# regular install of the same version, treat as already installed.
print(json.dumps({"version": __version__, "path": path}))
PY
}

verify() {
  local bin="$1"
  if ! "$bin" --version >/dev/null 2>&1; then
    err "memex installed but '$bin --version' fails"
    exit 1
  fi
  local v
  v=$("$bin" --version 2>&1)
  ok "$v  (binary: $bin)"
}

# PyTorch 2.12+ imports torch._dynamo, whose stdlib polyfills expect
# sys.get_int_max_str_digits (added in CPython 3.11.0 final). Pre-release
# 3.11.0rc1 and similar interpreters crash sentence-transformers at import.
require_stdlib_for_torch_stack() {
  local py="$1"
  if ! "$py" -c "import sys; sys.exit(0 if getattr(sys, 'get_int_max_str_digits', None) else 1)" 2>/dev/null; then
    err "Python at $py cannot run the default torch/transformers stack (missing sys.get_int_max_str_digits)."
    echo "  Typical cause: a pre-release CPython (e.g. 3.11.0rc1). Install a final release, then recreate .venv." >&2
    echo "  Example (uv-managed CPython):" >&2
    echo "    uv python install 3.11" >&2
    echo "    rm -rf \"$VENV_ABS\"" >&2
    echo "    bash \"$PROJECT_ROOT/scripts/install.sh\" --uv --python 3.11" >&2
    exit 1
  fi
}

# ----- install --------------------------------------------------------------
case "$INSTALLER" in
  uv)
    if [[ -d "$VENV_ABS" && -x "$VENV_ABS/bin/python" ]]; then
      note "venv already exists, reusing: $VENV_ABS"
    else
      log "creating venv via uv"
      if [[ -n "$PYTHON_VERSION" ]]; then
        uv venv --python "$PYTHON_VERSION" "$VENV_ABS" >/dev/null
      else
        # Default to CPython 3.11 so we avoid broken pre-release system Pythons
        # and match the project's recommended version.
        uv venv --python 3.11 "$VENV_ABS" >/dev/null
      fi
    fi
    require_stdlib_for_torch_stack "$VENV_ABS/bin/python"
    log "installing editable + extras: $EXTRAS_SUFFIX"
    VIRTUAL_ENV="$VENV_ABS" uv pip install -e "$EXTRAS_SUFFIX" ${QUIET:+--quiet}
    BIN="$VENV_ABS/bin/memex"
    verify "$BIN"
    note "activate the venv with:  source ${VENV_ABS}/bin/activate"
    ;;

  pip)
    # Pick the newest available Python >= 3.11 (3.10 on many Debian/Ubuntu
    # hosts lacks ensurepip in the venv module). Honour --python if given.
    pick_python() {
      if [[ -n "${PYTHON_VERSION}" ]]; then
        local cand="python${PYTHON_VERSION}"
        command -v "$cand" >/dev/null 2>&1 && { echo "$cand"; return; }
        err "requested --python ${PYTHON_VERSION} but $cand not on PATH"; exit 2
      fi
      for cand in python3.13 python3.12 python3.11 python3; do
        if command -v "$cand" >/dev/null 2>&1; then
          if "$cand" -c "import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)" \
              && "$cand" -c "import ensurepip" 2>/dev/null \
              && "$cand" -c "import sys; sys.exit(0 if getattr(sys, 'get_int_max_str_digits', None) else 1)" 2>/dev/null; then
            echo "$cand"; return
          fi
        fi
      done
      err "no working python>=3.10 found on PATH with ensurepip and torch-compatible stdlib (need sys.get_int_max_str_digits — avoid CPython pre-releases like 3.11.0rc1)"
      exit 2
    }
    PY=$(pick_python)
    note "using interpreter: $PY ($($PY --version 2>&1))"
    log "creating venv via $PY -m venv"
    if [[ ! -d "$VENV_ABS" ]]; then
      "$PY" -m venv "$VENV_ABS"
    fi
    require_stdlib_for_torch_stack "$VENV_ABS/bin/python"
    note "upgrading pip + wheel"
    "$VENV_ABS/bin/pip" install --upgrade pip wheel ${QUIET:+--quiet} >/dev/null
    log "installing editable + extras: $EXTRAS_SUFFIX"
    "$VENV_ABS/bin/pip" install -e "$EXTRAS_SUFFIX" ${QUIET:+--quiet}
    BIN="$VENV_ABS/bin/memex"
    verify "$BIN"
    note "activate the venv with:  source ${VENV_ABS}/bin/activate"
    ;;

  tool)
    # Isolated CLI install via `uv tool install` (like pipx). Best for end
    # users who only want `memex` on their PATH and don't care about a venv.
    log "installing as a uv tool (isolated env)"
    uv tool install --reinstall "$EXTRAS_SUFFIX" ${QUIET:+--quiet}
    # uv puts tool binaries under "$HOME/.local/bin/memex" by default.
    BIN=$(command -v memex || echo "$HOME/.local/bin/memex")
    if [[ ! -x "$BIN" ]]; then
      err "uv tool install succeeded but 'memex' is not on PATH. Try:"
      echo "       uv tool update-shell" >&2
      exit 1
    fi
    verify "$BIN"
    ;;

  system)
    # Install into the currently active Python environment. No venv created.
    log "installing into the active Python env"
    PY=${PYTHON:-python3}
    "$PY" -m pip install --upgrade pip wheel ${QUIET:+--quiet} >/dev/null
    "$PY" -m pip install -e "$EXTRAS_SUFFIX" ${QUIET:+--quiet}
    BIN=$(command -v memex || true)
    if [[ -z "$BIN" ]]; then
      err "memex installed but not found on PATH (try restarting your shell)"
      exit 1
    fi
    verify "$BIN"
    ;;
esac

# ----- next steps -----------------------------------------------------------
if [[ $QUIET -eq 0 ]]; then
  cat <<EOF

${B}next steps${X}
  Note: if \`uv tool install memex\` is also on your PATH, bare \`memex\` may hit ~/.local/bin
  instead of this venv. Use \`source ${VENV_ABS}/bin/activate\` or \`uv run memex\` from the repo.
  1. (optional) activate the venv:
       source ${VENV_ABS}/bin/activate
  2. initialise a memex root (defaults to ~/memex):
       memex init                       # OpenAI cloud profile (needs OPENAI_API_KEY)
       memex init --profile local       # offline embedder + OpenAI-compat LLM endpoint
  3. add a doc and search:
       echo "# hello" | memex doc add - --title hello --tags inbox
       memex doc search "hello"
  4. (optional) wire into Cursor:
       memex cursor install-hooks
       memex cursor install-rule .
       memex cursor install-agents --scope user

Docs: ${PROJECT_ROOT}/docs/ (quickstart.md, cli.md, api.md, docker.md, cursor.md, config.md, architecture.md)
EOF
fi
