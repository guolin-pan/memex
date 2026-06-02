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
# pipefail so `cmd | tail` returns cmd's exit code, not tail's. Without this
# a failed `docker build ... | tail -200` looks like success.
set -o pipefail

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
#
# We intentionally DON'T use `curl -f` — it makes curl exit 22 on any 4xx/5xx
# and swallows the actual HTTP code (`-w "%{http_code}"` prints nothing).
# That made a correctly-returned 401 look like "got HTTP curl: (22)...".
# -s silences progress; -S still shows real errors (DNS, connection).
expect_curl() {
  local desc="$1" want="$2" method="$3" url="$4"; shift 4
  local code
  code=$(curl -sS -o /tmp/memex-curl-body -w "%{http_code}" -X "$method" "$url" "$@" 2>/tmp/memex-curl-err)
  if [[ "$code" == "$want" ]]; then
    pass "$desc"
  else
    fail "$desc (got HTTP $code, want $want)"
    echo "${D}---body (first 10 lines)---"; head -10 /tmp/memex-curl-body 2>/dev/null | sed 's/^/  /'; echo "---------------------------${X}"
    if [[ -s /tmp/memex-curl-err ]]; then
      echo "${D}---stderr---"; sed 's/^/  /' /tmp/memex-curl-err; echo "------------${X}"
    fi
  fi
}

cleanup() {
  echo
  step "cleanup"
  docker rm -f "$CONTAINER" >/dev/null 2>&1 || true
  # The data dir contains files written by the in-container uid 1000 user.
  # If that doesn't map to the host's uid, `rm -rf` will fail with permission
  # denied. Try as the current user; on failure suggest the manual fix.
  if ! rm -rf "$DATA_DIR" 2>/dev/null; then
    echo "  (could not rm $DATA_DIR as $(id -un); try: sudo rm -rf $DATA_DIR )"
  fi
}
# Cleanup only runs when KEEP=1 isn't set, so failures can be inspected.
if [[ "${KEEP:-0}" != "1" ]]; then
  trap cleanup EXIT
fi

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

# Forward proxy env to the build if the host has one. Pass both upper and
# lowercase forms because some tools (apt, curl) only look at one.
PROXY_ARGS=()
for _v in HTTP_PROXY HTTPS_PROXY NO_PROXY http_proxy https_proxy no_proxy; do
  if [[ -n "${!_v:-}" ]]; then
    PROXY_ARGS+=( --build-arg "${_v}=${!_v}" )
    note "proxy passthrough: ${_v}=${!_v}"
  fi
done

if [[ "${FAST:-0}" == "1" ]] && docker image inspect "$FULL_IMAGE" >/dev/null 2>&1; then
  note "FAST=1 and image exists — skipping rebuild"
  pass "image already present"
else
  # Stream the full build log to a file so the user can re-read it; print only
  # the tail in the terminal. The build's own exit code is captured separately.
  BUILD_LOG=/tmp/memex-docker-build.log
  docker build \
       --build-arg "WITH_LOCAL_MODELS=$WITH_LOCAL_MODELS" \
       "${PROXY_ARGS[@]}" \
       --progress=plain \
       -t "$FULL_IMAGE" \
       . > "$BUILD_LOG" 2>&1
  rc=$?
  tail -60 "$BUILD_LOG"
  if [[ $rc -eq 0 ]]; then
    pass "image built  (full log: $BUILD_LOG)"
  else
    fail "docker build (exit=$rc; full log: $BUILD_LOG)"
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
chmod 0777 "$DATA_DIR"  # works whether or not uid 1000 maps to host user

# Pre-populate memex.yaml with the LOCAL profile so the embedder uses the
# baked offline ONNX MiniLM (no network calls). Otherwise the container
# auto-uses the openai profile and embedding calls time out.
cat > "$DATA_DIR/memex.yaml" <<'YAML'
user_id: e2e

embedder:
  provider: chroma-default
  model: all-MiniLM-L6-v2

# LLM is only invoked by mem0 when --infer is used (this test never does).
# We still need a syntactically valid block so MemStore can build its config.
llm:
  provider: openai
  model: qwen3:4b
  temperature: 0.1
  base_url: http://127.0.0.1:0/v1
  api_key: no-key
YAML
note "wrote memex.yaml with local profile -> embedder=chroma-default"

# Run as the host user so files written to the bind-mounted /data are
# owned by the caller (not container uid 1000). The image's read-only
# model files at /opt/memex/models are world-readable, so the process can
# still load them under a different uid.
HOST_UID=$(id -u)
HOST_GID=$(id -g)
note "running container as uid:gid = ${HOST_UID}:${HOST_GID}"

CID=$(docker run -d \
  --name "$CONTAINER" \
  --user "${HOST_UID}:${HOST_GID}" \
  -p "${HOST_PORT}:8000" \
  -v "${DATA_DIR}:/data" \
  -e "MEMEX_API_TOKEN=$TOKEN" \
  -e "OPENAI_API_KEY=no-key" \
  -e "HOME=/home/memex" \
  -e "MEM0_DIR=/data/.cache/mem0_home" \
  -e "USER=memex" \
  -e "LOGNAME=memex" \
  -e "TORCHINDUCTOR_CACHE_DIR=/tmp/torch-inductor" \
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

# helper: print response body + http code, without -f (so 4xx/5xx still yield body)
# Generous timeout: the FIRST request after container start cold-loads the
# ChromaDB ONNX model into memory (~30s on slow disks).
_capture() {
  local method="$1" url="$2"; shift 2
  local code
  code=$(curl -sS -m 120 -o /tmp/memex-curl-body -w "%{http_code}" -X "$method" "$url" "$@" 2>/tmp/memex-curl-err)
  cat /tmp/memex-curl-body
  # FastAPI JSON responses don't end with \n, so without this echo the
  # ::HTTP_CODE:: marker ends up on the same line as the body and `sed
  # /::HTTP_CODE::/d` would then delete the body too.
  echo ""
  echo "::HTTP_CODE::$code"
}

# doc add via API
ADD_FULL=$(_capture POST "$BASE/doc/add" "${AUTH[@]}" \
  -H "Content-Type: application/json" \
  -d '{"body":"# Postgres tuning\n\n## work_mem\n\nBump 64MB for analytics.\n","title":"Postgres tuning","tags":["db","reference"]}')
ADD_CODE=$(echo "$ADD_FULL" | grep '::HTTP_CODE::' | sed 's/.*:://')
ADD_RESP=$(echo "$ADD_FULL" | sed '/::HTTP_CODE::/d')
expect_eq        "POST /doc/add returns HTTP 200" "$ADD_CODE" "200"
expect_contains  "POST /doc/add returns id"       "$ADD_RESP" '"id"'
expect_contains  "POST /doc/add returns title"    "$ADD_RESP" '"Postgres tuning"'
DOC_ID=$(echo "$ADD_RESP" | python3 -c 'import sys,json; print(json.load(sys.stdin)["id"])' 2>/dev/null || echo "<no-id>")
note "doc_id: $DOC_ID"

# search
SEARCH_FULL=$(_capture GET "$BASE/doc/search?q=postgres+work+memory&k=2" "${AUTH[@]}")
SEARCH_CODE=$(echo "$SEARCH_FULL" | grep '::HTTP_CODE::' | sed 's/.*:://')
SEARCH_RESP=$(echo "$SEARCH_FULL" | sed '/::HTTP_CODE::/d')
expect_eq       "GET /doc/search returns HTTP 200" "$SEARCH_CODE" "200"
expect_contains "GET /doc/search hits Postgres"    "$SEARCH_RESP" "Postgres"

# list / show / delete
expect_curl "GET /doc lists docs"       200 GET "$BASE/doc" "${AUTH[@]}"
expect_curl "GET /doc/{id} returns doc" 200 GET "$BASE/doc/$DOC_ID" "${AUTH[@]}"
expect_curl "DELETE /doc/{id} works"    200 DELETE "$BASE/doc/$DOC_ID" "${AUTH[@]}"
expect_curl "GET /doc/{id} after delete" 404 GET "$BASE/doc/$DOC_ID" "${AUTH[@]}"

# /ctx (no mem yet)
CTX_FULL=$(_capture POST "$BASE/ctx" "${AUTH[@]}" \
  -H "Content-Type: application/json" \
  -d '{"query":"postgres","include_profile":false,"include_memories":false,"budget":500}')
CTX_CODE=$(echo "$CTX_FULL" | grep '::HTTP_CODE::' | sed 's/.*:://')
CTX_RESP=$(echo "$CTX_FULL" | sed '/::HTTP_CODE::/d')
expect_eq       "POST /ctx returns HTTP 200" "$CTX_CODE" "200"
expect_contains "POST /ctx returns BEGIN block" "$CTX_RESP" "BEGIN memex-context"

# --- 6. mem (LLM-backed) ---------------------------------------------------
step "6. mem (verbatim insert — no LLM needed)"
MEMA_FULL=$(_capture POST "$BASE/mem/add" "${AUTH[@]}" \
  -H "Content-Type: application/json" \
  -d '{"text":"User prefers TypeScript for new services","category":"pref"}')
MEMA_CODE=$(echo "$MEMA_FULL" | grep '::HTTP_CODE::' | sed 's/.*:://')
MEM_ADD=$(echo "$MEMA_FULL" | sed '/::HTTP_CODE::/d')
expect_eq       "POST /mem/add returns HTTP 200" "$MEMA_CODE" "200"
expect_contains "POST /mem/add returns ids"      "$MEM_ADD" '"ids"'

MEM_LS=$(curl -sS -m 30 "$BASE/mem" "${AUTH[@]}")
expect_contains "GET /mem returns memories" "$MEM_LS" "TypeScript"

MEM_SEARCH=$(curl -sS -m 30 -G --data-urlencode "q=typescript" --data-urlencode "k=3" "$BASE/mem/search" "${AUTH[@]}")
expect_contains "GET /mem/search hits" "$MEM_SEARCH" "TypeScript"

MEM_PROFILE=$(curl -sS -m 30 "$BASE/mem/profile" "${AUTH[@]}")
expect_contains "GET /mem/profile renders" "$MEM_PROFILE" "About the user"

# --- 7. memex client (inside the container) ---------------------------------
step "7. in-container memex client"
INSIDE() { docker exec "$CONTAINER" env MEMEX_API_URL="http://127.0.0.1:8000" MEMEX_API_TOKEN="$TOKEN" "$@"; }
expect_contains "memex client status" "$(INSIDE memex client status --json 2>&1)" '"docs_count"'
expect_contains "memex client mem ls" "$(INSIDE memex client mem ls 2>&1)"  "TypeScript"

# --- 8. status / persistence ------------------------------------------------
step "8. status + persistence (restart container)"
STATUS_BEFORE=$(curl -sS "$BASE/status" "${AUTH[@]}")
DOCS_BEFORE=$(echo "$STATUS_BEFORE" | python3 -c 'import sys,json; print(json.load(sys.stdin)["docs_count"])' 2>/dev/null || echo "?")
MEMS_BEFORE=$(curl -sS "$BASE/mem" "${AUTH[@]}" | python3 -c 'import sys,json; print(len(json.load(sys.stdin)["memories"]))' 2>/dev/null || echo "?")
note "before restart - docs=$DOCS_BEFORE, mems=$MEMS_BEFORE"

# add one more doc so we can verify persistence
curl -sS -X POST "$BASE/doc/add" "${AUTH[@]}" \
  -H "Content-Type: application/json" \
  -d '{"body":"# Persistence test\n\nlives across restart.\n","title":"Persistence test","tags":["restart"]}' >/dev/null
DOCS_AFTER_ADD=$(curl -sS "$BASE/status" "${AUTH[@]}" | python3 -c 'import sys,json; print(json.load(sys.stdin)["docs_count"])')

docker restart "$CONTAINER" >/dev/null
# wait for back up
deadline=$((SECONDS + TIMEOUT_BOOT))
while (( SECONDS < deadline )); do
  if curl -sS -m 2 "$BASE/healthz" >/dev/null 2>&1; then break; fi
  sleep 1
done

STATUS_AFTER=$(curl -sS "$BASE/status" "${AUTH[@]}")
DOCS_AFTER=$(echo "$STATUS_AFTER" | python3 -c 'import sys,json; print(json.load(sys.stdin)["docs_count"])')
MEMS_AFTER=$(curl -sS "$BASE/mem" "${AUTH[@]}" | python3 -c 'import sys,json; print(len(json.load(sys.stdin)["memories"]))')
note "after restart  - docs=$DOCS_AFTER, mems=$MEMS_AFTER"

expect_eq "docs survived restart"     "$DOCS_AFTER"     "$DOCS_AFTER_ADD"
expect_eq "memories survived restart" "$MEMS_AFTER"     "$MEMS_BEFORE"

# search the new doc to prove the vector index was persisted, not just the markdown
PERSIST_SEARCH=$(curl -sS -G --data-urlencode "q=persistence restart" "$BASE/doc/search" "${AUTH[@]}")
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
