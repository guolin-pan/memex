# Configuration

Everything that controls memex's behavior lives in **two places**:

1. The YAML file `<root>/memex.yaml`.
2. A handful of environment variables for things YAML shouldn't hold (secrets, server bind address, the location of `<root>` itself).

This page walks both surfaces field-by-field.

## Where the YAML lives

```
<root>/memex.yaml
```

`<root>` defaults to `~/memex`. Override:

- CLI flag: `memex -R /path/to/root <command>` — once.
- Env var:  `export MEMEX_ROOT=/path/to/root` — persistent for the shell.
- Both:    CLI flag wins.

A first-time `memex init` writes the file from one of two templates:

- `--profile openai` (default) -> [`templates/memex.yaml`-style defaults written by `write_default_config`](../memex/core/config.py).
- `--profile local` -> [`templates/memex.local.yaml`](../templates/memex.local.yaml) (the fully-offline + OpenAI-compatible-LLM setup).

## YAML schema, field by field

The full grammar (every nested dataclass in [`memex/core/config.py`](../memex/core/config.py)):

```yaml
user_id: <string>

embedder:
  provider: <openai | sentence-transformers | chroma-default>
  model:    <model name>
  dims:     <int>           # optional; auto-detected for known models
  base_url: <url>           # optional; OpenAI-compatible endpoint override
  api_key:  <string>        # optional; the api_key for the endpoint above

llm:
  provider:    <openai | ollama>
  model:       <model name>
  temperature: <float>
  base_url:    <url>        # optional; OpenAI-compatible endpoint override
  api_key:     <string>     # optional; the api_key for the endpoint above

chunking:
  target_tokens:     <int>
  overlap_tokens:    <int>
  min_chunk_tokens:  <int>
  split_by_headings: [h2, h3]   # which heading levels split chunks

search:
  top_k_docs:    <int>
  top_k_mems:    <int>
  hybrid_alpha:  <float 0.0..1.0>   # 1.0 = pure vector, 0.0 = pure BM25
  min_score:     <float>

ctx:
  budget_tokens:    <int>
  include_profile:  <bool>
  include_memories: <bool>
  include_docs:     <bool>
```

Missing sections fall back to package defaults; missing keys within a section fall back to their dataclass defaults. So you can write a minimal yaml that only overrides what you care about:

```yaml
# minimal valid memex.yaml
user_id: alice
llm:
  base_url: http://10.242.29.48:11434/v1
  model:    qwen3:4b
  api_key:  no-key
```

## Field reference

### `user_id`
The mem0 user identifier. Every memory is stored under this id. Two `user_id` values on the same machine give you two independent personal-memory spaces; the wiki is shared.

### `embedder.*`

```
+------------------------+---------------+-----------------------------------------+
| provider               | default model | use when                                |
+------------------------+---------------+-----------------------------------------+
| openai                 | text-embedding-3-small | you have OPENAI_API_KEY, or an  |
|                        | (1536 dims)            | OpenAI-compatible endpoint via   |
|                        |                        | base_url/api_key                 |
+------------------------+------------------------+----------------------------------+
| sentence-transformers  | all-MiniLM-L6-v2       | you installed the [local] extra  |
|                        | (384 dims)             | (~800 MB torch)                  |
+------------------------+------------------------+----------------------------------+
| chroma-default         | all-MiniLM-L6-v2       | you want offline embeddings      |
|                        | (384 dims, ONNX        | with no torch (chromadb's bundled|
|                        |  runtime)              | ONNX runtime; ~80 MB)            |
+------------------------+------------------------+----------------------------------+
```

`dims` is auto-detected for known models (see [`_expected_embedding_dims()` in mem_store.py](../memex/backends/mem_store.py)). Override only if you use a model the heuristic doesn't know.

`base_url` and `api_key` work for `provider: openai` only. Useful for self-hosted gateways that speak OpenAI's `/v1` protocol — Ollama, vLLM, LM Studio, LiteLLM, an OpenAI-proxy etc. Example for an Ollama embedder:

```yaml
embedder:
  provider: openai
  model:    qwen3-embedding:latest
  base_url: http://localhost:11434/v1
  api_key:  no-key
```

### `llm.*`

The LLM is only used by mem0 for fact extraction (i.e. `mem add --infer` and `mem learn`). Verbatim `mem add` and the wiki layer are LLM-free.

```
+----------+----------------+-----------------------------------------------+
| provider | default model  | notes                                         |
+----------+----------------+-----------------------------------------------+
| openai   | gpt-4o-mini    | also covers any OpenAI-compatible endpoint    |
|          |                | via base_url+api_key (Ollama /v1, vLLM, ...)  |
+----------+----------------+-----------------------------------------------+
| ollama   | (per your run) | for endpoints that DON'T speak the OpenAI v1  |
|          |                | dialect and need mem0's native Ollama         |
|          |                | adapter; the base_url here lands as           |
|          |                | ollama_base_url in mem0's config              |
+----------+----------------+-----------------------------------------------+
```

For most local-LLM setups, `provider: openai` + an Ollama base_url is the recommended path — it goes through the well-tested OpenAI client and surfaces clearer errors.

### `chunking.*`

Controls how each markdown doc is sliced before embedding.

| Field | Default | Effect |
|---|---|---|
| `target_tokens` | 800 | Greedy ceiling per chunk (tiktoken-counted). |
| `overlap_tokens` | 100 | When a hard split is needed, repeat this many tokens at the boundary so context isn't lost. |
| `min_chunk_tokens` | 50 | Don't flush a chunk smaller than this unless we hit EOF. |
| `split_by_headings` | `[h2, h3]` | Which heading levels create new chunks. `h2` only = bigger chunks; `[h2, h3, h4]` = finer. |

Code fences are **always atomic** — never split mid-`\`\`\``-block, regardless of these settings.

Changing chunking? Run `memex doc reindex --all` afterwards.

### `search.*`

| Field | Default | Effect |
|---|---|---|
| `top_k_docs` | 5 | `memex doc search` and `memex ctx` default k. |
| `top_k_mems` | 5 | `memex mem search` and `memex ctx` default k. |
| `hybrid_alpha` | 0.5 | Weight of vector vs BM25. 1.0 = pure vector, 0.0 = pure BM25 keyword. |
| `min_score` | 0.0 | Drop hits with score below this after blending. |

### `ctx.*`

`memex ctx`'s default behavior. Hooks pass overrides via flags.

| Field | Default | Effect |
|---|---|---|
| `budget_tokens` | 2000 | Total token cap for the assembled context block. |
| `include_profile` | true | Include the "About the user" block. |
| `include_memories` | true | Include relevant-memories block (needs a non-empty query). |
| `include_docs` | true | Include wiki-search block (needs a non-empty query). |

The three sections share the budget proportionally (1 : 2 : 3 for profile : memories : docs). Each is tiktoken-truncated to its quota.

## Environment variables

These are the only env vars memex itself reads. Everything else (e.g. `OPENAI_API_KEY` for the OpenAI SDK, `HF_HOME` for HuggingFace) is read by the underlying libraries.

```
+----------------------+------------------+-----------------------------------------+
| name                 | who reads it     | what it does                            |
+----------------------+------------------+-----------------------------------------+
| MEMEX_ROOT           | CLI + server     | Override <root> path. Equivalent to     |
|                      |                  | -R flag on the CLI.                     |
| MEMEX_API_URL        | memex client     | The remote API to talk to.              |
|                      |                  | Default: http://127.0.0.1:7963          |
| MEMEX_API_TOKEN      | memex serve +    | If set on the server, every endpoint    |
|                      | memex client     | except /healthz needs Authorization:    |
|                      |                  | Bearer <token>. The client sends it     |
|                      |                  | automatically when set on its side.     |
| MEM0_TELEMETRY       | mem0 (memex sets | Defaulted to "False" by MemStore on     |
|                      | the default)     | first build so mem0 doesn't open a      |
|                      |                  | secondary qdrant collection just for    |
|                      |                  | telemetry. Set "True" explicitly to     |
|                      |                  | re-enable.                              |
+----------------------+------------------+-----------------------------------------+
```

Common standard env vars that DO affect memex:

```
+-----------------------+------------------------------------------------------+
| OPENAI_API_KEY        | required if llm or embedder uses provider: openai    |
|                       | (cloud); ignored when base_url points to local LLM   |
+-----------------------+------------------------------------------------------+
| HF_HOME               | where HuggingFace caches model files; in the Docker  |
|                       | image we set it to /opt/memex/models/hf so models    |
|                       | live in the image, not on the volume                 |
+-----------------------+------------------------------------------------------+
| TRANSFORMERS_OFFLINE  | set to 1 in the Docker image to force the HF stack   |
| HF_HUB_OFFLINE        | to use the baked cache only (no phone-home)          |
| HF_DATASETS_OFFLINE   |                                                      |
+-----------------------+------------------------------------------------------+
| EDITOR                | `memex doc edit` and `memex doc add --open`          |
+-----------------------+------------------------------------------------------+
```

## Profiles, side-by-side

```
+----------------------------------------------------+-----------------------------------------------+
| openai (cloud)                                     | local (offline emb + OpenAI-compatible LLM)   |
+----------------------------------------------------+-----------------------------------------------+
| user_id: <handle>                                  | user_id: <handle>                             |
|                                                    |                                               |
| embedder:                                          | embedder:                                     |
|   provider: openai                                 |   provider: chroma-default                    |
|   model: text-embedding-3-small                    |   model: all-MiniLM-L6-v2                     |
|                                                    |                                               |
| llm:                                               | llm:                                          |
|   provider: openai                                 |   provider: openai                            |
|   model: gpt-4o-mini                               |   model: qwen3:4b                             |
|   temperature: 0.1                                 |   temperature: 0.1                            |
|                                                    |   base_url: http://10.242.29.48:11434/v1      |
|                                                    |   api_key: no-key                             |
|                                                    |                                               |
| chunking / search / ctx: defaults                  | chunking / search / ctx: defaults             |
+----------------------------------------------------+-----------------------------------------------+
| needs OPENAI_API_KEY                               | needs nothing external                        |
+----------------------------------------------------+-----------------------------------------------+
```

## Common recipes

### Cloud OpenAI but with the offline embedder

```yaml
embedder:
  provider: chroma-default
  model:    all-MiniLM-L6-v2
llm:
  provider: openai
  model:    gpt-4o-mini
```

OpenAI's `OPENAI_API_KEY` still needs to be set in the environment; the embedder runs locally; mem0's fact extraction uses the cloud LLM.

### Self-hosted vLLM endpoint

```yaml
llm:
  provider: openai
  model:    Qwen/Qwen2.5-7B-Instruct
  base_url: http://vllm.internal:8000/v1
  api_key:  <vllm-token>
```

### Two memex roots on the same machine

```bash
# work
MEMEX_ROOT=~/work-memex memex init -u me-at-work
# personal
MEMEX_ROOT=~/personal-memex memex init -u me-personal
```

Then alias each profile:

```bash
alias work-memex='MEMEX_ROOT=~/work-memex memex'
alias home-memex='MEMEX_ROOT=~/personal-memex memex'
```

### Tweak retrieval defaults

```yaml
search:
  top_k_docs: 8         # broader recall
  hybrid_alpha: 0.7     # lean more on vectors
ctx:
  budget_tokens: 3000   # bigger context block
```

## Reloading config

The CLI reads `memex.yaml` fresh on every invocation. The server (`memex serve`) reads it **once on startup** and caches; restart the server (or `docker compose restart memex`) after editing the config.
