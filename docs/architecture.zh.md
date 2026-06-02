# 架构与设计决策

这页讲"为什么"——memex 为什么长成这个样子背后的权衡。想要"是什么"看 [overview.zh.md](overview.zh.md)；想要"怎么用"看 [cli.zh.md](cli.zh.md) 和 [api.zh.md](api.zh.md)。

## 指导原则

1. **本地优先。** 你的笔记和记忆都在你自己的硬盘上。云是可选的，永远不是必要的。
2. **两套后端，一个 CLI。** 不同的存储引擎对应不同的工作负载；用户只看到一个命令界面。
3. **没价值就别引入中间件。** 没有 MCP server、没有消息队列、没有需要照顾的守护进程。Hooks 走 shell。API 就是 FastAPI + uvicorn，没了。
4. **用户才是事实的源头，不是 LLM。** `mem add` 默认逐字。LLM 是可选的事实抽取（`--infer`、`mem learn`）。
5. **id 稳定，文件可移动。** Frontmatter 里的 ULID id 把"是什么"和"放在哪"解耦。随便重命名和移动。

## 分层蛋糕

```
+-------------------------------------------------------------------------+
|  Cursor chat / shell / CI / curl                                        |
+-------------------------------------------------------------------------+
                |
                | typer dispatch                http
                v                                |
+-------------------------------------------+   |
|  CLI 命令模块                              |  |
|  memex/commands/*_cmd.py                   |  |
|  init, doc, mem, ctx, cursor, status,      |  |
|  serve, client                             |  |
+--------+-----------------------------------+  |
         |                                      |
         |       FastAPI handlers (经由 HTTP 时) |
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
| chromadb persistent  |    | 或 ONNX Runtime       |    | 本地 qdrant +        |
| client + BM25        |    | (embedder)            |    | history.db + LLM     |
+----------------------+    +-----------------------+    +----------------------+
```

虚线以上每一层都是**无状态**的（最多缓存）。虚线以下每一层都有持久数据，每个进程只有一个实例。

## 为什么用两层记忆（mem0 + ChromaDB）

纯向量的知识库会存所有东西——包括"用户喜欢 TypeScript"的十几个改写版本。纯事实抽取的存储会丢掉长架构文档里的真实内容。

```
+--------------------------+----------------------+----------------------+
| 操作                      | mem0 (事实)          | ChromaDB (wiki)      |
+--------------------------+----------------------+----------------------+
| 加 100 条"I prefer X"    | 好（去重 + 合并）     | 浪费（一块一条；信号  |
| 类的小事实                |                      | 极弱）                |
+--------------------------+----------------------+----------------------+
| 加一篇 5 段的架构文档      | 差——LLM 抽取会有     | 好——切块后按子段     |
|                          | 损失地总结            | 检索                 |
+--------------------------+----------------------+----------------------+
| 用今天的偏好覆盖昨天的     | 好——mem0 会替换旧的  | 手动（改 md + 重索   |
|                          |                      | 引）                 |
+--------------------------+----------------------+----------------------+
| "我们 project-x 用什么栈?"| 差——还得回去找文档   | 好——top-k 片段带     |
|                          |                      | 引用                 |
+--------------------------+----------------------+----------------------+
| "用户偏好什么?"            | 好——profile 块聚合   | 差——还得回去找事实   |
+--------------------------+----------------------+----------------------+
```

所以 memex 两个都用，CLI 把这个分裂藏起来。`memex ctx` 并发查两边并合成一个块；subagents（`/memex-archive`）按输入类型把写入路由到合适的那一边。

## 为什么不上 MCP

把 CLI 形态的工具接到 LLM 上，MCP 是"显而易见"的选择。我们刻意不出 MCP。原因：

1. **延迟。** Cursor hooks 每次事件 fork 一个子进程，开销就一次 fork。MCP 需要一个常驻 stdio server，*且*每次工具调用走 JSON-RPC 往返。对同主机上的个人工具来说，是没有收益的开销。
2. **接触面。** 每多一个 MCP 工具，LLM 就多一个能犯错的地方。我们的 hook 直接注入上下文；agent 看到的是结果，不是工具。（你照样可以让 agent shell 调 `memex`，但 prompt 接触面是规则，不是工具列表。）
3. **可观测性。** Shell 命令失败就写 stderr 退非零。MCP 工具失败要解开两层 JSON 信封。前者更好调试。
4. **升级路径。** `pip install -U memex` 一步搞定。升级 MCP server 是两步（重启 host 进程），还要确保 host 知道新的工具签名。
5. **没有状态耦合。** Hook 无状态、每条 prompt 重新触发。MCP server 是个一旦底层数据布局变了就得重启的进程。

代价是：agent 不能像通过 MCP 那样*发现*工具。我们用一个 60 行的 Cursor rule 列举相关 CLI 命令来补偿。

如果你的场景确实需要 MCP（多工具组合、热插拔工具等），基于 HTTP API 在上面套一层就行——那是干净的包装。

## 为什么本地优先，而不是服务器优先

绝大多数知识工具（Notion、Obsidian Sync、Roam）都是云优先，然后再 bolt-on 本地。memex 是本地优先，再 bolt-on 服务器。这个选择有连锁影响：

- **没有多租户代码。** `user_id` 在 mem0 里就是个 tag；我们从不为"租户 A vs 租户 B"做性能索引。
- **没有登录。** API 有 Bearer 鉴权，那就是唯一的认证模型。
- **备份是 tar.gz，不是"从设置里导出"。** 它就是个目录。
- **故障模式是"你笔记本磁盘满了"，不是"平台挂了"。**

要云版？把 Docker 镜像扔 VPS、设个 token、暴露 API。云这件事是栈的上层，跟本地优先的设计不冲突。

## 并发模型

```
+----------+--------+---------------------------------------+
| 调用方   | 在哪    | 并发模型                              |
+----------+--------+---------------------------------------+
| CLI      | 进程   | 一个 CLI 一个进程，不开线程            |
+----------+--------+---------------------------------------+
| serve    | 进程   | uvicorn workers（默认 1）。            |
|          |        | 每个 worker 一份 Wiki + 一份 MemStore。|
+----------+--------+---------------------------------------+
| ctx      | 线程   | ThreadPoolExecutor 并行查 mem profile、|
|          |        | mem search、doc search。MemStore 的    |
|          |        | 懒构建上锁，避免 qdrant 被开两次。      |
+----------+--------+---------------------------------------+
| watcher  | 线程   | watchdog 事件循环 + Timer 做 debounce。|
|          |        | 写入串行经过同一个 Wiki 实例。          |
+----------+--------+---------------------------------------+
```

**qdrant 文件锁是关键约束。** 本地 qdrant 存储一次只能被**一个**进程打开。所以我们：

- 默认单 uvicorn worker，
- MemStore 的懒构建套锁（`memex/backends/mem_store.py`），
- 注册 `atexit` 释放锁，让 CLI 进程退出前先把锁还回去，
- 默认设 `MEM0_TELEMETRY=False`，避免 mem0 为遥测再开一个 qdrant collection。

## 为什么 `mem add` 默认逐字

mem0 的 `add(infer=True)` 会调 LLM 抽取 / 合并 / 去重。对精炼的输入（"记住：我喜欢 pgvector"）来说，太重、太慢、且容易被 LLM 改写用户的原话。所以我们：

- `MemStore.add()` 里 `infer=False` 是默认，
- CLI 暴露 `--infer` / `--no-infer`，
- `mem learn` 走独立方法（`MemStore.learn()`），永远 `infer=True`，对应"这是个长 transcript，你自己解析"的场景。

界线很清楚：**`mem add` = 用户是权威。`mem learn` = LLM 是策展人。**

## ID 策略

```
+------------+------------------+---------------------------------------------+
| 对象       | id 格式           | 为什么                                       |
+------------+------------------+---------------------------------------------+
| 文档       | ULID（26 字符，   | 按时间排序、URL 安全、可排序、永不撞；存在    |
|            | base32）         | frontmatter 里，所以文件可任意改名/移动      |
+------------+------------------+---------------------------------------------+
| chunk      | <doc_id>#        | 复合 + 标题 slug + 序号：确定性、可调试；      |
|            | <heading_slug># | 文档结构没变时重索引仍能一一对上              |
|            | <ord>            |                                             |
+------------+------------------+---------------------------------------------+
| memory     | uuid (mem0)      | mem0 选的，我们不跟它对着干                   |
+------------+------------------+---------------------------------------------+
```

最大的好处：磁盘上的文档可以 `mv` 从 `inbox/foo.md` 到 `projects/x/foo.md`，**啥都不会坏**——因为我们靠 frontmatter id 查找，不靠路径。

## Chunking 策略

按标题感知、按 fence 感知、贪心打包：

```
输入 markdown
        |
        v
+--------------------------------------+
| 1. 读原始 md                         |
| 2. 按 H2/H3 边界切                   |
|    （可配置；代码 fence 是原子的——  |
|    永不在 fence 中间切）             |
| 3. 把相邻段贪心打包到 target_tokens  |
| 4. 遇到超大段时按空行硬切（仍尊重    |
|    fence），在边界重复 overlap_tokens|
+------------------+-------------------+
                   |
                   v
            (chunk_id, heading, text, metadata) 的列表
                   |
                   v
        Chroma upsert(ids=, documents=, metadatas=)
```

代码 fence 原子，因为没有什么比把代码块切成两半更能毁掉检索质量了。

按标题切，因为用户写的就是*段*；保留段边界让每个 chunk 主题内聚，BM25 侧路才有用（关键词自然在段内聚集）。

token 计数走 tiktoken（`cl100k_base`），跟 OpenAI 模型兼容；对任何现代分词器都是合理的近似。

## 混合检索（向量 + BM25）

```
query
   |
   v
+---------------+        +---------------+
| 用配置的       |        | tokenize +    |
| embedder      |        | BM25 打分     |
| 算 embedding  |        | (Chroma docs  |
+-------+-------+        | 缓存在侧路)    |
        |                +-------+-------+
        v                        |
  ChromaDB top-k                 |
        |                        |
        +-----------+------------+
                    |
                    v
       每条混合打分：
       alpha * 向量分 + (1-alpha) * bm25 分
                    |
                    v
      排序、取 top_k、后置 tag/since 过滤
```

纯向量会漏精确关键词命中（"postgres_max_connections" 被改名了，query 用了新拼写但文档里是老的）。纯 BM25 漏改写。`alpha=0.5` 把两边的好处都拿到。

## 扩展点

| 想做什么 | 怎么做 |
|---|---|
| 加新 CLI 子命令 | 在 `memex/commands/<name>_cmd.py` 新建，在 `memex/cli.py` 注册。 |
| 加新 embedder | 在 `memex/backends/embeddings.py` 继承 `EmbeddingFunction`；在 `build_embedder()` 里注册。 |
| 加新 API 端点 | 在 `memex/server/api.py` 加 route；在 `memex/server/schemas.py` 加请求/响应模型。 |
| 加新 Cursor subagent | 在 `templates/agents/memex-<name>.md` 放文件；更新 `memex/commands/cursor_cmd.py` 的 `AGENT_NAMES`。 |
| 加新 memory category | 改 `memex/backends/mem_store.py` 的 `ALLOWED_CATEGORIES`；考虑要不要进 `PROFILE_CATEGORIES`。 |
| 换掉 ChromaDB | 仿 `memex/backends/chroma_store.py` 写一个同接口（`upsert_chunks`/`delete_doc`/`search` 等）的；让 `Wiki` 用新的。 |

## 我们做了哪些权衡，代价是什么

| 取舍 | 我们得到 | 我们让出 |
|---|---|---|
| 本地 qdrant，不开 qdrant server | 零运维、文件级可移植 | 单写者（一次只能一个进程） |
| 默认单 uvicorn worker | 无 worker 间 qdrant 争抢 | 并发受限；需要时多起几个实例+负载均衡 |
| Shell hooks，不上 MCP | 更简单、更快、可调试 | LLM 不能 tool-discover；要靠 Cursor rule 列命令 |
| `mem add` 默认逐字 | 可预测、快、无需 LLM | 用户把杂乱文本扔 `mem add` 会得到一坨大记忆；我们用 `--infer`/`mem learn` 文档来补偿 |
| Docker 镜像烤进所有运行时模型 | 容器开箱即用、完全离线、启动不联网 | 镜像 ~1.5-2 GB；不再提供瘦身版（明确选择以可靠性换体积） |
| 两层记忆（mem0 + Chroma） | 各司其职 | 内部稍复杂；用户只看一个 CLI |
| frontmatter-id，不用路径作 id | 文件可任意改名/移动 | 每个文件都得有 frontmatter；不能直接指向已有 wiki |
| ULID，不用 UUID | 按时间排序、更短、URL 友好 | 没 UUID 通用；UUID 周边工具更多 |

## 如果不用 memex 用什么

如果非要找最接近的替代：

- **只用 mem0**：覆盖个人记忆，没有 wiki RAG。
- **AnythingLLM / PrivateGPT**：覆盖 wiki RAG 带 UI，但没有个人记忆层，且和特定 UI 紧耦合。
- **Notion + 一个独立 RAG 工具**：更精致，但云优先，嵌入 agent 工作流更难。
- **DIY（一个 `~/notes/` 目录 + ChromaDB + 一个脚本）**：我们自己最初的做法，直到我们厌倦了把同样六个命令反复写一遍。

差异化在于：**两层记忆 + CLI + HTTP + Cursor 集成 全部在同一个工具、同一份配置文件里。**

## 接着读

- [overview.zh.md](overview.zh.md) —— 如果跳过了"是什么"的话。
- 源码其实很短：[`memex/core/`](../memex/core/) ~800 行，backends 再 ~500 行，commands 是机械的 Typer 接线。整个项目一下午就能审完。
