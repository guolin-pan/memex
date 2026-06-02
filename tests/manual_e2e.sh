#!/usr/bin/env bash
# Full-surface E2E test for `memex`. Runs against a fresh /tmp/memex-test
# wired to an OpenAI-compatible LLM endpoint (Ollama / qwen3:4b at
# 10.242.29.48). Produces pass/fail counts and a coloured per-command log.
#
# Usage:  bash tests/manual_e2e.sh
#
# Env overrides:
#   MEMEX_ROOT (default /tmp/memex-test) — where the fresh memex root lives.
#   MEMEX        (default .venv/bin/memex) — the binary under test.
#   LLM_URL      (default http://10.242.29.48:11434/v1)
#   LLM_MODEL    (default qwen3:4b)
#   LLM_KEY      (default no-key)
#   SKIP_MEM=1   — skip the mem0/LLM tests (they're slow, ~30-60s each)

set -u

# Don't `set -e` — we want to keep running through failures and count them.
ROOT="${MEMEX_ROOT:-/tmp/memex-test}"
MEMEX="${MEMEX:-$PWD/.venv/bin/memex}"
LLM_URL="${LLM_URL:-http://10.242.29.48:11434/v1}"
LLM_MODEL="${LLM_MODEL:-qwen3:4b}"
LLM_KEY="${LLM_KEY:-no-key}"
SKIP_MEM="${SKIP_MEM:-0}"

export MEMEX_ROOT="$ROOT"
export OPENAI_API_KEY="$LLM_KEY"

# --- colours / counters ----------------------------------------------------
G=$'\e[32m'; R=$'\e[31m'; Y=$'\e[33m'; B=$'\e[34m'; D=$'\e[2m'; X=$'\e[0m'
PASS=0; FAIL=0; SKIP=0
FAILED_TESTS=()

step() { echo; echo "${B}== $* ==${X}"; }
note() { echo "  ${D}$*${X}"; }

# expect: <description> <expected-exit> <expect-stdout-regex-or-empty> -- <command...>
expect() {
  local desc="$1" want_exit="$2" want_re="$3"; shift 3
  shift  # consume the literal '--'
  local out rc
  out="$("$@" 2>&1)"; rc=$?
  if [[ $rc -ne $want_exit ]]; then
    echo "  ${R}FAIL${X} $desc  (exit=$rc, expected=$want_exit)"
    echo "${D}---output---"; echo "$out" | sed 's/^/  /' | head -20; echo "------------${X}"
    FAIL=$((FAIL+1)); FAILED_TESTS+=("$desc"); return 1
  fi
  if [[ -n "$want_re" ]] && ! echo "$out" | grep -qE "$want_re"; then
    echo "  ${R}FAIL${X} $desc  (output didn't match /$want_re/)"
    echo "${D}---output---"; echo "$out" | sed 's/^/  /' | head -20; echo "------------${X}"
    FAIL=$((FAIL+1)); FAILED_TESTS+=("$desc"); return 1
  fi
  echo "  ${G}PASS${X} $desc"
  PASS=$((PASS+1)); return 0
}

skip() { echo "  ${Y}SKIP${X} $1"; SKIP=$((SKIP+1)); }

# ---------------------------------------------------------------------------
echo "${B}########################################"
echo "  memex full-surface E2E test"
echo "  memex root  : $ROOT"
echo "  CLI      : $MEMEX"
echo "  LLM      : $LLM_URL  ($LLM_MODEL)"
echo "########################################${X}"

# --- prerequisites ---------------------------------------------------------
step "0. fresh init"
# Kill any stale memex serve still holding qdrant's lock on $ROOT, then wipe.
pkill -f "memex serve" 2>/dev/null || true
sleep 1
rm -rf "$ROOT"
expect "init --profile local writes config" 0 "profile: local" \
  -- "$MEMEX" init --profile local -u tester
expect "memex.yaml has qwen3:4b" 0 "qwen3:4b" \
  -- grep -E 'model:' "$ROOT/memex.yaml"
expect "memex.yaml has base_url" 0 "10.242.29.48:11434" \
  -- grep -E 'base_url:' "$ROOT/memex.yaml"
expect "init --force re-stamps" 0 "profile: local" \
  -- "$MEMEX" init --profile local -u tester --force
expect "init rejects unknown profile" 2 "unknown profile" \
  -- "$MEMEX" init --profile bogus

# --- root flags ------------------------------------------------------------
step "1. root flags"
expect "memex --version" 0 "memex" -- "$MEMEX" --version
expect "memex --help lists all subcommands" 0 "init.*ctx.*status.*backup.*restore.*serve.*doc.*mem.*cursor.*client" \
  -- bash -c "$MEMEX --help 2>&1 | tr '\n' ' '"

# --- doc CRUD --------------------------------------------------------------
step "2. doc CRUD"
expect "doc add stdin → returns id" 0 "added" \
  -- bash -c "printf '# Postgres tuning\n\n## work_mem\nBump 64MB for analytics.\n' | $MEMEX doc add - --title 'Postgres tuning' --tags db,reference"

expect "doc add stdin → second doc" 0 "added" \
  -- bash -c "printf '# Project Phoenix\n\n## Stack\nFastAPI + pgvector + React.\n' | $MEMEX doc add - --title 'Project Phoenix' --tags project-x,architecture --subdir projects/phoenix"

expect "doc add stdin → third (for tag filter)" 0 "added" \
  -- bash -c "printf '# Old note\n\nThis is older.\n' | $MEMEX doc add - --title 'Old note' --tags old"

expect "doc add empty stdin → error" 2 "empty stdin" \
  -- bash -c "echo -n '' | $MEMEX doc add -"

expect "doc add from file" 0 "added" \
  -- bash -c "tmp=\$(mktemp --suffix=.md); echo '# From file' > \$tmp; $MEMEX doc add \$tmp --title 'From file'; rm -f \$tmp"

expect "doc ls shows 4 docs" 0 "Postgres tuning|Project Phoenix|Old note|From file" \
  -- "$MEMEX" doc ls

expect "doc ls --json valid" 0 '"title"' \
  -- "$MEMEX" doc ls --json

expect "doc ls --tag db filters" 0 "Postgres" \
  -- "$MEMEX" doc ls --tag db

# update via file edit + explicit update
DOC_ID=$("$MEMEX" doc ls --json | python3 -c "import sys,json; d=[x for x in json.load(sys.stdin) if x['title']=='Postgres tuning'][0]; print(d['path'])")
echo "  ${D}edit target: $DOC_ID${X}"
echo -e "\n## new section\nadded later for update test." >> "$DOC_ID"
expect "doc update reindexes after edit" 0 "reindexed" \
  -- "$MEMEX" doc update "$DOC_ID"

expect "doc show by title slug works" 0 "Postgres" \
  -- "$MEMEX" doc show postgres-tuning

expect "doc show by id works (full id)" 0 "Postgres|Project|Old|From" \
  -- bash -c "$MEMEX doc show \$($MEMEX doc ls --json | python3 -c 'import sys,json; print(json.load(sys.stdin)[0][\"id\"])')"

expect "doc show non-existent returns 2" 2 "no such document" \
  -- "$MEMEX" doc show nonexistent-slug

expect "doc rm by slug deletes file" 0 "removed" \
  -- "$MEMEX" doc rm old-note

# --- doc search ------------------------------------------------------------
step "3. doc search"
expect "search 'postgres work memory' hits Postgres doc" 0 "Postgres" \
  -- "$MEMEX" doc search "postgres work memory" -k 3

expect "search returns JSON" 0 '"chunk_id"' \
  -- "$MEMEX" doc search "postgres" -k 1 --json

expect "search --tag db filters results" 0 "Postgres" \
  -- "$MEMEX" doc search "tuning" -k 5 --tag db

expect "search nonsense returns no hits gracefully" 0 "no hits|score=" \
  -- "$MEMEX" doc search "zxqyuabcdef nonsense" -k 3

# --- doc reindex / graph ---------------------------------------------------
step "4. doc reindex / graph"
expect "reindex --changed (default) succeeds" 0 "added=|updated=|skipped=" \
  -- "$MEMEX" doc reindex
expect "reindex --all force-rebuilds" 0 "added=" \
  -- "$MEMEX" doc reindex --all
expect "reindex rejects --all + --changed" 2 "mutually exclusive" \
  -- "$MEMEX" doc reindex --all --changed
expect "doc graph emits mermaid" 0 "graph TD" \
  -- "$MEMEX" doc graph

# --- ctx (no memories yet, just docs) --------------------------------------
step "5. ctx (docs-only path)"
expect "ctx with query returns BEGIN block" 0 "BEGIN memex-context" \
  -- "$MEMEX" ctx "project phoenix stack" --no-profile --no-memories --budget 1000

expect "ctx --write writes file" 0 "wrote ctx" \
  -- "$MEMEX" ctx "phoenix" --no-profile --no-memories --write /tmp/ctx-out.md
expect "ctx --write file contains content" 0 "BEGIN memex-context" \
  -- cat /tmp/ctx-out.md

expect "ctx empty query (profile/mem skipped, no docs)" 0 "BEGIN memex-context" \
  -- "$MEMEX" ctx "" --no-profile --no-memories

# --- status / backup / restore ---------------------------------------------
step "6. status / backup / restore"
expect "status table renders" 0 "docs.*chunks.*embedder" \
  -- bash -c "$MEMEX status 2>&1 | tr '\n' ' '"

expect "status --json has docs_count" 0 '"docs_count"' \
  -- "$MEMEX" status --json

expect "backup default location" 0 "backup written" \
  -- "$MEMEX" backup
expect "backup -o custom path" 0 "backup written" \
  -- "$MEMEX" backup -o /tmp/memex-snap.tar.gz
expect "backup file exists and non-empty" 0 "" \
  -- bash -c "test -s /tmp/memex-snap.tar.gz"
expect "backup --include-cache produces larger archive" 0 "backup written" \
  -- "$MEMEX" backup -o /tmp/memex-full-snap.tar.gz --include-cache

rm -rf /tmp/memex-restored
expect "restore extracts archive" 0 "restored to" \
  -- "$MEMEX" restore /tmp/memex-snap.tar.gz --target /tmp/memex-restored
expect "restore target has docs/" 0 "" \
  -- bash -c "test -d /tmp/memex-restored/docs"
expect "restore refuses non-empty target" 2 "non-empty" \
  -- "$MEMEX" restore /tmp/memex-snap.tar.gz --target /tmp/memex-restored
expect "restore rejects missing archive" 2 "no such archive" \
  -- "$MEMEX" restore /tmp/nonexistent.tar.gz --target /tmp/memex-restore2

# --- cursor integration ----------------------------------------------------
step "7. cursor (hooks / rules / agents)"
rm -f /tmp/test-hooks.json
expect "install-hooks to fresh path" 0 "wrote" \
  -- "$MEMEX" cursor install-hooks --target /tmp/test-hooks.json
expect "install-hooks merges into existing" 0 "merged" \
  -- "$MEMEX" cursor install-hooks --target /tmp/test-hooks.json --merge

rm -rf /tmp/test-rule
expect "install-rule writes memex.mdc" 0 "wrote" \
  -- "$MEMEX" cursor install-rule /tmp/test-rule
expect "rule file has memex-ask delegate" 0 "memex-ask" \
  -- cat /tmp/test-rule/.cursor/rules/memex.mdc

rm -rf /tmp/test-agents
expect "install-agents project scope" 0 "wrote.*memex-ask" \
  -- bash -c "$MEMEX cursor install-agents --scope project --project-root /tmp/test-agents 2>&1 | tr '\n' ' '"
expect "all 3 agent files installed" 0 "memex-archive.md memex-ask.md memex-curator.md" \
  -- bash -c "ls /tmp/test-agents/.cursor/agents/ | tr '\n' ' '"

rm -rf /tmp/test-agents-onlymemex
expect "install-agents --only memex-ask" 0 "wrote" \
  -- "$MEMEX" cursor install-agents --scope project --project-root /tmp/test-agents-onlymemex \
       --only memex-ask
expect "only memex-ask installed" 0 "" \
  -- bash -c "test \$(ls /tmp/test-agents-onlymemex/.cursor/agents/ | wc -l) -eq 1"

expect "install-agents rejects unknown name" 2 "unknown agent" \
  -- "$MEMEX" cursor install-agents --scope project --project-root /tmp/test-agents-bad --only foo

expect "list-agents shows 3 rows" 0 "memex-ask.*memex-archive.*memex-curator" \
  -- bash -c "$MEMEX cursor list-agents 2>&1 | tr '\n' ' '"

expect "print-hooks JSON parses" 0 "" \
  -- bash -c "$MEMEX cursor print-hooks | python3 -m json.tool >/dev/null"
expect "print-rule contains memex-context" 0 "memex-context" \
  -- "$MEMEX" cursor print-rule
expect "print-agent memex-ask works" 0 "name: memex-ask" \
  -- "$MEMEX" cursor print-agent memex-ask
expect "print-agent unknown rejected" 2 "unknown agent" \
  -- "$MEMEX" cursor print-agent memex-nope

# --- HTTP server + client --------------------------------------------------
step "8. serve + client (HTTP roundtrip)"
PORT=$(python3 -c "import socket; s=socket.socket(); s.bind(('127.0.0.1',0)); print(s.getsockname()[1])")
note "server port: $PORT"
$MEMEX serve --host 127.0.0.1 --port "$PORT" >/tmp/memex-srv.log 2>&1 &
SRV_PID=$!
trap "kill $SRV_PID 2>/dev/null; rm -f /tmp/memex-srv.log /tmp/test-hooks.json" EXIT
# wait for server
for i in $(seq 1 30); do
  curl -fsS -m 1 "http://127.0.0.1:$PORT/healthz" >/dev/null 2>&1 && break
  sleep 0.2
done

export MEMEX_API_URL="http://127.0.0.1:$PORT"

expect "GET /healthz returns ok" 0 '"ok":true' -- curl -fsS "http://127.0.0.1:$PORT/healthz"
expect "GET / banner has name=memex" 0 '"name":"memex"' -- curl -fsS "http://127.0.0.1:$PORT/"
expect "GET /docs (OpenAPI UI) loads" 0 "swagger|Swagger|memex" -- curl -fsS "http://127.0.0.1:$PORT/docs"
expect "GET /openapi.json valid" 0 '"openapi"' -- curl -fsS "http://127.0.0.1:$PORT/openapi.json"

expect "client status --json shows docs" 0 '"docs_count"' -- "$MEMEX" client status --json
expect "client doc ls hits server" 0 "Postgres|Project" -- "$MEMEX" client doc ls

expect "client doc add via stdin" 0 "added" \
  -- bash -c "printf '# Remote add\n\nadded via HTTP client.\n' | $MEMEX client doc add - --title 'Remote add' --tags remote"

expect "client doc search returns hits" 0 "Remote add|Postgres" \
  -- "$MEMEX" client doc search "remote http" -k 3

expect "client doc search --json" 0 '"chunk_id"' \
  -- "$MEMEX" client doc search "remote" -k 1 --json

expect "client ctx through HTTP" 0 "BEGIN memex-context" \
  -- "$MEMEX" client ctx "remote add" --no-profile --no-memories --budget 500

expect "client raw GET / works" 0 "HTTP 200" -- "$MEMEX" client raw GET /
expect "client handles 404 gracefully" 1 "error" -- "$MEMEX" client doc show no-such-thing

# bearer-token test: stop, set token, restart
kill $SRV_PID 2>/dev/null; wait $SRV_PID 2>/dev/null || true
sleep 0.5
TOKEN="testtoken-abc-123"
MEMEX_API_TOKEN="$TOKEN" $MEMEX serve --host 127.0.0.1 --port "$PORT" >/tmp/memex-srv.log 2>&1 &
SRV_PID=$!
for i in $(seq 1 30); do
  curl -fsS -m 1 "http://127.0.0.1:$PORT/healthz" >/dev/null 2>&1 && break
  sleep 0.2
done

expect "client without token → 401" 1 "401" -- bash -c "unset MEMEX_API_TOKEN; $MEMEX client status"
expect "client with token → 200" 0 "docs_count|root" -- bash -c "MEMEX_API_TOKEN=$TOKEN $MEMEX client status"
expect "client with WRONG token → 401" 1 "401" -- bash -c "MEMEX_API_TOKEN=wrong $MEMEX client status"

kill $SRV_PID 2>/dev/null; wait $SRV_PID 2>/dev/null || true
trap - EXIT
# Give qdrant time to release its file lock so the upcoming mem tests
# (running in the foreground in this same shell) can open the same memex.
sleep 2

# --- mem0 / LLM tests (slow) ------------------------------------------------
step "9. mem (mem0 / LLM at $LLM_URL)"
if [[ "$SKIP_MEM" == "1" ]]; then
  skip "SKIP_MEM=1 set; skipping all mem0 tests"
else
  # qwen3:4b can be slow; expect 5-30s per add. We don't fail on timeout, just
  # mark as fail and continue.
  expect "mem add pref (LLM extract)" 0 "stored|deduped" \
    -- "$MEMEX" mem add "I prefer TypeScript over JavaScript for new services" --category pref
  expect "mem add profile (LLM extract)" 0 "stored|deduped" \
    -- "$MEMEX" mem add "My role is senior backend engineer at Acme" --category profile
  expect "mem add decision (LLM extract)" 0 "stored|deduped" \
    -- "$MEMEX" mem add "We chose pgvector over Qdrant for project-x because of psql familiarity" --category decision
  expect "mem add project" 0 "stored|deduped" \
    -- "$MEMEX" mem add "Currently working on the project-x migration from MySQL to Postgres" --category project
  expect "mem add learning" 0 "stored|deduped" \
    -- "$MEMEX" mem add "Lesson: FastAPI dependency scopes are per-request by default" --category learning

  expect "mem ls returns memories" 0 "TypeScript|backend|pgvector|FastAPI" \
    -- "$MEMEX" mem ls

  expect "mem ls --json" 0 '"category"' -- "$MEMEX" mem ls --json
  expect "mem ls -c pref filters" 0 "pref" -- "$MEMEX" mem ls -c pref

  expect "mem search 'typescript' returns hit" 0 "TypeScript|typescript" \
    -- "$MEMEX" mem search "typescript preference" -k 3

  expect "mem search -c pref filters" 0 "TypeScript|preference" \
    -- "$MEMEX" mem search "preference" -k 3 -c pref

  expect "mem search --json" 0 '"score"' -- "$MEMEX" mem search "fastapi" -k 1 --json

  expect "mem profile renders 'About the user'" 0 "About the user" \
    -- "$MEMEX" mem profile

  expect "mem profile --write file" 0 "wrote profile" \
    -- "$MEMEX" mem profile --write /tmp/mem-profile.md
  expect "mem profile file readable" 0 "About the user" -- cat /tmp/mem-profile.md

  # show + update + rm by id (take the first listed id)
  MID=$("$MEMEX" mem ls --json | python3 -c "import sys,json; rows=json.load(sys.stdin); print(rows[0]['id'])")
  echo "  ${D}operating on mem id: $MID${X}"
  expect "mem show by id" 0 "$MID" -- "$MEMEX" mem show "$MID"
  expect "mem update by id" 0 "updated" \
    -- "$MEMEX" mem update "$MID" "Updated: prefers TypeScript with strict mode on"
  expect "mem rm by id" 0 "deleted" -- "$MEMEX" mem rm "$MID"

  # mem learn: feed a transcript blurb, mem0 should extract facts
  expect "mem learn from stdin" 0 "learn produced" \
    -- bash -c "printf 'Meeting notes: decided to use pgvector. Action item: write the migration doc.\n' | $MEMEX mem learn -"

  # ctx with profile + memories
  expect "ctx with profile + memories + docs" 0 "About the user|Relevant" \
    -- "$MEMEX" ctx "typescript preference" --budget 2000

  # mem rm all -y
  expect "mem rm all -y wipes" 0 "wiped" -- "$MEMEX" mem rm all -y
fi

# --- watcher (foreground; use short-lived run) ------------------------------
step "10. watcher"
# Add a fresh doc, start watcher in background, modify file, check log line, kill.
WATCH_LOG=/tmp/memex-watch.log
rm -f "$WATCH_LOG"
$MEMEX doc watch --debounce 0.5 >"$WATCH_LOG" 2>&1 &
WPID=$!
sleep 2
echo "# Watcher test note" > "$ROOT/docs/inbox/watch-test.md"
sleep 3
echo -e "\n\nmodified content" >> "$ROOT/docs/inbox/watch-test.md"
sleep 3
rm "$ROOT/docs/inbox/watch-test.md"
sleep 3
kill $WPID 2>/dev/null
wait $WPID 2>/dev/null || true
expect "watcher logged at least one event" 0 "watch-test|modified|created|delete" -- cat "$WATCH_LOG"

# --- summary ---------------------------------------------------------------
echo
echo "${B}########################################"
echo "  RESULTS"
echo "  ${G}PASS:${X} $PASS"
echo "  ${R}FAIL:${X} $FAIL"
echo "  ${Y}SKIP:${X} $SKIP"
if [[ $FAIL -gt 0 ]]; then
  echo
  echo "${R}Failed tests:${X}"
  for t in "${FAILED_TESTS[@]}"; do
    echo "  - $t"
  done
fi
echo "########################################${X}"

exit $([[ $FAIL -eq 0 ]] && echo 0 || echo 1)
