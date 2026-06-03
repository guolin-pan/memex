# 总览

> *"设想一种供个人使用的未来设备，它是一种机械化的私人档案与图书馆。"* —— Vannevar Bush，《诚如所思》，1945

`memex` 是一个面向个人的、本地优先的助手 + 知识库。它把**两层互补的记忆**封装在一套 CLI（`memex`）、一套 HTTP API（`memex serve`）、和一套 Cursor 集成（hooks + rules + subagents）背后。

## 两层记忆，一句话讲完

- **mem0** 装几百条短事实——用户自己告诉助手的关于他/她自己的事。
- **ChromaDB** 装用户写的 markdown wiki，切块后向量化，用来检索。

每层都擅长另一层不擅长的东西：

| 层 | 装什么 | 单条大小 | 强项 |
|---|---|---|---|
| mem0    | "我喜欢 pnpm。""我们选了 pgvector。" | 一句话 | 去重、合并、"了解用户" |
| Chroma  | 一整篇架构文档 | 几 KB | 在真实内容上做语义检索 |

## 总体架构

```
                            +------------------------------+
                            |  Cursor 聊天 / shell / CI    |
                            +--------------+---------------+
                                           |
                  +------------------------+------------------------+
                  |                        |                        |
                  v                        v                        v
        +-------------------+   +-------------------+    +--------------------+
        |  memex CLI (本地)  |   |  memex client     |    |  Cursor hooks      |
        |  memex doc / mem / |   |  (HTTP wrapper)   |    |  + subagents       |
        |  ctx / status ...  |   |                   |    |                    |
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
    |  (wiki 向量)         |           |  (qdrant + history)  |
    |  ~/memex/.cache/    |           |  ~/memex/.cache/     |
    |  chroma/            |           |  mem0/               |
    +---------------------+           +----------------------+

    +----------------------------------------------------+
    |  ~/memex/docs/   (markdown wiki，可选 git 仓库)     |
    +----------------------------------------------------+
```

从这张图能看出三件事：

1. **只有一套文件目录**（`~/memex/`）。无论你直接用 CLI、走 HTTP API、还是在 Docker 里跑——同一份 `docs/`、同一份 `memex.yaml`、同一份 `.cache/`。
2. HTTP 层**完全是可选的**。CLI 直接调 core；`serve` 只在别的机器 / 容器 / LLM 工具要远程访问同一份数据时才需要。
3. Cursor 接入是**走 shell 的**，不是 MCP。Hooks 把 `memex`（或 `memex client`）当子进程跑。没有守护进程，没有自定义协议。

## 进什么，出什么

两条日常流程把系统在做的事直观摊开：

### 写入流程 —— "记一下这条事实"

```
  用户在 Cursor 里说：
  "记一下我喜欢 pgvector 做混合检索。"

       |
       v
  (a) Cursor subagent /memex-archive 识别意图，预览要写的内容，
      要确认（yes/no），然后跑：

      memex mem add "prefers pgvector for hybrid search" \
                    --category pref

       |
       v
  (b) MemStore 逐字存储（infer=False，不让 LLM 改写）
       |
       v
  (c) mem0 写入：
        - 文本 + metadata{category:"pref"} -> qdrant collection "kb_mem"
        - 审计行                            -> history.db
       |
       v
  (d) 打印新 memory id；之后的 ctx 块就会带上这条。
```

### 读取流程 —— "我们 project-x 用的是什么栈？"

```
  用户在 Cursor 里问：
  "What's our project-x stack?"

       |
       v
  Cursor 的 beforeSubmitPrompt hook 触发：
       memex ctx "What's our project-x stack?" --write /tmp/ctx.md

       |
       v
  ctx_cmd 用 ThreadPoolExecutor 并发查三个来源构建统一上下文：

     +----------------+   +----------------+   +----------------+
     | mem.profile    |   | mem.search     |   | wiki.search    |
     | (长期 profile/  |   | (与本次提问    |   | (Chroma top-k  |
     |  pref 类记忆)   |   |  相关的记忆)    |   |  片段)         |
     +-------+--------+   +--------+-------+   +--------+-------+
             |                     |                    |
             +----------+----------+--------------------+
                        |
                        v
              +-----------------------+
              |  按 token 预算合并    |
              |  (tiktoken 截断)      |
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
  Cursor 把这个块拼到 LLM 看到的 prompt 前面。
  LLM 用 "你自己的笔记" 为依据回答，并带引用。
```

## 模块速览

```
+---------------------+----------------------------------------------------------+
| memex/core/         | 纯 Python，不在配置根之外做 I/O。                         |
|   config.py         |   memex.yaml 加载器 + dataclass schema。                  |
|   document.py       |   Markdown 解析、frontmatter、按标题切块。                |
|   wiki.py           |   高层 docs 操作 (add/update/rm/search/...)。             |
|   utils.py          |   Slug、sha256、token 计数、--since 解析。                |
+---------------------+----------------------------------------------------------+
| memex/backends/     | 两个存储适配器。                                          |
|   chroma_store.py   |   ChromaDB 包装 + BM25 混合打分。                         |
|   mem_store.py      |   mem0 OSS 包装，套上我们的 category 约定。               |
|   embeddings.py     |   OpenAI / sentence-transformers / chroma-default。       |
+---------------------+----------------------------------------------------------+
| memex/commands/     | Typer 子命令模块，每组一个文件。                          |
|   init/doc/mem/ctx/cursor/status/serve/client_cmd.py                           |
+---------------------+----------------------------------------------------------+
| memex/server/       | `memex serve` 暴露的 FastAPI app 与 Pydantic schemas。   |
|   api.py / schemas.py / factory.py                                             |
+---------------------+----------------------------------------------------------+
| memex/integrations/ | 旁路通道。                                                |
|   watcher.py        |   watchdog 文件监听，给 `memex doc watch` 用。            |
+---------------------+----------------------------------------------------------+
| templates/          | `memex cursor install-*` 真正吐到磁盘的文件。             |
|   hooks.json        |   sessionStart/beforeSubmitPrompt/sessionEnd 配置。       |
|   memex.mdc         |   主线程用的 Cursor 项目规则。                            |
|   agents/*.md       |   自定义 subagent (/memex-ask, /memex-archive ...)。      |
|   memex.local.yaml  |   "全离线 + Ollama" 配置模板。                            |
+---------------------+----------------------------------------------------------+
```

## memex 不是什么

- **不是托管 SaaS。** 数据全在你硬盘上。要做托管版，请基于 HTTP API 自己加一层。
- **不是通用向量数据库。** ChromaDB 是底层引擎；memex 的责任是让 markdown 和向量永远同步。
- **不是 LLM 提供商的包装。** LLM 只被 mem0 用来抽取事实（而且只在你显式 `--infer` 时才用）。wiki 和大部分 CLI 命令完全不需要 LLM。
- **不是 Obsidian / Notion 替代品。** 自带你喜欢的编辑器，memex 负责把成品索引起来。

## 接着读

- [quickstart.zh.md](quickstart.zh.md) —— 从 "git clone" 到 "第一次有用的查询"，五条命令。
- [architecture.zh.md](architecture.zh.md) —— 为什么这么设计（mem0 与 Chroma 为什么分开、为什么不上 MCP、并发模型等）。
- [cli.zh.md](cli.zh.md) —— 详尽的命令表。
