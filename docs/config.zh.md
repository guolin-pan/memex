# 配置

所有控制 memex 行为的东西在**两个地方**：

1. YAML 文件 `<root>/memex.yaml`。
2. 少数几个环境变量（不该写进 YAML 的：密钥、服务端绑定地址、`<root>` 本身的位置）。

本页逐字段过一遍。

## YAML 在哪

```
<root>/memex.yaml
```

`<root>` 默认 `~/memex`。覆盖：

- CLI 参数：`memex -R /path/to/root <command>` —— 一次性。
- 环境变量：`export MEMEX_ROOT=/path/to/root` —— shell 内持久。
- 都给：CLI 参数胜出。

第一次 `memex init` 会按两个模板之一写文件：

- `--profile openai`（默认）-> [`write_default_config` 在 config.py 里产生的默认 YAML](../memex/core/config.py)。
- `--profile local` -> [`templates/memex.local.yaml`](../templates/memex.local.yaml)（全离线 + OpenAI 兼容 LLM）。

## YAML Schema 逐字段

完整语法（在 [`memex/core/config.py`](../memex/core/config.py) 里）：

```yaml
user_id: <字符串>

embedder:
  provider: <openai | sentence-transformers | chroma-default>
  model:    <模型名>
  dims:     <int>           # 可选；已知模型会自动检测
  base_url: <url>           # 可选；OpenAI 兼容端点覆盖
  api_key:  <字符串>         # 可选；上面端点的 api_key

llm:
  provider:    <openai | ollama>
  model:       <模型名>
  temperature: <float>
  base_url:    <url>        # 可选；OpenAI 兼容端点覆盖
  api_key:     <字符串>      # 可选；上面端点的 api_key

chunking:
  target_tokens:     <int>
  overlap_tokens:    <int>
  min_chunk_tokens:  <int>
  split_by_headings: [h2, h3]   # 在哪些级别的标题切分

search:
  top_k_docs:    <int>
  top_k_mems:    <int>
  hybrid_alpha:  <float 0.0..1.0>   # 1.0=纯向量；0.0=纯 BM25
  min_score:     <float>

ctx:
  budget_tokens:    <int>
  include_profile:  <bool>
  include_memories: <bool>
  include_docs:     <bool>
```

缺的段会回落到包默认值；段里缺的字段也会回落到 dataclass 默认值。所以可以写一个只覆盖你关心的字段的最小 yaml：

```yaml
# 最小有效 memex.yaml
user_id: alice
llm:
  base_url: http://10.242.29.48:11434/v1
  model:    qwen3:4b
  api_key:  no-key
```

## 字段释义

### `user_id`
mem0 user 标识符。所有记忆都挂在这个 id 下。同一台机器上两个不同的 `user_id` 等于两个独立的个人记忆空间；wiki 是共享的。

### `embedder.*`

```
+------------------------+------------------------+----------------------------------+
| provider               | 默认模型                | 何时用                            |
+------------------------+------------------------+----------------------------------+
| openai                 | text-embedding-3-small | 你有 OPENAI_API_KEY，或者一个    |
|                        | (1536 dims)            | OpenAI 兼容端点（base_url +      |
|                        |                        | api_key）                       |
+------------------------+------------------------+----------------------------------+
| sentence-transformers  | all-MiniLM-L6-v2       | 你装了 [local] extra（~800 MB   |
|                        | (384 dims)             | torch）                         |
+------------------------+------------------------+----------------------------------+
| chroma-default         | all-MiniLM-L6-v2       | 你要离线嵌入且不想要 torch       |
|                        | (384 dims, ONNX 运行时) | （ChromaDB 自带 ONNX；~80 MB）  |
+------------------------+------------------------+----------------------------------+
```

`dims` 对已知模型会自动检测（见 [`_expected_embedding_dims()` in mem_store.py](../memex/backends/mem_store.py)）。只有用启发式不认识的模型才需要手写。

`base_url` 和 `api_key` 只对 `provider: openai` 生效。适合任何能讲 OpenAI `/v1` 协议的自托管网关——Ollama、vLLM、LM Studio、LiteLLM、OpenAI proxy 等。Ollama embedder 例子：

```yaml
embedder:
  provider: openai
  model:    qwen3-embedding:latest
  base_url: http://localhost:11434/v1
  api_key:  no-key
```

### `llm.*`

LLM 只被 mem0 用来抽取事实（即 `mem add --infer` 和 `mem learn`）。逐字 `mem add` 和 wiki 层都不调 LLM。

```
+----------+----------------+-----------------------------------------------+
| provider | 默认模型        | 备注                                          |
+----------+----------------+-----------------------------------------------+
| openai   | gpt-4o-mini    | 也覆盖任意 OpenAI 兼容端点（base_url +        |
|          |                | api_key：Ollama /v1、vLLM 等）                |
+----------+----------------+-----------------------------------------------+
| ollama   | (按你的部署)    | 给那些**不**讲 OpenAI v1 方言、需要走 mem0   |
|          |                | 原生 Ollama 适配器的端点用；这里的 base_url   |
|          |                | 会以 ollama_base_url 落到 mem0 配置          |
+----------+----------------+-----------------------------------------------+
```

绝大多数本地 LLM 场景，**推荐用 `provider: openai` + Ollama 的 base_url**——走经过充分测试的 OpenAI client，错误也清楚。

### `chunking.*`

控制每篇 markdown 在嵌入前怎么切。

| 字段 | 默认 | 作用 |
|---|---|---|
| `target_tokens` | 800 | 每块 token 数上限（tiktoken 计数）。 |
| `overlap_tokens` | 100 | 硬切时在边界重复这么多 token，避免上下文断裂。 |
| `min_chunk_tokens` | 50 | 块小于这个数不 flush，除非到文件末尾。 |
| `split_by_headings` | `[h2, h3]` | 哪些级别的标题会产生新块。只 `h2` = 块更大；`[h2, h3, h4]` = 更细。 |

代码 fence **永远是原子的**——绝不会在 `\`\`\`` 块中间切，无论上述如何设置。

改了 chunking？记得 `memex doc reindex --all`。

### `search.*`

| 字段 | 默认 | 作用 |
|---|---|---|
| `top_k_docs` | 5 | `memex doc search` 和 `memex ctx` 的默认 k。 |
| `top_k_mems` | 5 | `memex mem search` 和 `memex ctx` 的默认 k。 |
| `hybrid_alpha` | 0.5 | 向量 vs BM25 的权重。1.0 = 纯向量，0.0 = 纯关键词。 |
| `min_score` | 0.0 | 混合后分数低于此值的命中丢弃。 |

### `ctx.*`

`memex ctx` 的默认行为。Hooks 通过命令行参数覆盖。

| 字段 | 默认 | 作用 |
|---|---|---|
| `budget_tokens` | 2000 | 合成上下文块的总 token 上限。 |
| `include_profile` | true | 是否包含 "About the user" 段。 |
| `include_memories` | true | 是否包含相关记忆段（query 非空才有意义）。 |
| `include_docs` | true | 是否包含 wiki 搜索段（query 非空才有意义）。 |

三段按比例分预算（profile : memories : docs = 1 : 2 : 3），各自按 tiktoken 截断到自己的配额。

## 环境变量

memex 自身只读这几个；其他（如 `OPENAI_API_KEY` 给 OpenAI SDK、`HF_HOME` 给 HuggingFace）都是底层库读的。

```
+----------------------+------------------+-----------------------------------------+
| 名字                 | 谁读它            | 作用                                    |
+----------------------+------------------+-----------------------------------------+
| MEMEX_ROOT           | CLI + server     | 覆盖 <root> 路径；等价于 CLI -R 参数。  |
| MEMEX_API_URL        | memex client     | 要访问的远端 API。                       |
|                      |                  | 默认: http://127.0.0.1:8000             |
| MEMEX_API_TOKEN      | memex serve +    | server 端设了之后，除 /healthz 外所有   |
|                      | memex client     | 端点都要求 Authorization: Bearer <tok>。|
|                      |                  | client 端设了会自动带上。                |
| MEM0_TELEMETRY       | mem0（memex 默认 | MemStore 第一次构建时默认设成 "False"，  |
|                      | 设它）           | 让 mem0 不要为遥测再开一个独立 qdrant   |
|                      |                  | collection。要重新开启就显式设 "True"。 |
+----------------------+------------------+-----------------------------------------+
```

会影响 memex 的常见标准环境变量：

```
+-----------------------+------------------------------------------------------+
| OPENAI_API_KEY        | llm 或 embedder 用 provider: openai 时（云）需要；   |
|                       | base_url 指向本地 LLM 时会被忽略                     |
+-----------------------+------------------------------------------------------+
| HF_HOME               | HuggingFace 模型缓存目录；Docker 镜像里设成          |
|                       | /opt/memex/models/hf，让模型住镜像而不是数据卷       |
+-----------------------+------------------------------------------------------+
| TRANSFORMERS_OFFLINE  | Docker 镜像里设为 1，强制 HF 栈只用烤进去的缓存      |
| HF_HUB_OFFLINE        | （不联网）                                           |
| HF_DATASETS_OFFLINE   |                                                      |
+-----------------------+------------------------------------------------------+
| EDITOR                | `memex doc edit` 和 `memex doc add --open` 用       |
+-----------------------+------------------------------------------------------+
```

## 两个 profile 并排对比

```
+----------------------------------------------------+-----------------------------------------------+
| openai (云)                                         | local (离线 emb + OpenAI 兼容 LLM)            |
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
| chunking / search / ctx：默认                      | chunking / search / ctx：默认                 |
+----------------------------------------------------+-----------------------------------------------+
| 需要 OPENAI_API_KEY                                | 不需要任何外部凭据                              |
+----------------------------------------------------+-----------------------------------------------+
```

## 常见配方

### 用 OpenAI 云，但 embedder 走离线

```yaml
embedder:
  provider: chroma-default
  model:    all-MiniLM-L6-v2
llm:
  provider: openai
  model:    gpt-4o-mini
```

`OPENAI_API_KEY` 仍要在环境里；embedder 在本地跑；mem0 的事实抽取走云 LLM。

### 自托管 vLLM 端点

```yaml
llm:
  provider: openai
  model:    Qwen/Qwen2.5-7B-Instruct
  base_url: http://vllm.internal:8000/v1
  api_key:  <vllm-token>
```

### 同一台机器上两个 memex 根

```bash
# 工作
MEMEX_ROOT=~/work-memex memex init -u me-at-work
# 个人
MEMEX_ROOT=~/personal-memex memex init -u me-personal
```

然后给每个 profile 一个 alias：

```bash
alias work-memex='MEMEX_ROOT=~/work-memex memex'
alias home-memex='MEMEX_ROOT=~/personal-memex memex'
```

### 调整检索默认值

```yaml
search:
  top_k_docs: 8         # 召回更广
  hybrid_alpha: 0.7     # 更偏向向量
ctx:
  budget_tokens: 3000   # 上下文块更大
```

## 配置生效时机

CLI 每次调用都重读 `memex.yaml`。服务（`memex serve`）只在**启动时**读一次并缓存；改完配置要重启服务（或 `docker compose restart memex`）。
