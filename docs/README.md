# memex documentation

Pick the angle that fits what you're trying to do. Every page has a Chinese twin (`*.zh.md`).

| Page | What it covers |
|---|---|
| [overview.md](overview.md) | What memex is, the two-layer memory model, big-picture data flow |
| [quickstart.md](quickstart.md) | Get from zero to "first useful query" in 5 minutes |
| [cli.md](cli.md) | Every CLI command, every flag, with copy-pasteable examples |
| [api.md](api.md) | HTTP / REST surface: auth, endpoints, schemas, error format |
| [docker.md](docker.md) | Container build (offline models baked in), compose, persistence, ops |
| [cursor.md](cursor.md) | Three Cursor integration channels: hooks, project rule, subagents |
| [config.md](config.md) | `memex.yaml` schema field-by-field, profiles, environment variables |
| [architecture.md](architecture.md) | Why the design looks like it does. Trade-offs and extension points |

## Reading paths

```
+-------------------+        +--------------------+        +-----------------+
| I want to try it  | -----> | quickstart.md      | -----> | cli.md          |
+-------------------+        +--------------------+        +-----------------+

+-------------------+        +--------------------+        +-----------------+
| I want to deploy  | -----> | docker.md          | -----> | config.md       |
+-------------------+        +--------------------+        +-----------------+
                                                                   |
                                                                   v
                                                           +-----------------+
                                                           | api.md          |
                                                           +-----------------+

+-------------------+        +--------------------+        +-----------------+
| I want to plug it | -----> | cursor.md          | -----> | overview.md     |
| into Cursor       |        |                    |        |                 |
+-------------------+        +--------------------+        +-----------------+

+-------------------+        +--------------------+        +-----------------+
| I want to extend  | -----> | architecture.md    | -----> | source under    |
| or audit it       |        |                    |        | memex/          |
+-------------------+        +--------------------+        +-----------------+
```

## Conventions

- All diagrams in these docs are pure ASCII (`+ - | -> <- <-> v ^`). No Unicode arrows or box-drawing characters. Safe to paste anywhere.
- Code blocks tagged `bash` are shell-ready; `yaml`, `json`, `python` reflect file content.
- `<placeholder>` means "replace with your value"; `$ENV_VAR` means a real env var.

## Outside this folder

- [`../README.md`](../README.md) — top-level project overview (the elevator pitch).
- [`../DOCKER.md`](../DOCKER.md) — operations-oriented Docker guide (deeper than [docker.md](docker.md)).
- `../templates/` — the actual hooks/rule/agent files that ship with the CLI.
- `../scripts/docker-build-test.sh` — one-shot build + E2E test against the live image.
