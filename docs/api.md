# HTTP API reference

`memex serve` exposes the same operations as the CLI over HTTP. Use it when the agent and the data live on different machines (LLM in a container, memex on a host; or multiple developers sharing one personal KB).

## Boot

```bash
memex serve --host 0.0.0.0 --port 7963
```

OpenAPI / Swagger UI is at `/docs`; the raw schema is at `/openapi.json`.

## Authentication

Optional bearer token, via env var:

```bash
MEMEX_API_TOKEN=$(openssl rand -hex 32) memex serve
```

When set, **every endpoint except `/healthz` requires** `Authorization: Bearer <token>`. When unset the API is open (sensible for `127.0.0.1` and private docker networks; reckless for anything reachable from the internet).

## Architecture

```
+--------------------+        HTTP/1.1         +---------------------------+
|  caller            | ------- request ------> |  uvicorn :7963            |
|  (memex client,    |                         |  FastAPI app              |
|   curl, httpx,     | <----- response ------- |  built by build_app(root) |
|   any HTTP client) |                         +-------------+-------------+
+--------------------+                                       |
                                                             v
                                                +-------------------------+
                                                |  one Wiki + one MemStore|
                                                |  per process (shared    |
                                                |  across requests)       |
                                                +-------------------------+
                                                             |
                                            +----------------+----------------+
                                            v                                 v
                                  +-----------------+               +-----------------+
                                  |  ChromaDB       |               |  mem0 OSS       |
                                  +-----------------+               +-----------------+
```

A single `Wiki` and a single `MemStore` are built lazily and shared by every request — so the qdrant file-lock is taken once, and Chroma's in-memory caches stay warm.

## Endpoint matrix

```
+--------+-------------------------+---------------------------------------------+
| method | path                    | what it does                                |
+--------+-------------------------+---------------------------------------------+
| GET    | /                       | banner: name, version, root, auth_required  |
| GET    | /healthz                | liveness (always open, never authenticated) |
| GET    | /status                 | doc/chunk count, sizes, embedder, llm, ver  |
+--------+-------------------------+---------------------------------------------+
| POST   | /doc/add                | add a markdown doc                          |
| GET    | /doc                    | list docs (?tag=, ?since=)                  |
| GET    | /doc/search             | hybrid search (?q=, ?k=, ?tag=, ?since=)    |
| GET    | /doc/{ident}            | show one doc by id, slug, or path           |
| DELETE | /doc/{ident}            | remove one (?keep_file=true to keep on disk)|
| POST   | /doc/reindex            | reindex (?all=true forces full rebuild)     |
+--------+-------------------------+---------------------------------------------+
| POST   | /mem/add                | add a memory (verbatim or with infer=true)  |
| GET    | /mem                    | list memories (?category=)                  |
| GET    | /mem/profile            | rendered 'About the user' block             |
| GET    | /mem/search             | semantic search (?q=, ?k=, ?category=)      |
| GET    | /mem/{mem_id}           | show one memory                             |
| DELETE | /mem/{mem_id}           | delete by id, or 'all' to wipe              |
+--------+-------------------------+---------------------------------------------+
| POST   | /ctx                    | build the unified context block             |
+--------+-------------------------+---------------------------------------------+
| GET    | /docs                   | OpenAPI / Swagger UI                        |
| GET    | /openapi.json           | machine-readable schema                     |
+--------+-------------------------+---------------------------------------------+
```

## Conventions

- JSON in, JSON out. `Content-Type: application/json` for POST bodies.
- Error responses: `{ "error": "<ExceptionType>", "detail": "<message>" }`. Standard HTTP codes (`400` = validation, `401` = bad token, `404` = not found, `500` = internal).
- All timestamps are ISO 8601 UTC.

## Examples

### Healthz + banner

```bash
curl -fsS http://localhost:7963/healthz
# -> {"ok":true}

curl -fsS http://localhost:7963/
# -> {"name":"memex","version":"0.1.0","root":"/data","docs":"/docs",
#     "openapi":"/openapi.json","auth_required":true}
```

### Status (requires bearer)

```bash
TOKEN=...                       # from $MEMEX_API_TOKEN

curl -fsS http://localhost:7963/status -H "Authorization: Bearer $TOKEN"
# -> { "root":"/data", "user_id":"alice",
#      "docs_count": 23, "chunks_count": 142,
#      "embedder": "chroma-default:all-MiniLM-L6-v2",
#      "llm":      "openai:qwen3:4b",
#      "docs_dir_bytes": 1048576, ... ,
#      "version":"0.1.0" }
```

### Add a doc

```bash
curl -fsS -X POST http://localhost:7963/doc/add \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
        "body":   "# Postgres tuning\n\n## work_mem\n\nBump to 64MB.\n",
        "title":  "Postgres tuning",
        "tags":   ["db","reference"],
        "subdir": "reference"
      }'
# -> { "id":"01HZAB...","title":"Postgres tuning",
#      "path":"/data/docs/reference/postgres-tuning.md",
#      "tags":["db","reference"],
#      "created":"2026-06-01T12:00:00+00:00",
#      "updated":"2026-06-01T12:00:00+00:00" }
```

### Search

```bash
curl -fsS -G http://localhost:7963/doc/search \
  -H "Authorization: Bearer $TOKEN" \
  --data-urlencode 'q=postgres analytic memory' \
  --data-urlencode 'k=3' \
  --data-urlencode 'tag=db'
```

Response shape:

```json
{
  "query": "postgres analytic memory",
  "hits": [
    {
      "chunk_id": "01HZAB...#work-mem#0",
      "doc_id":   "01HZAB...",
      "title":    "Postgres tuning",
      "path":     "/data/docs/reference/postgres-tuning.md",
      "heading":  "Postgres tuning / work_mem",
      "text":     "# Postgres tuning\n\n## work_mem\n\nBump to 64MB.\n",
      "score":    0.317,
      "tags":     ["db", "reference"],
      "updated":  "2026-06-01T12:00:00+00:00"
    }
  ]
}
```

### Build a context block (what Cursor hooks call)

```bash
curl -fsS -X POST http://localhost:7963/ctx \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
        "query":            "what is our postgres tuning policy?",
        "budget":           2000,
        "top_k_docs":       5,
        "top_k_mems":       5,
        "include_profile":  true,
        "include_memories": true,
        "include_docs":     true
      }'
# -> { "block": "<!-- BEGIN memex-context (auto-generated by /ctx) -->\n
#                ## About the user\n- ...\n## Relevant docs\n### [...]\n...
#                <!-- END memex-context -->\n",
#      "tokens": 1342 }
```

### Add a memory (verbatim)

```bash
curl -fsS -X POST http://localhost:7963/mem/add \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
        "text":     "Prefers pgvector for hybrid search",
        "category": "pref"
      }'
# -> { "ids": ["abc-123-..."] }
```

To opt into mem0's LLM-driven fact extraction (split/merge/dedupe), add `"infer": true`. By default it's `false` — your text goes in exactly as-is.

### Render the profile block

```bash
curl -fsS http://localhost:7963/mem/profile -H "Authorization: Bearer $TOKEN"
# -> { "block": "## About the user\n\n- (profile) ...\n- (pref) ...\n",
#      "count": 7 }
```

### Reindex (admin)

```bash
curl -fsS -X POST 'http://localhost:7963/doc/reindex?all=true' -H "Authorization: Bearer $TOKEN"
# -> { "added": [...], "updated": [...], "skipped": [...], "deleted": [] }
```

## Pydantic schemas (the wire format)

Lifted from [`memex/server/schemas.py`](../memex/server/schemas.py); use these as the source of truth.

### DocAddRequest

```json
{
  "body":   "...",          // raw markdown, REQUIRED
  "title":  "...",          // optional; inferred from H1 / filename
  "tags":   ["..."],        // optional
  "subdir": "inbox"         // default: "inbox"
}
```

### DocOut (returned from add/show/list)

```json
{
  "id":      "01HZAB...",
  "title":   "...",
  "path":    "/data/docs/.../<slug>.md",
  "tags":    ["..."],
  "created": "2026-...",
  "updated": "2026-..."
}
```

### DocSearchHitOut (each element in /doc/search hits)

```json
{
  "chunk_id": "01HZ...#section#ord",
  "doc_id":   "01HZ...",
  "title":    "...",
  "path":     "...",
  "heading":  "...",
  "text":     "...",
  "score":    0.0,
  "tags":     ["..."],
  "updated":  "..."
}
```

### MemAddRequest / MemAddResponse / MemOut

```json
// request
{
  "text":     "...",        // REQUIRED
  "category": "fact",       // one of: profile pref project decision learning fact
  "tags":     ["..."],
  "infer":    false         // default false (verbatim); true = LLM extraction
}

// add response
{ "ids": ["..."] }

// item shape (search/list/show)
{
  "id":       "...",
  "text":     "...",
  "category": "...",
  "score":    0.0,
  "metadata": { ... }
}
```

### CtxRequest / CtxResponse

```json
// request
{
  "query":            "",
  "budget":           null,    // null -> use server's memex.yaml default
  "top_k_docs":       null,
  "top_k_mems":       null,
  "include_profile":  true,
  "include_memories": true,
  "include_docs":     true
}

// response
{ "block": "<!-- BEGIN ... -->\n...\n<!-- END memex-context -->\n",
  "tokens": 1342 }
```

### StatusResponse

```json
{
  "root":              "/data",
  "user_id":           "alice",
  "docs_count":        23,
  "chunks_count":      142,
  "embedder":          "chroma-default:all-MiniLM-L6-v2",
  "llm":               "openai:qwen3:4b",
  "docs_dir_bytes":    1048576,
  "chroma_dir_bytes":  348160,
  "mem0_dir_bytes":    65536,
  "history_dir_bytes": 4096,
  "version":           "0.1.0"
}
```

## Calling from a Cursor subagent

In the simplest case, an agent shells out to `memex client`:

```bash
export MEMEX_API_URL=https://memex.internal.example
export MEMEX_API_TOKEN=...
memex client ctx "$CURSOR_USER_PROMPT" --write /tmp/ctx.md --budget 2000
```

`memex client` is the recommended path for agents because:

1. It surfaces only safe, network-friendly operations.
2. Errors come out as `error 401: ...` instead of giant Python tracebacks.
3. The output formats match what the local CLI emits, so existing prompts that parse `memex` output don't need changes.

See also [docker.md](docker.md) for the full "Cursor subagent on host + memex in Docker" recipe.
