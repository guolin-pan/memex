# CLI 命令手册

每个命令、每个参数、可复制的例子。`memex <group> --help` 永远是权威帮助文本；这份文档补充例子和"什么时候用"。

## 安装

如果当前 PATH 里还没有 `memex` 二进制，最快路径：

```bash
git clone <repo-url> && cd memex
bash scripts/install.sh                  # 自动选 uv / pip，幂等
source .venv/bin/activate
memex --version
```

其他方式（uv tool、pipx、手动 venv）见 [quickstart.zh.md](quickstart.zh.md)。

## 全局参数

```
memex [--root PATH | -R PATH] [--version] <命令> ...
```

| 参数 | 环境变量 | 默认 | 作用 |
|---|---|---|---|
| `--root`, `-R` | `MEMEX_ROOT` | `~/memex` | 切到另一个 memex 根。对所有子命令生效。 |
| `--version` |  |  | 打印版本号并退出。 |

退出码：`0` 成功，`1` 运行时错误，`2` 用户错误（参数错、文件不存在、校验失败等）。

## 一图看完

```
+--------------+----------------------------------------------------------+
| memex init   |   一次性初始化                                            |
+--------------+----------------------------------------------------------+
| memex doc    |   add update edit rm ls show search reindex watch graph  |
+--------------+----------------------------------------------------------+
| memex mem    |   add ls show update rm search profile learn             |
+--------------+----------------------------------------------------------+
| memex ctx    |   构建统一上下文块（Cursor hooks 调它）                   |
+--------------+----------------------------------------------------------+
| memex cursor |   install-hooks install-rule install-agents              |
|              |   list-agents print-hooks print-rule print-agent         |
+--------------+----------------------------------------------------------+
| memex status |   文档数 + chunk 数 + 磁盘用量 + provider 概要            |
| memex backup |   wiki 的 tar.gz 快照                                     |
| memex restore|   把快照解到一个新目录                                    |
+--------------+----------------------------------------------------------+
| memex serve  |   启动 FastAPI 服务                                       |
| memex client |   轻量 HTTP 客户端 (status ctx doc mem raw)               |
+--------------+----------------------------------------------------------+
```

---

## `memex init`

初始化一个全新的 memex 根目录。

```
memex init [DIR] [-u USER_ID] [-p PROFILE] [--no-git] [-f]
```

| 选项 | 默认 | 作用 |
|---|---|---|
| `DIR`（位置参数） | `$MEMEX_ROOT` 或 `~/memex` | 在哪里创建根目录。 |
| `-u`, `--user-id` | `default` | mem0 user_id，所有记忆都挂在这个 id 下。 |
| `-p`, `--profile` | `openai` | `openai`（云）或 `local`（离线嵌入 + OpenAI 兼容 LLM）。 |
| `--no-git` | 关 | 跳过 `git init`。 |
| `-f`, `--force` | 关 | 即使 `memex.yaml` 等已存在也重写。 |

例子：

```bash
memex init                              # ~/memex + openai profile
memex init ~/work-kb -u me --profile local
memex init -f --profile local           # 原地切到 local profile
```

---

## `memex doc`

wiki 侧。所有命令都作用于 `<root>/docs/`。

### `memex doc add`

```
memex doc add [PATH | -] [-t TITLE] [--tags T1,T2] [-d SUBDIR] [--open]
```

| 选项 | 默认 | 作用 |
|---|---|---|
| `PATH` 或 `-` | `-`（stdin） | 要导入的文件。`-` 表示从 stdin 读 markdown。 |
| `-t`, `--title` | 从 H1 / 文件名推断 | 文档标题。 |
| `--tags` | 空 | 逗号分隔的标签列表。建议 1-3 个小写、用 `-` 分隔。 |
| `-d`, `--subdir` | `inbox` | 落到 `docs/` 下哪个子目录。 |
| `--open` | 关 | 创建后用 `$EDITOR` 打开。 |

例子：

```bash
# 从 stdin
echo "# Note\n\nbody" | memex doc add - --title "Note" --tags inbox

# 从已有文件（复制进 wiki，补上 frontmatter）
memex doc add /tmp/scratch.md --tags work --subdir work
```

### `memex doc update PATH`

手动改文件后重新索引一篇。`memex doc watch` 自动做这件事；这是手动兜底。

### `memex doc edit IDENT`

用 `$EDITOR` 打开，保存后自动重新索引。`IDENT` 可以是 ULID、slug、或文件路径。

### `memex doc rm IDENT [--keep-file]`

从索引（默认也从磁盘）删除一篇文档。

```bash
memex doc rm postgres-tuning            # 按 slug
memex doc rm 01HZAB...                  # 按 ulid
memex doc rm postgres-tuning --keep-file # 只从 chroma 删，保留 .md
```

### `memex doc ls`

```
memex doc ls [--tag T] [--since DUR] [--json]
```

`--since` 接受 ISO 时间戳（`2026-01-01`）或时长（`30d`、`6h`、`2w`）。

### `memex doc show IDENT [--raw]`

打印一篇文档。`--raw` 包含 frontmatter；不加只显示标题 + 正文。

### `memex doc search`

```
memex doc search QUERY [-k N] [--tag T] [--since DUR] [--json] [--snippet-tokens N]
```

向量 + BM25 混合检索。`--snippet-tokens` 控制每条结果显示多少字（默认 180 token）。

例子：

```bash
memex doc search "postgres tuning" -k 5
memex doc search "rust patterns" --tag learning --since 90d --json
```

### `memex doc reindex`

```
memex doc reindex [--all | --changed]
```

默认 `--changed`：只对 `content_hash` 变化的文档重新嵌入。`--all` 强制重建全部，适合切换 chunking 策略或 embedder 后用。两者互斥。

### `memex doc watch`

```
memex doc watch [--debounce SECS]
```

常驻进程。用 `watchdog` 在你用任意编辑器改文件时同步更新 Chroma 索引。原子重命名保存（vim、VSCode）会被 debounce 窗口（默认 1.0 秒）合并。

```bash
memex doc watch                         # 前台
memex doc watch &                       # 当前 shell 后台
# 生产环境：写一个 systemd unit / launchd plist 让它开机启动。
```

### `memex doc graph`

按每篇文档的 frontmatter `links: [...]` 字段输出 [mermaid](https://mermaid.js.org/) 关系图：

```bash
memex doc graph > graph.md
```

---

## `memex mem`

个人记忆侧，底层是 mem0 OSS。

内置六个 category：`profile`、`pref`、`project`、`decision`、`learning`、`fact`。请贯彻使用——会话开始时的 profile 块只汇总 `profile` + `pref`。

### `memex mem add TEXT`

```
memex mem add TEXT [-c CATEGORY] [--tag T] [--infer/--no-infer]
```

| 选项 | 默认 | 作用 |
|---|---|---|
| `-c`, `--category` | `fact` | 取值：`profile pref project decision learning fact`。 |
| `--tag` | 空 | 可重复；和记忆一起存储。 |
| `--infer/--no-infer` | `--no-infer` | 关 = 逐字写入（一条输入一条记忆，不调 LLM）。开 = mem0 用 LLM 抽 / 合 / 去重。 |

要精确存下一条事实，用默认 `--no-infer`：

```bash
memex mem add "Prefers pgvector for hybrid search" --category pref
memex mem add "My role is senior backend engineer at Acme" --category profile
```

要让 mem0 自己解析非结构化文本，用 `--infer`：

```bash
memex mem add "$(cat meeting-notes.md)" --infer
```

### `memex mem ls [-c CATEGORY] [--json]`

列出当前 `user_id` 下的所有记忆。可按 category 过滤。

### `memex mem show ID`

按 id 打印一条记忆（JSON 格式）。

### `memex mem update ID TEXT`

替换一条记忆的文本。**破坏性**：mem0 会重新嵌入新文本。脚本里建议先 show 出 before/after。

### `memex mem rm`

```
memex mem rm ID            # 删一条
memex mem rm all -y        # 清空当前 user_id 下所有记忆（必须 -y）
```

### `memex mem search QUERY [-k N] [-c CATEGORY] [--json]`

记忆语义检索。默认阈值由 mem0 控制（`0.1`）；要更严格就在 `memex.yaml` 改。

### `memex mem profile [--write PATH] [--max-items N]`

把 `profile` + `pref` 类的记忆渲染成 "About the user" 块。`sessionStart` hook 调的就是它。

### `memex mem learn`

```
memex mem learn [SOURCE] [--from PATH] [--from-cursor-transcript] [-c CATEGORY]
```

永远走 `infer=True`。输入来源优先级：位置参数 `SOURCE`（路径或 `-`）→ `--from PATH` → `$CURSOR_TRANSCRIPT_PATH` → stdin。

```bash
cat meeting.md | memex mem learn -
memex mem learn meeting.md
memex mem learn --from-cursor-transcript    # sessionEnd hook 调的就是这个
```

---

## `memex ctx`

```
memex ctx QUERY [--write PATH] [--budget TOKENS] [-k DOCS] [--top-k-mems N]
                [--no-profile] [--no-memories] [--no-docs]
```

万用刀。并行查 profile + memories + docs，按 token 预算合并，生成一个 `<!-- BEGIN memex-context -->` 块。

| 选项 | 默认 | 作用 |
|---|---|---|
| `QUERY` | "" | 用户当前的 prompt / 主题。空字符串则只产 profile 块。 |
| `--write` | stdout | 把块写到这个路径（Cursor hooks 用）。 |
| `--budget` | 来自 `memex.yaml` (`ctx.budget_tokens`) | 总 token 上限，按 tiktoken 计数。 |
| `-k`, `--top-k-docs` | 来自 `memex.yaml` |  |
| `--top-k-mems` | 来自 `memex.yaml` |  |
| `--no-profile` / `--no-memories` / `--no-docs` | 关 | 关掉某段。只留 profile 最轻，适合 session-start。 |

例子：

```bash
memex ctx "what's our project-x stack?" --budget 2000
memex ctx --no-memories --no-docs --write /tmp/profile.md     # 只产 profile
```

---

## `memex cursor`

Cursor 接入助手。"哪个通道做什么"看 [cursor.zh.md](cursor.zh.md)。

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

| 子命令 | 默认落地位置 | 备注 |
|---|---|---|
| `install-hooks` | `~/.cursor/hooks.json` | `--merge`（默认）合并到已有 hooks；`--replace` 需要 `--force`。 |
| `install-rule` | `<project>/.cursor/rules/memex.mdc` | 位置参数传项目根。 |
| `install-agents` | `--scope user` -> `~/.cursor/agents/`；`--scope project` -> `<root>/.cursor/agents/` | `--only memex-ask` 只装某个。 |

---

## `memex status / backup / restore`

```
memex status [--json]
memex backup [-o PATH] [--include-cache]
memex restore ARCHIVE [--target DIR]
```

`status` 显示文档数、chunk 数、provider、磁盘占用（`docs/`、`.cache/chroma/`、`.cache/mem0/`、`.cache/history/`）。

`backup` 默认只打包 `docs/` + `memex.yaml` + `.kbignore`。`--include-cache` 把 Chroma 和 mem0 也一起打——这样 restore 时不用重新嵌入。

`restore` 解到一个新目录（已存在且非空时拒绝覆盖）。先用 `MEMEX_ROOT=<target> memex status` 验证，再替换原目录。

---

## `memex serve`

```
memex serve [--host H] [--port P] [--reload] [--workers N] [-R ROOT]
```

启 uvicorn + FastAPI 服务。默认 `127.0.0.1:7963`，单 worker。端点表见 [api.zh.md](api.zh.md)，生产部署见 [docker.zh.md](docker.zh.md)。

`--reload` 仅开发用；配它时 `--workers` 必须是 1。

---

## `memex client`

走 httpx 的轻量客户端，镜像了 API 的读写能力。

服务端选择（优先级：CLI 标志 > 环境变量 > 默认）：

| 项 | 标志 | 环境变量 | 默认 |
|---|---|---|---|
| 服务地址 | `--url`、`-u` | `MEMEX_API_URL` | `http://127.0.0.1:7963` |
| Bearer 令牌 | `--token` | `MEMEX_API_TOKEN` | _（空 = 不带鉴权头）_ |

`--url` / `--token` 在 `memex client` 这一层接收，对后续每个子命令都生效，比如 `memex client --url http://host:7963 --token abc doc search "x"`。

```
memex client [--url URL] [--token T]
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

只能在本地跑的命令（`init`、`watch`、`cursor *`、`backup`、`restore`、`serve`）刻意**不**通过 HTTP 暴露——它们碰本地文件系统的方式不适合远程操作。

例子：一个 Cursor subagent 通过 HTTP 访问 Docker 部署：

```bash
# 一次性指定：
memex client --url https://memex.internal.example/ --token "$(pass show memex/api-token)" \
  doc search "postgres tuning" -k 3

# 或在 shell 里钉一次，所有子命令继承：
export MEMEX_API_URL=https://memex.internal.example/
export MEMEX_API_TOKEN=$(pass show memex/api-token)
memex client doc search "postgres tuning" -k 3
memex client ctx "what is project-x stack" --write /tmp/ctx.md
```
