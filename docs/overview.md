# Overview

> *"Consider a future device for individual use, which is a sort of mechanized private file and library."* — Vannevar Bush, *As We May Think*, 1945

`memex` is a single-user, local-first personal assistant + knowledge base. It pairs **two complementary memory layers** behind one CLI (`memex`), one HTTP API (`memex serve`), and one set of Cursor integrations (hooks + rules + subagents).

## The two layers, in one sentence

- **mem0** holds a few hundred short facts the user has explicitly told you about themselves.
- **ChromaDB** holds the markdown wiki the user has written, chunked and embedded for retrieval.

Each layer is good at exactly what the other is bad at:

| Layer | Holds | Typical entry size | Wins at |
|---|---|---|---|
| mem0    | "I prefer pnpm." "We chose pgvector." | one short sentence | dedup, fact-merging, "know the user" |
| Chroma  | A whole architecture doc | several KB | semantic search over real content |

## Big-picture architecture

```
                            +------------------------------+
                            |  Cursor chat / shell / CI    |
                            +--------------+---------------+
                                           |
                  +------------------------+------------------------+
                  |                        |                        |
                  v                        v                        v
        +-------------------+   +-------------------+    +--------------------+
        |  memex CLI (local) |   |  memex client     |    |  Cursor hooks      |
        |  memex doc / mem / |   |  (HTTP wrapper)   |    |  + subagents       |
        |  ctx / status / ...|   |                   |    |                    |
        +---------+---------+   +---------+---------+    +---------+----------+
                  |                       |                        |
                  |                       v                        |
                  |             +-------------------+              |
                  |             |  memex serve      |              |
                  |             |  FastAPI :7963    | <------------+
                  |             +---------+---------+
                  |                       |
                  +-----------+-----------+
                              |
                              v
                  +-----------------------+
                  |  memex core (Python)  |
                  |  Wiki + MemStore      |
                  +-----+-----------+-----+
                        |           |
              +---------+           +-----------+
              |                                 |
              v                                 v
    +---------------------+           +----------------------+
    |  ChromaDB           |           |  mem0 OSS            |
    |  (wiki vectors)     |           |  (qdrant + history)  |
    |  ~/memex/.cache/    |           |  ~/memex/.cache/     |
    |  chroma/            |           |  mem0/               |
    +---------------------+           +----------------------+

    +----------------------------------------------------+
    |  ~/memex/docs/   (markdown wiki, optional git repo) |
    +----------------------------------------------------+
```

Three observations from the picture:

1. There is **exactly one filesystem layout** (`~/memex/`). Whether you use the CLI directly, talk to the HTTP API, or run inside Docker — the same `docs/`, the same `memex.yaml`, the same `.cache/`.
2. The HTTP layer is **strictly optional**. The CLI talks to the core directly; `serve` only exists so other machines / containers / LLM tools can hit the same data over the network.
3. Cursor integration is **shell-level**, not MCP. Hooks invoke `memex` (or `memex client`) as a child process. No daemon, no custom protocol.

## What you put in, what you get out

Two everyday flows show what the system actually does:

### Write flow — "save this fact about me"

```
  user types in Cursor:
  "Remember that I prefer pgvector for hybrid search."

       |
       v
  (a) Cursor subagent /memex-archive picks up the intent,
      previews the write, asks for yes/no, then runs:

      memex mem add "prefers pgvector for hybrid search" \
                    --category pref

       |
       v
  (b) MemStore stores the fact verbatim (infer=False)
       |
       v
  (c) mem0 writes:
        - text + metadata{category:"pref"} -> qdrant collection "kb_mem"
        - audit row                         -> history.db
       |
       v
  (d) New memory id printed; future ctx blocks will mention it.
```

### Read flow — "what's our project-x stack?"

```
  user types in Cursor:
  "What's our project-x stack?"

       |
       v
  Cursor beforeSubmitPrompt hook fires:
       memex ctx "What's our project-x stack?" --write /tmp/ctx.md

       |
       v
  ctx_cmd builds a unified block by querying THREE sources
  in parallel (ThreadPoolExecutor):

     +----------------+   +----------------+   +----------------+
     | mem.profile    |   | mem.search     |   | wiki.search    |
     | (long-term     |   | (relevant      |   | (top-k chunks  |
     |  profile/pref) |   |  memories)     |   |  from Chroma)  |
     +-------+--------+   +--------+-------+   +--------+-------+
             |                     |                    |
             +----------+----------+--------------------+
                        |
                        v
              +-----------------------+
              |  budget-aware merge   |
              |  (tiktoken-truncated) |
              +-----------+-----------+
                          |
                          v
                <!-- BEGIN memex-context -->
                ## About the user
                - (pref) prefers pgvector for hybrid search
                ## Relevant docs
                ### [Project X stack](...)
                ...
                <!-- END memex-context -->

       |
       v
  Cursor prepends the block to the prompt the LLM sees.
  The LLM answers with citations grounded in YOUR notes.
```

## Component sketch

```
+---------------------+----------------------------------------------------------+
| memex/core/         | Pure-Python, no I/O outside the configured root.         |
|   config.py         |   Loader for memex.yaml + dataclass schema.              |
|   document.py       |   Markdown parsing, frontmatter, heading-aware chunking. |
|   wiki.py           |   High-level docs operations (add/update/rm/search/...). |
|   utils.py          |   Slug, sha256, token counting, since-duration parsing.  |
+---------------------+----------------------------------------------------------+
| memex/backends/     | The two storage adapters.                                |
|   chroma_store.py   |   ChromaDB wrapper + BM25 hybrid scoring.                |
|   mem_store.py      |   mem0 OSS wrapper with our category convention.         |
|   embeddings.py     |   OpenAI / sentence-transformers / chroma-default.       |
+---------------------+----------------------------------------------------------+
| memex/commands/     | Typer subcommand modules, one file per group.            |
|   init/doc/mem/ctx/cursor/status/serve/client_cmd.py                           |
+---------------------+----------------------------------------------------------+
| memex/server/       | FastAPI app + Pydantic schemas exposed by `memex serve`. |
|   api.py / schemas.py / factory.py                                             |
+---------------------+----------------------------------------------------------+
| memex/integrations/ | Side-channels.                                           |
|   watcher.py        |   watchdog-based file watcher for `memex doc watch`.     |
+---------------------+----------------------------------------------------------+
| templates/          | Files shipped via `memex cursor install-*`.              |
|   hooks.json        |   sessionStart/beforeSubmitPrompt/sessionEnd wiring.     |
|   memex.mdc         |   Project-level Cursor rule for the main thread.         |
|   agents/*.md       |   Custom subagents (/memex-ask, /memex-archive, ...).    |
|   memex.local.yaml  |   "fully offline + Ollama" config profile.               |
+---------------------+----------------------------------------------------------+
```

## What memex is not

- **Not a hosted SaaS.** Everything lives on your disk. If you want a hosted variant, build it on top of the HTTP API.
- **Not a generic vector DB.** ChromaDB is the storage engine; memex's job is to keep markdown and vectors in sync.
- **Not a wrapper around an LLM provider.** The LLM is only used by mem0 to extract facts (and only when you opt into `--infer`). The wiki and most CLI commands work LLM-free.
- **Not an Obsidian / Notion replacement.** Bring your own editor; memex indexes the result.

## Reading next

- [quickstart.md](quickstart.md) — go from "git clone" to "first useful query" in five commands.
- [architecture.md](architecture.md) — why the design looks like it does (mem0 vs Chroma split, no-MCP decision, threading model, etc.).
- [cli.md](cli.md) — the exhaustive command surface.
