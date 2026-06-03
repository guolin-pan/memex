# Architecture & design decisions

This page is the "why" — the trade-offs behind why memex looks the way it does. If you want the "what", read [overview.md](overview.md); for "how", read [cli.md](cli.md) and [api.md](api.md).

## Guiding principles

1. **Local-first.** Your notes and memory live on your disk. Cloud is optional, never required.
2. **Two backends, one CLI.** Distinct storage engines for distinct workloads; users only ever see one command surface.
3. **No middleware unless it earns its keep.** No MCP server, no message queue, no daemon you have to babysit. Hooks are shell. The API is FastAPI + uvicorn, period.
4. **The user, not the LLM, is the source of truth.** `mem add` is verbatim by default. The LLM is opt-in for fact extraction (`--infer`, `mem learn`).
5. **Stable ids, mobile files.** ULID frontmatter ids decouple "what" from "where on disk". Move/rename freely.

## Layer cake

```
+-------------------------------------------------------------------------+
|  Cursor chat / shell / CI / curl                                        |
+-------------------------------------------------------------------------+
                |
                | typer dispatch                http
                v                                |
+-------------------------------------------+   |
|  CLI command modules                       |  |
|  memex/commands/*_cmd.py                   |  |
|  init, doc, mem, ctx, cursor, status,      |  |
|  serve, client                             |  |
+--------+-----------------------------------+  |
         |                                      |
         |       FastAPI handlers (when via HTTP)|
         v                                      v
+--------+----------------------+   +-----------+-----------+
|  Wiki + MemStore (core)        |   |  memex/server/api.py  |
|  memex/core/wiki.py            |   |                       |
|  memex/backends/mem_store.py   |   +-----------+-----------+
+--------+-----------------------+               |
         |                                       |
         v                                       v
+----------------------+    +-----------------------+    +----------------------+
| ChromaDB             |    | sentence-transformers |    | mem0 OSS             |
| chromadb persistent  |    | OR ONNX Runtime       |    | local qdrant +       |
| client + BM25        |    | (embedder)            |    | history.db + LLM     |
+----------------------+    +-----------------------+    +----------------------+
```

Each layer above the dashed line is **stateless** (or holds only caches). Each layer below it owns persistent data and gets a single instance per process.

## Why two memory layers (mem0 vs ChromaDB)

A purely-vector knowledge base will store everything — including dozens of paraphrases of "the user prefers TypeScript". A purely-fact-extracted store will lose the actual content of a long architecture doc.

```
+--------------------------+----------------------+----------------------+
| operation                | mem0 (facts)         | ChromaDB (wiki)      |
+--------------------------+----------------------+----------------------+
| add 100 small facts      | good (dedup + merge) | wasteful (one chunk  |
| of "I prefer X"          |                      | each; little signal) |
+--------------------------+----------------------+----------------------+
| add a 5-section          | poor — LLM extraction| good — chunked,      |
| architecture doc         | will lossily         | retrieved by sub-    |
|                          | summarize            | section              |
+--------------------------+----------------------+----------------------+
| update yesterday's       | good — mem0 will     | manual (edit md +    |
| pref to today's          | replace older entry  | reindex)             |
+--------------------------+----------------------+----------------------+
| "what's our project-x    | poor — would need    | good — top-k chunks  |
| stack?"                  | the doc anyway       | with citations       |
+--------------------------+----------------------+----------------------+
| "what does the user      | good — profile block | poor — would need    |
| prefer?"                 | aggregates           | the facts anyway     |
+--------------------------+----------------------+----------------------+
```

So memex uses both, and the CLI hides the split. `memex ctx` queries both in parallel and assembles a single block; subagents (`/memex-archive`) route writes to whichever side fits the input.

## Why no MCP

MCP would be the "obvious" choice for connecting a CLI-shaped tool to an LLM. We deliberately don't ship it. Reasons:

1. **Latency.** Cursor hooks invoke a child process per event; the marginal cost is one fork. MCP needs a long-lived stdio server *plus* every tool call goes through a JSON-RPC round-trip. For a personal tool on the same host, that's overhead with no benefit.
2. **Surface area.** Every MCP tool is one more thing the LLM can blunder into. Our hook injects context; agents only see results, not the tool. (You can still run `memex` from agent shell, but the prompt surface is the rule, not a tool list.)
3. **Observability.** A failing shell command writes to stderr and exits non-zero. A failing MCP tool returns a JSON envelope nested two deep. The former is easier to debug.
4. **Update story.** `pip install -U memex` is one step. Updating an MCP server is two (restart the host process), plus making sure the host knows about new tool signatures.
5. **No state coupling.** A hook is stateless and gets re-invoked per prompt. An MCP server is a process you have to restart when the underlying data layout changes.

The cost: agents can't *discover* tools the way they can with MCP. We compensate with a 60-line Cursor rule that lists the relevant CLI commands.

If your use case truly needs MCP (multi-tool composition, hot-swappable tools, etc.), build it on top of the HTTP API — it's a clean wrap.

## Why local-first, not server-first

Most knowledge tools (Notion, Obsidian Sync, Roam) start cloud and bolt local on. memex starts local and bolts server on. This choice cascades:

- **No multi-tenant code.** `user_id` is just a tag in mem0; we never index for "tenant A vs tenant B" performance.
- **No login.** The API has bearer auth; that's the only auth model.
- **Backups are tar.gz, not "export from settings".** They're a directory.
- **Failure modes are "your laptop ran out of disk", not "the platform is down."**

If you want the cloud variant — fine, run the Docker image on a VPS, set a token, expose the API. The local-first design is up the stack from that decision.

## Concurrency model

```
+----------+--------+---------------------------------------+
| caller   | where  | concurrency                           |
+----------+--------+---------------------------------------+
| CLI      | proc   | one CLI = one process, no threads     |
+----------+--------+---------------------------------------+
| serve    | proc   | uvicorn workers (1 by default).       |
|          |        | One Wiki + one MemStore per worker.   |
+----------+--------+---------------------------------------+
| ctx      | thread | ThreadPoolExecutor across mem profile,|
|          |        | mem search, doc search. MemStore has  |
|          |        | a Lock around lazy _build() to keep   |
|          |        | qdrant from being opened twice.       |
+----------+--------+---------------------------------------+
| watcher  | thread | watchdog event loop + a Timer for     |
|          |        | debounce. Writes are serialized       |
|          |        | through the Wiki instance.            |
+----------+--------+---------------------------------------+
```

**The qdrant file lock is the load-bearing constraint.** A local qdrant store can only be opened by **one** process. That's why we:

- run a single uvicorn worker by default,
- lazy-build MemStore behind a lock (`memex/backends/mem_store.py`),
- register an `atexit` close so a CLI process releases the lock before the next CLI process starts,
- set `MEM0_TELEMETRY=False` by default so mem0 doesn't open a *second* qdrant collection for its telemetry table.

## Why "verbatim by default" for `mem add`

mem0's `add(infer=True)` ships an LLM call to extract / merge / dedupe. For curated input ("remember: I prefer pgvector"), that's overkill, slow, and prone to the LLM rewriting the user's words. So we:

- default `infer=False` in `MemStore.add()`,
- expose `--infer` / `--no-infer` in the CLI,
- give `mem learn` its own method (`MemStore.learn()`) that always forces `infer=True`, for the "here's a transcript, you figure it out" case.

The line is: **`mem add` = user is the authority. `mem learn` = LLM is the curator.**

## ID strategy

```
+------------+------------------+---------------------------------------------+
| object     | id format        | why                                         |
+------------+------------------+---------------------------------------------+
| document   | ULID (26 chars,  | time-ordered, URL-safe, sortable, never     |
|            | base32)          | colliding; stored in frontmatter so file    |
|            |                  | moves/renames are free                      |
+------------+------------------+---------------------------------------------+
| chunk      | <doc_id>#        | composite + heading slug + ordinal:         |
|            | <heading_slug>#  | deterministic, debuggable; survives a       |
|            | <ord>            | reindex if doc structure didn't change      |
+------------+------------------+---------------------------------------------+
| memory     | uuid (mem0)      | mem0 chose this; we don't fight it          |
+------------+------------------+---------------------------------------------+
```

The big win: a doc on disk can be `mv`'d from `inbox/foo.md` to `projects/x/foo.md` and **nothing breaks**, because we look it up by frontmatter id, not path.

## Chunking strategy

Heading-aware, fence-aware, greedy packing:

```
input markdown
        |
        v
+--------------------------------------+
| 1. read raw md                       |
| 2. split on H2/H3 boundaries         |
|    (configurable; code fences are    |
|    atomic — never split mid-block)   |
| 3. greedily pack adjacent sections   |
|    up to target_tokens               |
| 4. when an oversized section is hit, |
|    hard-split on blank lines (still  |
|    fence-aware), repeating           |
|    overlap_tokens at the boundary    |
+------------------+-------------------+
                   |
                   v
            list of (chunk_id,
                     heading,
                     text,
                     metadata)
                   |
                   v
        Chroma upsert(ids=, documents=, metadatas=)
```

Code fences are atomic because nothing kills retrieval quality faster than half a code block.

Heading-based splitting because users write *sections*; preserving section boundaries means each chunk has a coherent topic, which makes the BM25 sidecar useful (keywords cluster naturally inside a section).

Token counting via tiktoken (`cl100k_base`) for compatibility with OpenAI models; the count is a reasonable proxy for any modern tokenizer.

## Hybrid retrieval (vector + BM25)

```
query
   |
   v
+---------------+        +---------------+
| embed via the |        | tokenize +    |
| configured    |        | BM25 score    |
| embedder      |        | over Chroma's |
+-------+-------+        | docs cached   |
        |                | in a sidecar  |
        v                +-------+-------+
  ChromaDB top-k                 |
        |                        |
        +-----------+------------+
                    |
                    v
       per-hit blend score:
       alpha * vector + (1-alpha) * bm25
                    |
                    v
      sort, slice top_k, post-filter (tag/since)
```

Pure-vector misses exact-keyword hits ("postgres_max_connections" was renamed and your query uses the new spelling but the doc has the old one). Pure-BM25 misses paraphrase. Blending both at `alpha=0.5` gets you most of the wins of either.

## Extension points

| What | How |
|---|---|
| Add a new CLI subcommand | Create `memex/commands/<name>_cmd.py`, register in `memex/cli.py`. |
| Add a new embedder backend | Subclass `EmbeddingFunction` in `memex/backends/embeddings.py`; register in `build_embedder()`. |
| Add a new API endpoint | Add a route in `memex/server/api.py`; add request/response models in `memex/server/schemas.py`. |
| Add a new Cursor subagent | Drop a `templates/agents/memex-<name>.md` file; update `AGENT_NAMES` in `memex/commands/cursor_cmd.py`. |
| Add a new "category" of memory | Edit `ALLOWED_CATEGORIES` in `memex/backends/mem_store.py`; consider `PROFILE_CATEGORIES` if it should land in the profile block. |
| Swap ChromaDB for something else | Build a sibling of `memex/backends/chroma_store.py` with the same interface (`upsert_chunks`, `delete_doc`, `search`, etc.); have `Wiki` instantiate it. |

## Trade-offs we made and the cost

| Trade | What we got | What we gave up |
|---|---|---|
| Local qdrant, no qdrant server | Zero ops, file-portable | Single-writer (one process at a time) |
| Single uvicorn worker by default | No cross-worker qdrant contention | Limited concurrent requests; fix by running multiple instances behind a load balancer if needed |
| Shell hooks, not MCP | Simpler, faster, debuggable | LLMs can't tool-discover; we have to maintain a Cursor rule that lists commands |
| Verbatim `mem add` default | Predictable, fast, doesn't need LLM | If users dump messy text into `mem add` they get one big memory; we documented `--infer` and `mem learn` to compensate |
| All runtime models baked into Docker | Container is offline-ready out of the box and makes zero network calls on start | Image is ~1.5-2 GB; we no longer ship a slim variant (deliberate trade: size for reliability) |
| Two memory backends (mem0 + Chroma) | Each one is great at its job | Slightly more complex internals; users only ever see one CLI surface |
| Frontmatter-id, not path-as-id | Files can be renamed/moved freely | We have to keep the markdown front of every file; can't just point at an existing wiki |
| ULID, not UUID | Time-ordered, shorter, URL-friendly | Slightly less standard than UUID; tooling around UUID is more common |

## What would you replace memex with?

If we had to articulate the closest equivalents:

- **mem0 alone**: covers personal-memory but not wiki RAG.
- **AnythingLLM, PrivateGPT**: cover wiki RAG with a UI, but no personal-memory layer and tight coupling to specific UIs.
- **Notion + a separate RAG tool**: more polish, but cloud-first, harder to embed into agent workflows.
- **DIY (a `~/notes/` dir + ChromaDB + a script)**: what we were before we got tired of rebuilding the same six commands.

The differentiator is the **two-layer memory + CLI + HTTP + Cursor integration** showing up as one tool with one config file.

## Reading next

- [overview.md](overview.md) — the "what" if you skipped it.
- The actual source is short: [`memex/core/`](../memex/core/) is ~800 lines, the backends are another ~500, commands are mechanical Typer wiring. The whole thing is auditable in an afternoon.
