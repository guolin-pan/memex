#!/usr/bin/env bash
# scripts/docker-build-test.sh
# ----------------------------------------------------------------------------
# Build the memex Docker image with offline models baked in, then run an
# end-to-end test battery against the running container.
#
# Pre-reqs:
#   - Docker daemon reachable from this shell (you are in the `docker` group
#     OR you run this script under sudo).
#   - Network access during BUILD ONLY (to fetch base image, wheels, models).
#   - `curl` and `jq` on the host.
#
# Usage:
#   bash scripts/docker-build-test.sh                # full build + full tests
#   FAST=1     bash scripts/docker-build-test.sh    # skip build if image exists
#   SKIP_MEM=1 bash scripts/docker-build-test.sh    # skip slow LLM tests
#   IMAGE=memex:dev TAG=test bash scripts/docker-build-test.sh
#
# What it verifies:
#   1. Image builds end-to-end with WITH_LOCAL_MODELS=1.
#   2. Baked models exist on disk inside the image
#      (/opt/memex/models/chroma/ and /opt/memex/models/hf/).
#   3. HF_HUB_OFFLINE=1 / TRANSFORMERS_OFFLINE=1 are set.
#   4. Container becomes healthy on /healthz.
#   5. Full API surface works: /, /status, /doc/*, /mem/*, /ctx.
#   6. The memex persists across container restarts (volume binding works).
#   7. Bearer-token auth gating works.
#   8. (Optional) `memex client` from inside the container talks to the API.
#
# Exit code 0 on success, non-zero on any failure.
# ----------------------------------------------------------------------------

set -u

IMAGE="${IMAGE:-memex}"
TAG="${TAG:-e2e}"
FULL_IMAGE="${IMAGE}:${TAG}"
CONTAINER="${CONTAINER:-memex-e2e}"
HOST_PORT="${HOST_PORT:-18000}"
DATA_DIR="${DATA_DIR:-/tmp/memex-docker-data}"
TOKEN="${MEMEX_API_TOKEN:-e2e-token-$(date +%s)}"
TIMEOUT_BOOT="${TIMEOUT_BOOT:-60}"
WITH_LOCAL_MODELS="${WITH_LOCAL_MODELS:-1}"

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

# -- colours / counters ------------------------------------------------------
G=$'\e[32m'; R=$'\e[31m'; Y=$'\e[33m'; B=$'\e[34m'; D=$'\e[2m'; X=$'\e[0m'
PASS=0; FAIL=0
FAILED=()

step()   { echo; echo "${B}== $* ==${X}"; }
note()   { echo "  ${D}$*${X}"; }
pass()   { echo "  ${G}PASS${X} $1"; PASS=$((PASS+1)); }
fail()   { echo "  ${R}FAIL${X} $1"; FAIL=$((FAIL+1)); FAILED+=("$1"); }

# expect_eq: <desc> <actual> <expected>
expect_eq() {
  if [[ "$2" == "$3" ]]; then pass "$1"; else fail "$1 (got=$2, want=$3)"; fi
}

# expect_contains: <desc> <stdout> <regex>
expect_contains() {
  if echo "$2" | grep -qE "$3"; then pass "$1"
  else fail "$1 (output didn't match /$3/)"; echo "${D}---out---"; echo "$2" | head -10 | sed 's/^/  /'; echo "---------${X}"
  fi
}

# expect_curl: <desc> <expected-http-code> <method> <url> [extra-curl-args...]
expect_curl() {
  local desc="$1" want="$2" method="$3" url="$4"; shift 4
  local out code
  out=$(curl -fsS -o /tmp/memex-curl-body -w "%{http_code}" -X "$method" "$url" "$@" 2>&1) || true
  code="$out"
  if [[ "$code" != "$want" ]]; then
    fail "$desc (got HTTP $code, want $want)"
    echo "${D}---body---"; head -10 /tmp/memex-curl-body 2>/dev/null | sed 's/^/  /'; echo "----------${X}"
  else
    pass "$desc"
  fi
}

cleanup() {
  echo
  step "cleanup"
  docker rm -f "$CONTAINER" >/dev/null 2>&1 || true
  rm -rf "$DATA_DIR" || true
}
trap cleanup EXIT

echo "${B}######################################################"
echo "  memex Docker build + E2E test"
echo "  image          : $FULL_IMAGE"
echo "  container      : $CONTAINER"
echo "  host port      : $HOST_PORT"
echo "  data dir       : $DATA_DIR"
echo "  WITH_LOCAL_MODELS: $WITH_LOCAL_MODELS"
echo "######################################################${X}"

# --- 0. preconditions -------------------------------------------------------
step "0. preconditions"
docker info >/dev/null 2>&1 || { echo "${R}fatal: docker daemon not reachable. add yourself to the docker group or run with sudo.${X}"; exit 2; }
pass "docker daemon reachable"
command -v curl >/dev/null || { echo "${R}fatal: curl required${X}"; exit 2; }
command -v jq   >/dev/null || { echo "${Y}warn: jq not installed; some checks will fall back to grep${X}"; HAS_JQ=0; } 
HAS_JQ="${HAS_JQ:-1}"

# --- 1. build ---------------------------------------------------------------
step "1. build image  (~5-10 min on first run; downloads models)"
if [[ "${FAST:-0}" == "1" ]] && docker image inspect "$FULL_IMAGE" >/dev/null 2>&1; then
  note "FAST=1 and image exists — skipping rebuild"
  pass "image already present"
else
  if docker build \
       --build-arg "WITH_LOCAL_MODELS=$WITH_LOCAL_MODELS" \
       --progress=plain \
       -t "$FULL_IMAGE" \
       . 2>&1 | tail -200; then
    pass "image built"
  else
    fail "docker build"
    exit 1
  fi
fi

# --- 2. image introspection -------------------------------------------------
step "2. baked-in model verification"
SIZE=$(docker image inspect "$FULL_IMAGE" --format '{{.Size}}')
SIZE_MB=$((SIZE / 1024 / 1024))
note "image size: ${SIZE_MB} MB"

inspect_env() { docker image inspect "$FULL_IMAGE" --format '{{range .Config.Env}}{{println .}}{{end}}'; }
ENV_DUMP=$(inspect_env)

expect_contains "HF_HOME baked"          "$ENV_DUMP" "HF_HOME=/opt/memex/models/hf"
expect_contains "HF_HUB_OFFLINE=1"        "$ENV_DUMP" "HF_HUB_OFFLINE=1"
expect_contains "TRANSFORMERS_OFFLINE=1"  "$ENV_DUMP" "TRANSFORMERS_OFFLINE=1"
expect_contains "MEMEX_ROOT=/data"        "$ENV_DUMP" "MEMEX_ROOT=/data"

if [[ "$WITH_LOCAL_MODELS" == "1" ]]; then
  # File existence inside the image (run a one-shot container with /bin/ls).
  CHROMA_LISTING=$(docker run --rm --entrypoint /bin/ls "$FULL_IMAGE" /opt/memex/models/chroma/onnx_models/all-MiniLM-L6-v2 2>&1)
  expect_contains "ChromaDB ONNX baked"   "$CHROMA_LISTING" "onnx"
  HF_LISTING=$(docker run --rm --entrypoint /bin/ls "$FULL_IMAGE" /opt/memex/models/hf/hub 2>&1)
  expect_contains "HF MiniLM baked"        "$HF_LISTING" "models--sentence-transformers--all-MiniLM-L6-v2"
  # symlink for chroma cache wired up
  SYMLINK=$(docker run --rm --entrypoint /bin/sh "$FULL_IMAGE" -c 'readlink -f /home/memex/.cache/chroma' 2>&1)
  expect_contains "chroma cache symlinked" "$SYMLINK" "/opt/memex/models/chroma"
fi

# --- 3. boot the container --------------------------------------------------
step "3. boot container"
docker rm -f "$CONTAINER" >/dev/null 2>&1 || true
rm -rf "$DATA_DIR" && mkdir -p "$DATA_DIR"
# Match in-container uid (1000:1000)
chmod 0777 "$DATA_DIR"  # works whether or not uid 1000 maps to host user

CID=$(docker run -d \
  --name "$CONTAINER" \
  -p "${HOST_PORT}:8000" \
  -v "${DATA_DIR}:/data" \
  -e "MEMEX_API_TOKEN=$TOKEN" \
  -e "OPENAI_API_KEY=no-key" \
  "$FULL_IMAGE")
[[ -n "$CID" ]] && pass "container started ($CID)" || { fail "docker run"; exit 1; }

# wait for /healthz
deadline=$((SECONDS + TIMEOUT_BOOT))
while (( SECONDS < deadline )); do
  if curl -fsS -m 2 "http://127.0.0.1:${HOST_PORT}/healthz" >/dev/null 2>&1; then
    pass "healthz responded within $((SECONDS - (deadline - TIMEOUT_BOOT)))s"
    break
  fi
  sleep 1
done
if ! curl -fsS -m 2 "http://127.0.0.1:${HOST_PORT}/healthz" >/dev/null 2>&1; then
  fail "container did not become healthy in ${TIMEOUT_BOOT}s"
  docker logs --tail=80 "$CONTAINER"
  exit 1
fi

# --- 4. confirm offline — server should not have phoned home on boot --------
step "4. offline-boot check"
LOGS=$(docker logs "$CONTAINER" 2>&1)
# Allowed: "Application startup complete", uvicorn lines.
# Disallowed: any sign that HF tried to reach the hub.
if echo "$LOGS" | grep -qiE "huggingface\.co|HF_HUB|cannot reach"; then
  fail "container logs reference huggingface.co (possible online fetch)"
  echo "$LOGS" | grep -iE "huggingface\.co|HF_HUB" | head -5 | sed 's/^/  /'
else
  pass "no huggingface.co traffic during boot"
fi

# --- 5. API surface --------------------------------------------------------
step "5. HTTP API"
AUTH=(-H "Authorization: Bearer $TOKEN")
BASE="http://127.0.0.1:${HOST_PORT}"

expect_curl "GET /healthz (open)"     200 GET "$BASE/healthz"
expect_curl "GET / (banner)"           200 GET "$BASE/"
expect_curl "GET /docs (OpenAPI UI)"   200 GET "$BASE/docs"
expect_curl "GET /openapi.json"        200 GET "$BASE/openapi.json"

# auth gating
expect_curl "GET /status without token → 401" 401 GET "$BASE/status"
expect_curl "GET /status wrong token → 401"   401 GET "$BASE/status" -H "Authorization: Bearer wrong-token"
expect_curl "GET /status with token → 200"    200 GET "$BASE/status" "${AUTH[@]}"

# doc add via API
ADD_RESP=$(curl -fsS -m 15 -X POST "$BASE/doc/add" "${AUTH[@]}" \
  -H "Content-Type: application/json" \
  -d '{"body":"# Postgres tuning\n\n## work_mem\n\nBump 64MB for analytics.\n","title":"Postgres tuning","tags":["db","reference"]}')
expect_contains "POST /doc/add returns id" "$ADD_RESP" '"id"'
expect_contains "POST /doc/add returns title" "$ADD_RESP" '"Postgres tuning"'
DOC_ID=$(echo "$ADD_RESP" | python3 -c 'import sys,json; print(json.load(sys.stdin)["id"])')
note "doc_id: $DOC_ID"

# search
SEARCH_RESP=$(curl -fsS -m 15 -G --data-urlencode "q=postgres work memory" --data-urlencode "k=2" "$BASE/doc/search" "${AUTH[@]}")
expect_contains "GET /doc/search hits Postgres" "$SEARCH_RESP" "Postgres"

# list / show / delete
expect_curl "GET /doc lists docs"       200 GET "$BASE/doc" "${AUTH[@]}"
expect_curl "GET /doc/{id} returns doc" 200 GET "$BASE/doc/$DOC_ID" "${AUTH[@]}"
expect_curl "DELETE /doc/{id} works"    200 DELETE "$BASE/doc/$DOC_ID" "${AUTH[@]}"
expect_curl "GET /doc/{id} after delete" 404 GET "$BASE/doc/$DOC_ID" "${AUTH[@]}"

# /ctx (no mem yet)
CTX_RESP=$(curl -fsS -m 15 -X POST "$BASE/ctx" "${AUTH[@]}" \
  -H "Content-Type: application/json" \
  -d '{"query":"postgres","include_profile":false,"include_memories":false,"budget":500}')
expect_contains "POST /ctx returns BEGIN block" "$CTX_RESP" "BEGIN memex-context"

# --- 6. mem (LLM-backed) ---------------------------------------------------
step "6. mem (verbatim insert — no LLM needed)"
MEM_ADD=$(curl -fsS -m 15 -X POST "$BASE/mem/add" "${AUTH[@]}" \
  -H "Content-Type: application/json" \
  -d '{"text":"User prefers TypeScript for new services","category":"pref"}')
expect_contains "POST /mem/add returns ids" "$MEM_ADD" '"ids"'

MEM_LS=$(curl -fsS -m 15 "$BASE/mem" "${AUTH[@]}")
expect_contains "GET /mem returns memories" "$MEM_LS" "TypeScript"

MEM_SEARCH=$(curl -fsS -m 15 -G --data-urlencode "q=typescript" --data-urlencode "k=3" "$BASE/mem/search" "${AUTH[@]}")
expect_contains "GET /mem/search hits" "$MEM_SEARCH" "TypeScript"

MEM_PROFILE=$(curl -fsS -m 15 "$BASE/mem/profile" "${AUTH[@]}")
expect_contains "GET /mem/profile renders" "$MEM_PROFILE" "About the user"

# --- 7. memex client (inside the container) ---------------------------------
step "7. in-container memex client"
INSIDE() { docker exec "$CONTAINER" env MEMEX_API_URL="http://127.0.0.1:8000" MEMEX_API_TOKEN="$TOKEN" "$@"; }
expect_contains "memex client status" "$(INSIDE memex client status --json 2>&1)" '"docs_count"'
expect_contains "memex client mem ls" "$(INSIDE memex client mem ls 2>&1)"  "TypeScript"

# --- 8. status / persistence ------------------------------------------------
step "8. status + persistence (restart container)"
STATUS_BEFORE=$(curl -fsS "$BASE/status" "${AUTH[@]}")
DOCS_BEFORE=$(echo "$STATUS_BEFORE" | python3 -c 'import sys,json; print(json.load(sys.stdin)["docs_count"])' 2>/dev/null || echo "?")
MEMS_BEFORE=$(curl -fsS "$BASE/mem" "${AUTH[@]}" | python3 -c 'import sys,json; print(len(json.load(sys.stdin)["memories"]))' 2>/dev/null || echo "?")
note "before restart — docs=$DOCS_BEFORE, mems=$MEMS_BEFORE"

# add one more doc so we can verify persistence
curl -fsS -X POST "$BASE/doc/add" "${AUTH[@]}" \
  -H "Content-Type: application/json" \
  -d '{"body":"# Persistence test\n\nlives across restart.\n","title":"Persistence test","tags":["restart"]}' >/dev/null
DOCS_AFTER_ADD=$(curl -fsS "$BASE/status" "${AUTH[@]}" | python3 -c 'import sys,json; print(json.load(sys.stdin)["docs_count"])')

docker restart "$CONTAINER" >/dev/null
# wait for back up
deadline=$((SECONDS + TIMEOUT_BOOT))
while (( SECONDS < deadline )); do
  if curl -fsS -m 2 "$BASE/healthz" >/dev/null 2>&1; then break; fi
  sleep 1
done

STATUS_AFTER=$(curl -fsS "$BASE/status" "${AUTH[@]}")
DOCS_AFTER=$(echo "$STATUS_AFTER" | python3 -c 'import sys,json; print(json.load(sys.stdin)["docs_count"])')
MEMS_AFTER=$(curl -fsS "$BASE/mem" "${AUTH[@]}" | python3 -c 'import sys,json; print(len(json.load(sys.stdin)["memories"]))')
note "after restart  — docs=$DOCS_AFTER, mems=$MEMS_AFTER"

expect_eq "docs survived restart"     "$DOCS_AFTER"     "$DOCS_AFTER_ADD"
expect_eq "memories survived restart" "$MEMS_AFTER"     "$MEMS_BEFORE"

# search the new doc to prove the vector index was persisted, not just the markdown
PERSIST_SEARCH=$(curl -fsS -G --data-urlencode "q=persistence restart" "$BASE/doc/search" "${AUTH[@]}")
expect_contains "search hits persisted doc" "$PERSIST_SEARCH" "Persistence test"

# --- 9. final summary -------------------------------------------------------
echo
echo "${B}######################################################"
echo "  RESULTS"
echo "  image      : $FULL_IMAGE  (${SIZE_MB} MB)"
echo "  ${G}PASS:${X} $PASS"
echo "  ${R}FAIL:${X} $FAIL"
if [[ $FAIL -gt 0 ]]; then
  echo
  echo "${R}Failed:${X}"
  for t in "${FAILED[@]}"; do echo "  - $t"; done
fi
echo "######################################################${X}"

exit $([[ $FAIL -eq 0 ]] && echo 0 || echo 1)
