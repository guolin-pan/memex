# Cursor integration

memex plugs into Cursor through **three independent channels**. They serve different purposes and you can mix and match. No MCP server, no extra runtime — every channel just shells out to `memex`.

## The three channels

```
+------------------------------+--------------------------------+----------------------------------+
|  channel                     |  trigger                       |  what gets executed              |
+------------------------------+--------------------------------+----------------------------------+
|  A. Hooks                    |  Cursor lifecycle events       |  shell commands at specific      |
|     (~/.cursor/hooks.json)   |  (sessionStart,                |  moments, deterministic          |
|                              |   beforeSubmitPrompt,          |                                  |
|                              |   sessionEnd)                  |                                  |
+------------------------------+--------------------------------+----------------------------------+
|  B. Project rule             |  always-on; loaded into        |  no shell; instructs the main    |
|     (.cursor/rules/memex.mdc)|  every chat in the project     |  agent on when to invoke memex   |
+------------------------------+--------------------------------+----------------------------------+
|  C. Subagents                |  user types /memex-ask,        |  isolated subagent context with  |
|     (.cursor/agents/         |  /memex-archive,               |  its own system prompt, runs the |
|      memex-*.md)             |  /memex-curator                |  shell tool                      |
+------------------------------+--------------------------------+----------------------------------+
```

In one picture:

```
                  +---------------------------------------+
                  |  Cursor (chat in any project)         |
                  +------+--------------------+-----------+
                         |                    |
            (a) every    |                    |  (c) user explicitly invokes
                msg      |                    |      /memex-ask  etc.
                         v                    v
                  +-------------+      +----------------------+
                  |  Hooks A    |      |  Subagent  C         |
                  | beforeSubmit|      | (own context window) |
                  | etc.        |      +----------+-----------+
                  +------+------+                 |
                         |                        |
                         v                        v
                       memex client (HTTP)
                                  |
                                  v
                       memex server (memex serve / docker)
                                  ^
                                  |
                  +---------------+----------------------+
                  |  Project rule B                       |
                  |  loaded into main agent's             |
                  |  system prompt; reminds it            |
                  |  to call `memex client doc search`    |
                  |  / `memex client mem search` when     |
                  |  the user asks knowledge-shaped       |
                  |  questions                            |
                  +---------------------------------------+
```

## Pick what you need

```
+----------------------------------------+-----------------------------+
|  goal                                  |  channels                   |
+----------------------------------------+-----------------------------+
|  "Just inject background automatically.|  A (Hooks)                  |
|   I don't want to think about it."     |                             |
+----------------------------------------+-----------------------------+
|  "I want the main thread agent to      |  A + B                      |
|   reach for memex when it's useful."   |                             |
+----------------------------------------+-----------------------------+
|  "I want explicit, safe write          |  A + B + C                  |
|   operations (archive, curator)."      |                             |
+----------------------------------------+-----------------------------+
```

---

## A. Hooks (automatic context injection)

Install:

```bash
memex cursor install-hooks                       # default target ~/.cursor/hooks.json
memex cursor install-hooks --target ./project-hooks.json
```

What it wires up (every command goes through `memex client` so the same hooks work whether memex is local or in a Docker container):

```
+--------------------------+--------------------------------------------------+
| lifecycle event          | command                                          |
+--------------------------+--------------------------------------------------+
| sessionStart             | memex client mem profile                         |
|                          |   --write /tmp/cursor-memex-profile.md           |
| beforeSubmitPrompt       | memex client ctx "$CURSOR_USER_PROMPT" \         |
|                          |   --write /tmp/cursor-memex-ctx.md --budget 2000 |
+--------------------------+--------------------------------------------------+
```

The hook output (`/tmp/cursor-memex-ctx.md`) is the `<!-- BEGIN memex-context -->` block; Cursor inlines it into the prompt the LLM sees, so you never have to ask "do you remember…?".

`memex client` reads `MEMEX_API_URL` (default `http://127.0.0.1:8000`) and `MEMEX_API_TOKEN` from the environment. Set those in your shell rc (or `.envrc`) once and every hook / subagent inherits them.

> **Note:** the previous template also ran `memex mem learn --from-cursor-transcript` on `sessionEnd`. That command reads the transcript file on the local disk and has **no HTTP equivalent**, so it is no longer in the default template — it would break against a Docker-deployed memex. If you run memex locally and want it back, add the entry manually to `~/.cursor/hooks.json`.

Cost: one `memex client ctx` call per user prompt. With the offline embedder + ChromaDB the round-trip is ~500 ms; with OpenAI it adds whatever the embeddings API takes.

To disable, delete the entries from `~/.cursor/hooks.json` or `memex cursor install-hooks --replace --force` and write a different file.

---

## B. Project rule (teach the main thread)

Install:

```bash
memex cursor install-rule .                      # writes .cursor/rules/memex.mdc
```

The slimmed `memex.mdc` covers exactly two things for the main agent:

1. How to read the auto-injected `<!-- BEGIN memex-context -->` block (use it directly; don't re-query).
2. When to manually shell out for a quick read-only lookup (`memex client doc search`, `memex client mem search`).

All write/maintenance operations are intentionally **delegated to the subagents** below — the main thread's rule explicitly tells the agent to NOT run `memex client doc add`, `memex client mem add`, `memex client doc rm`, etc.

Why bother if you already have hooks? Because hooks fire automatically with a single fixed budget; sometimes the agent needs a follow-up query (different angle, narrower tag filter). The rule licenses that.

---

## C. Subagents (user-invoked, isolated)

```bash
memex cursor install-agents --scope user                          # ~/.cursor/agents/
memex cursor install-agents --scope project --project-root .      # ./.cursor/agents/
memex cursor install-agents --only memex-ask                      # one at a time
```

Three focused agents ship:

```
+----------------+-------------+-------------------------------------------------+
|  name          |  readonly   |  purpose                                        |
+----------------+-------------+-------------------------------------------------+
|  /memex-ask    |  true       |  pure RAG Q&A over notes + memories;            |
|                |             |  always cites; never invents                    |
+----------------+-------------+-------------------------------------------------+
|  /memex-archive|  false      |  "save this", "remember that"; previews +       |
|                |             |  dedup-checks + confirms before writing         |
+----------------+-------------+-------------------------------------------------+
|  /memex-curator|  false      |  "clean duplicates / health-check the memex";   |
|                |             |  surveys first, asks per-action before destroy  |
+----------------+-------------+-------------------------------------------------+
```

Invoke from chat:

```
/memex-ask    What's our project-x stack?
/memex-archive   Save the conversation above as an architecture doc.
/memex-curator   Check for stale or contradictory prefs.
```

Each subagent runs in its **own Cursor context window**, with **its own system prompt** (see [`../templates/agents/`](../templates/agents/)) and (for `memex-ask`) `readonly: true` so it can't accidentally write. They use the shell tool to invoke `memex client` (the HTTP CLI; works against a local or Docker-deployed memex) and report results back to the main thread.

### Subagent file format

The shipped files use the documented Cursor frontmatter fields:

```yaml
---
name: memex-ask
description: Read-only RAG over the user's personal knowledge base...
model: inherit
readonly: true
is_background: false
---

You are **memex-ask**, the read-only personal-knowledge-base assistant.
...
```

Cursor docs (as of writing) cover these five fields only — there's no per-subagent shell allow-list. If you need that, layer your workspace permissions in `~/.cursor/cli-config.json` / `.cursor/cli.json` on top.

---

## Pointing the hooks / agents at the right server

`memex client` (used by every shipped hook and subagent) resolves the server like this:

1. `--url URL` / `-u URL` and `--token TOKEN` (CLI flags) — useful for one-offs.
2. `MEMEX_API_URL` and `MEMEX_API_TOKEN` (env vars) — recommended for hooks/subagents, set once in your shell rc.
3. Defaults: `http://127.0.0.1:8000` and no token.

```bash
# in ~/.bashrc / ~/.zshrc, or a project .envrc for direnv users:
export MEMEX_API_URL=http://memex.local:8000
export MEMEX_API_TOKEN=$(pass show memex/api-token)
```

If you need a per-subagent override (e.g. a curator that talks to a staging memex), edit `~/.cursor/agents/memex-curator.md` and pass `--url` / `--token` explicitly inside its commands.

### Going back to a pure-local CLI

If you run memex **only** as a local process and don't want the HTTP hop, replace `memex client` with `memex` everywhere in `~/.cursor/hooks.json` and `~/.cursor/agents/memex-*.md`. The local-only commands that have no HTTP form (`memex mem learn --from-cursor-transcript`, `memex doc graph`, `memex mem update`) become available again.

---

## Channel interactions to watch out for

```
+------+-----------------+--------------------------------------------------------+
| from | to              | interaction                                            |
+------+-----------------+--------------------------------------------------------+
| A    | (LLM context)   | Auto-injected block is ALWAYS present; main agent      |
|      |                 | should rely on it before re-querying. Rule B teaches   |
|      |                 | this.                                                  |
+------+-----------------+--------------------------------------------------------+
| A    | C               | Subagents inherit the same hooks, so /memex-ask also  |
|      |                 | sees the auto-injected context. Subagent prompts      |
|      |                 | should mention "if block is present, use it; don't    |
|      |                 | re-query unnecessarily."                              |
+------+-----------------+--------------------------------------------------------+
| B    | C               | Rule B explicitly tells the main agent NOT to do      |
|      |                 | writes; it should route to /memex-archive instead.    |
+------+-----------------+--------------------------------------------------------+
```

## Inspecting what got installed

```bash
memex cursor list-agents              # show the 3 agents + descriptions
memex cursor print-hooks              # dump the hooks.json template to stdout
memex cursor print-rule               # dump the memex.mdc rule
memex cursor print-agent memex-ask    # dump one specific agent
```

## Uninstall

memex doesn't ship an uninstaller — but everything it installs is a single file:

```bash
rm ~/.cursor/hooks.json                 # or just remove the memex-* entries
rm ./.cursor/rules/memex.mdc
rm ./.cursor/agents/memex-*.md          # or ~/.cursor/agents/...
```

The `~/memex/` data is untouched; uninstalling the Cursor wiring doesn't affect the KB.
