# CLI reference

Every command in one place. Run `memex <group> --help` for the canonical help text any time; this document adds examples + the "when do I reach for this?" framing.

## Install

If you don't yet have the `memex` binary on PATH, the fast path is:

```bash
git clone <repo-url> && cd memex
bash scripts/install.sh                  # auto-detects uv / pip, idempotent
source .venv/bin/activate
memex --version
```

See [quickstart.md](quickstart.md) for the alternatives (uv tool, pipx, manual venv).

## Global flags

```
memex [--root PATH | -R PATH] [--version] <command> ...
```

| Flag | Env var | Default | What it does |
|---|---|---|---|
| `--root`, `-R` | `MEMEX_ROOT` | `~/memex` | Switch the entire CLI to a different memex root. Affects every subcommand. |
| `--version` |  |  | Print the package version and exit. |

Exit codes: `0` success, `1` runtime error, `2` user error (bad flag, file not found, validation, etc.).

## Layout at a glance

```
+--------------+----------------------------------------------------------+
| memex init   |   one-time bootstrap                                     |
+--------------+----------------------------------------------------------+
| memex doc    |   add update edit rm ls show search reindex watch graph  |
+--------------+----------------------------------------------------------+
| memex mem    |   add ls show update rm search profile learn             |
+--------------+----------------------------------------------------------+
| memex ctx    |   unified context block builder (called by Cursor hooks) |
+--------------+----------------------------------------------------------+
| memex cursor |   install-hooks install-rule install-agents              |
|              |   list-agents print-hooks print-rule print-agent         |
+--------------+----------------------------------------------------------+
| memex status |   doc count + chunk count + sizes + provider summary     |
| memex backup |   tar.gz snapshot of the wiki                            |
| memex restore|   extract a snapshot into a fresh directory              |
+--------------+----------------------------------------------------------+
| memex serve  |   start the FastAPI server                               |
| memex client |   thin HTTP client (status ctx doc mem raw)              |
+--------------+----------------------------------------------------------+
```

---

## `memex init`

Bootstrap a fresh memex root directory.

```
memex init [DIR] [-u USER_ID] [-p PROFILE] [--no-git] [-f]
```

| Option | Default | Effect |
|---|---|---|
| `DIR` (positional) | `$MEMEX_ROOT` or `~/memex` | Where to create the root. |
| `-u`, `--user-id` | `default` | mem0 user_id, also used as a tag on every memory. |
| `-p`, `--profile` | `openai` | `openai` (cloud) or `local` (offline embeddings + OpenAI-compatible LLM). |
| `--no-git` | off | Skip `git init`. |
| `-f`, `--force` | off | Re-stamp `memex.yaml`, `.kbignore`, `.gitignore` even if they exist. |

Examples:

```bash
memex init                              # ~/memex + openai profile
memex init ~/work-kb -u me --profile local
memex init -f --profile local           # reset config to local in-place
```

---

## `memex doc`

The wiki side. All commands operate on `<root>/docs/`.

### `memex doc add`

```
memex doc add [PATH | -] [-t TITLE] [--tags T1,T2] [-d SUBDIR] [--open]
```

| Option | Default | Effect |
|---|---|---|
| `PATH` or `-` | `-` (stdin) | File to import. Use `-` to read markdown from stdin. |
| `-t`, `--title` | inferred from H1 / filename | Document title. |
| `--tags` | empty | Comma-separated tag list. Prefer 1-3 lowercase-hyphenated tags. |
| `-d`, `--subdir` | `inbox` | Subdirectory under `docs/` to land in. |
| `--open` | off | Open the new doc in `$EDITOR` after creation. |

Examples:

```bash
# from stdin
echo "# Note\n\nbody" | memex doc add - --title "Note" --tags inbox

# from an existing file (copied into the wiki, gets frontmatter)
memex doc add /tmp/scratch.md --tags work --subdir work
```

### `memex doc update PATH`

Re-index a single file after manual edits. The watcher (`memex doc watch`) does this automatically; this is the manual fallback.

### `memex doc edit IDENT`

Open the doc in `$EDITOR`, then re-index on save. `IDENT` may be the ULID id, slug, or filesystem path.

### `memex doc rm IDENT [--keep-file]`

Delete a doc from the index and (by default) from disk.

```bash
memex doc rm postgres-tuning            # by slug
memex doc rm 01HZAB...                  # by ulid
memex doc rm postgres-tuning --keep-file # only drop from chroma; keep .md
```

### `memex doc ls`

```
memex doc ls [--tag T] [--since DUR] [--json]
```

`--since` accepts ISO timestamps (`2026-01-01`) or durations (`30d`, `6h`, `2w`).

### `memex doc show IDENT [--raw]`

Print a doc. `--raw` includes the frontmatter; without it you get title + body only.

### `memex doc search`

```
memex doc search QUERY [-k N] [--tag T] [--since DUR] [--json] [--snippet-tokens N]
```

Hybrid vector + BM25 search. `--snippet-tokens` controls how much of each hit is shown (default 180 tokens).

Examples:

```bash
memex doc search "postgres tuning" -k 5
memex doc search "rust patterns" --tag learning --since 90d --json
```

### `memex doc reindex`

```
memex doc reindex [--all | --changed]
```

Default behavior is `--changed`: only re-embed docs whose `content_hash` differs from the stored chunk metadata. `--all` rebuilds everything; useful after changing chunking strategy or embedder. `--all` and `--changed` are mutually exclusive.

### `memex doc watch`

```
memex doc watch [--debounce SECS]
```

Long-running. Uses `watchdog` to keep the Chroma index in sync as you edit files in any editor. Atomic-rename saves (vim, VSCode) are coalesced via the debounce window (default 1.0s).

```bash
memex doc watch                         # foreground
memex doc watch &                       # background (your shell)
# or, in production: a systemd unit / launchd plist that runs this on login.
```

### `memex doc graph`

Emit a [mermaid](https://mermaid.js.org/) graph of inter-doc links (from each doc's frontmatter `links: [...]` field):

```bash
memex doc graph > graph.md
```

---

## `memex mem`

The personal-memory side, backed by mem0 OSS.

Five categories ship: `profile`, `pref`, `project`, `decision`, `learning`, `fact`. Use them consistently — the `profile` block at session start aggregates `profile` + `pref` only.

### `memex mem add TEXT`

```
memex mem add TEXT [-c CATEGORY] [--tag T] [--infer/--no-infer]
```

| Option | Default | Effect |
|---|---|---|
| `-c`, `--category` | `fact` | One of: `profile pref project decision learning fact`. |
| `--tag` | empty | Repeatable; stored alongside the memory. |
| `--infer/--no-infer` | `--no-infer` | Off = verbatim insert (one input -> one memory, no LLM call). On = mem0 LLM extracts/merges/dedupes. |

Use `--no-infer` (the default) when you have an exact fact you want stored as-is:

```bash
memex mem add "Prefers pgvector for hybrid search" --category pref
memex mem add "My role is senior backend engineer at Acme" --category profile
```

Use `--infer` when the text is messy and you want mem0 to do the parsing:

```bash
memex mem add "$(cat meeting-notes.md)" --infer
```

### `memex mem ls [-c CATEGORY] [--json]`

List memories for the configured `user_id`. Filter by category if useful.

### `memex mem show ID`

Print one memory in JSON.

### `memex mem update ID TEXT`

Replace a memory's text. **Destructive**: mem0 re-embeds the new text. Show before/after if you're scripting this.

### `memex mem rm`

```
memex mem rm ID            # single delete
memex mem rm all -y        # wipe everything for the configured user_id (require -y)
```

### `memex mem search QUERY [-k N] [-c CATEGORY] [--json]`

Semantic search over memories. Threshold is set by mem0 (`0.1` by default); tweak via `memex.yaml` if you want stricter matching.

### `memex mem profile [--write PATH] [--max-items N]`

Render the "About the user" block from `profile` + `pref` memories. This is what `sessionStart` hook calls.

### `memex mem learn`

```
memex mem learn [SOURCE] [--from PATH] [--from-cursor-transcript] [-c CATEGORY]
```

Always uses `infer=True`. Reads from positional `SOURCE` (path or `-`), `--from PATH`, `$CURSOR_TRANSCRIPT_PATH`, or stdin (in that priority order).

```bash
cat meeting.md | memex mem learn -
memex mem learn meeting.md
memex mem learn --from-cursor-transcript    # hook calls this on sessionEnd
```

---

## `memex ctx`

```
memex ctx QUERY [--write PATH] [--budget TOKENS] [-k DOCS] [--top-k-mems N]
                [--no-profile] [--no-memories] [--no-docs]
```

The Swiss-army knife. Builds a unified `<!-- BEGIN memex-context -->` block by querying profile + memories + docs in parallel and packing them under a token budget.

| Option | Default | Effect |
|---|---|---|
| `QUERY` | "" | The user's current prompt / topic. If empty, only the profile block can be produced. |
| `--write` | stdout | Write the block to this path (used by Cursor hooks). |
| `--budget` | from `memex.yaml` (`ctx.budget_tokens`) | Total token cap, tiktoken-counted. |
| `-k`, `--top-k-docs` | from `memex.yaml` |  |
| `--top-k-mems` | from `memex.yaml` |  |
| `--no-profile` / `--no-memories` / `--no-docs` | off | Drop a section. Profile only = lightest, useful for session-start. |

Examples:

```bash
memex ctx "what's our project-x stack?" --budget 2000
memex ctx --no-memories --no-docs --write /tmp/profile.md     # profile only
```

---

## `memex cursor`

Cursor integration helpers. See [cursor.md](cursor.md) for the "which channel does what" picture.

```
memex cursor install-hooks  [--target PATH] [--merge|--replace] [--force]
memex cursor install-rule   [PROJECT_ROOT] [--force]
memex cursor install-agents [-s user|project] [--project-root DIR]
                            [--only NAME ...] [-f]

memex cursor list-agents
memex cursor print-hooks
memex cursor print-rule
memex cursor print-agent NAME
```

| Sub | Default target | Notes |
|---|---|---|
| `install-hooks` | `~/.cursor/hooks.json` | `--merge` (default) augments existing hooks; `--replace` requires `--force`. |
| `install-rule` | `<project>/.cursor/rules/memex.mdc` | Pass any project root as the positional arg. |
| `install-agents` | `--scope user` -> `~/.cursor/agents/` ; `--scope project` -> `<root>/.cursor/agents/` | Use `--only memex-ask` etc. to install one at a time. |

---

## `memex status / backup / restore`

```
memex status [--json]
memex backup [-o PATH] [--include-cache]
memex restore ARCHIVE [--target DIR]
```

`status` shows doc count, chunk count, providers, and on-disk sizes for `docs/`, `.cache/chroma/`, `.cache/mem0/`, `.cache/history/`.

`backup` snapshots `docs/`, `memex.yaml`, `.kbignore` by default. With `--include-cache` it also includes Chroma + mem0 stores (so the snapshot is self-contained, no re-embedding needed on restore).

`restore` extracts into a fresh dir (refuses to overwrite a non-empty target). Verify with `MEMEX_ROOT=<target> memex status` before swapping in.

---

## `memex serve`

```
memex serve [--host H] [--port P] [--reload] [--workers N] [-R ROOT]
```

Boots a uvicorn-backed FastAPI server. Defaults: `127.0.0.1:8000`, single worker. See [api.md](api.md) for the endpoint surface and [docker.md](docker.md) for production deployment.

`--reload` is for development only; pin `--workers 1` with it.

---

## `memex client`

Thin httpx wrapper that mirrors the read/write API surface. Reads `MEMEX_API_URL` (default `http://127.0.0.1:8000`) and `MEMEX_API_TOKEN`.

```
memex client status               [--json]
memex client ctx QUERY            [--budget N] [-k K] [--write PATH]
                                  [--no-profile|--no-memories|--no-docs]
memex client doc add [PATH|-]     [-t TITLE] [--tags ...] [-d SUBDIR]
memex client doc search QUERY     [-k N] [--tag T] [--since DUR] [--json]
memex client doc ls               [--tag T] [--since DUR] [--json]
memex client doc show IDENT
memex client doc rm IDENT         [--keep-file]
memex client mem add TEXT         [-c CATEGORY] [--tag T] [--infer/--no-infer]
memex client mem search QUERY     [-k N] [-c CATEGORY] [--json]
memex client mem ls               [-c CATEGORY] [--json]
memex client mem show ID
memex client mem rm ID|all
memex client mem profile          [--write PATH] [--max-items N]
memex client raw METHOD PATH      [--body JSON]
```

Local-only commands (`init`, `watch`, `cursor *`, `backup`, `restore`, `serve`) are intentionally **not** exposed remotely — they touch the local filesystem in ways that don't make sense over HTTP.

Example: a Cursor subagent against a Docker deployment:

```bash
export MEMEX_API_URL=https://memex.internal.example/
export MEMEX_API_TOKEN=$(pass show memex/api-token)
memex client doc search "postgres tuning" -k 3
memex client ctx "what is project-x stack" --write /tmp/ctx.md
```
