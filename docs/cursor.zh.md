# Cursor 接入

memex 通过**三条独立通道**接入 Cursor。它们各有所长，可以叠加。没有 MCP、没有额外常驻进程——每条通道都只是 shell 出去调 `memex`。

## 三条通道

```
+------------------------------+--------------------------------+----------------------------------+
|  通道                         |  触发                          |  执行什么                         |
+------------------------------+--------------------------------+----------------------------------+
|  A. Hooks                    |  Cursor 生命周期事件            |  在特定时刻执行 shell 命令；      |
|     (~/.cursor/hooks.json)   |  (sessionStart、               |  确定性                          |
|                              |   beforeSubmitPrompt、         |                                  |
|                              |   sessionEnd)                  |                                  |
+------------------------------+--------------------------------+----------------------------------+
|  B. 项目规则                  |  始终生效；项目内所有 chat 都   |  不调 shell；告诉主线 agent       |
|     (.cursor/rules/memex.mdc)|  会加载                        |  何时该用 memex                  |
+------------------------------+--------------------------------+----------------------------------+
|  C. Subagents                |  用户敲 /memex-ask、           |  独立 subagent 上下文，自己的     |
|     (.cursor/agents/         |  /memex-archive、              |  system prompt，用 shell 工具     |
|      memex-*.md)             |  /memex-curator                |                                  |
+------------------------------+--------------------------------+----------------------------------+
```

放一张图：

```
                  +---------------------------------------+
                  |  Cursor (任意项目的 chat)              |
                  +------+--------------------+-----------+
                         |                    |
            (a) 每条消息  |                    |  (c) 用户显式调用
                         |                    |      /memex-ask 等
                         v                    v
                  +-------------+      +----------------------+
                  |  Hooks  A   |      |  Subagent  C         |
                  | beforeSubmit|      | (独立上下文窗口)      |
                  | 等          |      +----------+-----------+
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
                  |  项目规则  B                          |
                  |  加载到主 agent 的 system             |
                  |  prompt；提醒它在用户问知识           |
                  |  类问题时调 `memex client doc search` |
                  |  / `memex client mem search`          |
                  +---------------------------------------+
```

## 按需挑

```
+----------------------------------------+-----------------------------+
|  目的                                   |  需要哪些通道                 |
+----------------------------------------+-----------------------------+
|  "自动注入背景就行，不想管。"            |  A (Hooks)                   |
+----------------------------------------+-----------------------------+
|  "主线 agent 该用 memex 时主动用。"      |  A + B                      |
+----------------------------------------+-----------------------------+
|  "要显式、安全的写入                    |  A + B + C                  |
|   (archive、curator)。"                |                             |
+----------------------------------------+-----------------------------+
```

---

## A. Hooks（自动上下文注入）

安装：

```bash
memex cursor install-hooks                       # 默认目标 ~/.cursor/hooks.json
memex cursor install-hooks --target ./project-hooks.json
```

接进的内容（每条命令都走 `memex client`，所以 memex 部署在本地或 Docker 都一样能用）：

```
+--------------------------+--------------------------------------------------+
| 生命周期事件              | 命令                                              |
+--------------------------+--------------------------------------------------+
| sessionStart             | memex client mem profile                         |
|                          |   --write /tmp/cursor-memex-profile.md           |
| beforeSubmitPrompt       | memex client ctx "$CURSOR_USER_PROMPT" \         |
|                          |   --write /tmp/cursor-memex-ctx.md --budget 2000 |
+--------------------------+--------------------------------------------------+
```

Hook 的输出（`/tmp/cursor-memex-ctx.md`）就是那个 `<!-- BEGIN memex-context -->` 块；Cursor 会内联到 LLM 看到的 prompt 里，所以你永远不需要问"你还记得……吗？"。

`memex client` 读 `MEMEX_API_URL`（默认 `http://127.0.0.1:8000`）和 `MEMEX_API_TOKEN`。把它们设进 shell rc（或 `.envrc`）一次，每个 hook / subagent 都会继承。

> **注意**：旧模板还有一条 `sessionEnd` 跑 `memex mem learn --from-cursor-transcript`。这个命令读本地 transcript 文件，**没有 HTTP 对应实现**，所以默认模板里已经移除——保留它会让 Docker 部署直接报错。如果你跑的是本地 memex 想要这条 hook 回来，自己在 `~/.cursor/hooks.json` 里加上即可。

成本：每条用户 prompt 触发一次 `memex client ctx`。离线 embedder + ChromaDB 的话往返 ~500 ms；用 OpenAI 会加上 embeddings API 的耗时。

禁用：删 `~/.cursor/hooks.json` 里的相关条目，或 `memex cursor install-hooks --replace --force` 写新文件。

---

## B. 项目规则（教主线程）

安装：

```bash
memex cursor install-rule .                      # 写到 .cursor/rules/memex.mdc
```

精简后的 `memex.mdc` 给主 agent 讲两件事：

1. 如何使用自动注入的 `<!-- BEGIN memex-context -->` 块（直接用，别重新查）。
2. 什么时候手动 shell 出去做轻量只读查询（`memex client doc search`、`memex client mem search`）。

所有写入 / 维护操作都被**显式委派给下面的 subagents**——主线规则明确告诉 agent **不要**跑 `memex client doc add`、`memex client mem add`、`memex client doc rm` 等。

既然已经有 hooks，为什么还需要规则？因为 hooks 是定额触发的；有时 agent 需要再做一次跟进查询（换个角度、加更窄的 tag 过滤）。规则给它"许可"。

---

## C. Subagents（用户主动唤起、独立隔离）

```bash
memex cursor install-agents --scope user                          # ~/.cursor/agents/
memex cursor install-agents --scope project --project-root .      # ./.cursor/agents/
memex cursor install-agents --only memex-ask                      # 只装一个
```

内置三个聚焦型 agent：

```
+----------------+-------------+-------------------------------------------------+
|  名字          |  readonly   |  用途                                           |
+----------------+-------------+-------------------------------------------------+
|  /memex-ask    |  true       |  对笔记 + 记忆做纯 RAG 问答；                    |
|                |             |  必引用、绝不编造                                |
+----------------+-------------+-------------------------------------------------+
|  /memex-archive|  false      |  "记一下"、"存档"；写前先预览 + 去重检查 + 确认 |
+----------------+-------------+-------------------------------------------------+
|  /memex-curator|  false      |  "清理重复 / 体检 memex"；先勘察后逐项确认才动手 |
+----------------+-------------+-------------------------------------------------+
```

在 chat 里唤起：

```
/memex-ask    What's our project-x stack?
/memex-archive   把上面这段对话存成架构文档。
/memex-curator   检查一下有没有过期或冲突的 pref。
```

每个 subagent 跑在**它自己的 Cursor 上下文窗口**里，有**自己的 system prompt**（看 [`../templates/agents/`](../templates/agents/)），`memex-ask` 还设了 `readonly: true` 防止误写。它们用 shell 工具调 `memex client`（走 HTTP，本地 / Docker 部署都通），结果回到主线程。

### Subagent 文件格式

打包的文件用 Cursor 文档里的五个 frontmatter 字段：

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

Cursor 文档（截至本文）只支持这五个字段——没有 per-subagent shell 命令白名单。如果需要那种能力，请在 `~/.cursor/cli-config.json` / `.cursor/cli.json` 配工作区级权限叠加上去。

---

## 让 hooks / agents 指向正确的服务端

`memex client`（被所有出厂 hook 和 subagent 使用）的服务端解析优先级：

1. `--url URL` / `-u URL` 和 `--token TOKEN`（CLI 标志）—— 适合一次性。
2. `MEMEX_API_URL` 和 `MEMEX_API_TOKEN`（环境变量）—— 推荐给 hooks/subagents 用，在 shell rc 里设一次。
3. 默认：`http://127.0.0.1:8000`，无 token。

```bash
# 在 ~/.bashrc / ~/.zshrc 里，或 direnv 用户的 .envrc 里：
export MEMEX_API_URL=http://memex.local:8000
export MEMEX_API_TOKEN=$(pass show memex/api-token)
```

如果某个 subagent 需要单独的 URL/token（比如让 curator 指向 staging memex），编辑 `~/.cursor/agents/memex-curator.md`，把 `--url` / `--token` 显式写到命令里。

### 退回到纯本地 CLI

如果 memex **只**作为本地进程跑，不想多走一次 HTTP，把 `~/.cursor/hooks.json` 和 `~/.cursor/agents/memex-*.md` 里的 `memex client` 全部替换成 `memex` 即可。这样 `memex mem learn --from-cursor-transcript`、`memex doc graph`、`memex mem update` 这些**没有 HTTP 对应**的本地命令也能用回来。

---

## 通道间需要注意的交互

```
+------+-----------------+--------------------------------------------------------+
| 从   | 到              | 互动                                                    |
+------+-----------------+--------------------------------------------------------+
| A    | (LLM 上下文)    | 自动注入的块**永远在**；主 agent 先用它再考虑重新查询。 |
|      |                 | 规则 B 会教这一点。                                     |
+------+-----------------+--------------------------------------------------------+
| A    | C               | Subagent 继承同一份 hooks，所以 /memex-ask 也能看到自动 |
|      |                 | 注入的上下文。Subagent 的 prompt 里要说"块存在时直接用，|
|      |                 | 别没事重新查"。                                         |
+------+-----------------+--------------------------------------------------------+
| B    | C               | 规则 B 明确告诉主 agent **不要**做写入，而要 route 到   |
|      |                 | /memex-archive。                                       |
+------+-----------------+--------------------------------------------------------+
```

## 看一下装了什么

```bash
memex cursor list-agents              # 列出 3 个 agent + 描述
memex cursor print-hooks              # 打印 hooks.json 模板
memex cursor print-rule               # 打印 memex.mdc 规则
memex cursor print-agent memex-ask    # 打印某一个 agent
```

## 卸载

memex 没附带卸载器——但它装的每样东西都是单个文件：

```bash
rm ~/.cursor/hooks.json                 # 或只删 memex-* 条目
rm ./.cursor/rules/memex.mdc
rm ./.cursor/agents/memex-*.md          # 或 ~/.cursor/agents/...
```

`~/memex/` 里的数据不动；卸载 Cursor 接入不影响 KB。
