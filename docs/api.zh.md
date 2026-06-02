# HTTP API 手册

`memex serve` 把和 CLI 一样的能力通过 HTTP 暴露。适合 agent 和数据不在同一台机器的场景（容器里的 LLM、宿主机上的 memex；或者一个团队共享一个个人 KB）。

## 启动

```bash
memex serve --host 0.0.0.0 --port 8000
```

OpenAPI / Swagger UI 在 `/docs`；原始 schema 在 `/openapi.json`。

## 鉴权

可选 Bearer token，靠环境变量：

```bash
MEMEX_API_TOKEN=$(openssl rand -hex 32) memex serve
```

设了之后，**除 `/healthz` 外所有端点都要求** `Authorization: Bearer <token>`。不设则 API 开放（在 `127.0.0.1` 和私有 docker 网络里合理；暴露到公网就别这么干）。

## 架构

```
+--------------------+        HTTP/1.1         +---------------------------+
|  调用方             | ------- 请求 --------> |  uvicorn :8000            |
|  (memex client、   |                         |  FastAPI app              |
|   curl、httpx、    | <----- 响应 ---------- |  build_app(root) 构建     |
|   任何 HTTP 客户端) |                         +-------------+-------------+
+--------------------+                                       |
                                                             v
                                                +-------------------------+
                                                |  每进程一份 Wiki +      |
                                                |  一份 MemStore          |
                                                |  （所有请求共用）        |
                                                +-------------------------+
                                                             |
                                            +----------------+----------------+
                                            v                                 v
                                  +-----------------+               +-----------------+
                                  |  ChromaDB       |               |  mem0 OSS       |
                                  +-----------------+               +-----------------+
```

`Wiki` 和 `MemStore` 都是懒构建，每个进程一份，所有请求共享——qdrant 的文件锁只取一次，Chroma 的内存缓存也保持热。

## 端点总表

```
+--------+-------------------------+---------------------------------------------+
| method | path                    | 作用                                         |
+--------+-------------------------+---------------------------------------------+
| GET    | /                       | 服务横幅：name、version、root、auth_required |
| GET    | /healthz                | 存活探针（始终开放，不鉴权）                  |
| GET    | /status                 | 文档/chunk 数、磁盘用量、embedder、llm、版本 |
+--------+-------------------------+---------------------------------------------+
| POST   | /doc/add                | 加一篇 markdown 文档                          |
| GET    | /doc                    | 列文档 (?tag=, ?since=)                      |
| GET    | /doc/search             | 混合检索 (?q=, ?k=, ?tag=, ?since=)          |
| GET    | /doc/{ident}            | 按 id / slug / 路径展示一篇                   |
| DELETE | /doc/{ident}            | 删除一篇 (?keep_file=true 保留磁盘文件)       |
| POST   | /doc/reindex            | 重新索引 (?all=true 强制全量)                |
+--------+-------------------------+---------------------------------------------+
| POST   | /mem/add                | 加一条记忆（默认逐字；?infer=true 走 LLM）    |
| GET    | /mem                    | 列记忆 (?category=)                          |
| GET    | /mem/profile            | 渲染的 'About the user' 块                   |
| GET    | /mem/search             | 语义搜索 (?q=, ?k=, ?category=)              |
| GET    | /mem/{mem_id}           | 按 id 展示一条                                |
| DELETE | /mem/{mem_id}           | 按 id 删；id='all' 清空                       |
+--------+-------------------------+---------------------------------------------+
| POST   | /ctx                    | 构建统一上下文块                              |
+--------+-------------------------+---------------------------------------------+
| GET    | /docs                   | OpenAPI / Swagger UI                         |
| GET    | /openapi.json           | 机器可读 schema                              |
+--------+-------------------------+---------------------------------------------+
```

## 约定

- JSON 进，JSON 出。POST 请求带 `Content-Type: application/json`。
- 错误响应：`{ "error": "<异常类型>", "detail": "<消息>" }`。HTTP 码遵循标准（`400` 校验失败、`401` token 错、`404` 找不到、`500` 内部错）。
- 所有时间戳都是 ISO 8601 UTC。

## 例子

### Healthz + 横幅

```bash
curl -fsS http://localhost:8000/healthz
# -> {"ok":true}

curl -fsS http://localhost:8000/
# -> {"name":"memex","version":"0.1.0","root":"/data","docs":"/docs",
#     "openapi":"/openapi.json","auth_required":true}
```

### Status（需要 bearer）

```bash
TOKEN=...                       # 来自 $MEMEX_API_TOKEN

curl -fsS http://localhost:8000/status -H "Authorization: Bearer $TOKEN"
# -> { "root":"/data", "user_id":"alice",
#      "docs_count": 23, "chunks_count": 142,
#      "embedder": "chroma-default:all-MiniLM-L6-v2",
#      "llm":      "openai:qwen3:4b",
#      "docs_dir_bytes": 1048576, ... ,
#      "version":"0.1.0" }
```

### 加文档

```bash
curl -fsS -X POST http://localhost:8000/doc/add \
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

### 搜索

```bash
curl -fsS -G http://localhost:8000/doc/search \
  -H "Authorization: Bearer $TOKEN" \
  --data-urlencode 'q=postgres analytic memory' \
  --data-urlencode 'k=3' \
  --data-urlencode 'tag=db'
```

响应格式：

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

### 构建上下文块（Cursor hooks 调的就是它）

```bash
curl -fsS -X POST http://localhost:8000/ctx \
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

### 加记忆（逐字）

```bash
curl -fsS -X POST http://localhost:8000/mem/add \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
        "text":     "Prefers pgvector for hybrid search",
        "category": "pref"
      }'
# -> { "ids": ["abc-123-..."] }
```

要让 mem0 用 LLM 做事实抽取（拆 / 合 / 去重），加 `"infer": true`。默认 `false`——你给什么文本就存什么文本。

### 渲染 profile 块

```bash
curl -fsS http://localhost:8000/mem/profile -H "Authorization: Bearer $TOKEN"
# -> { "block": "## About the user\n\n- (profile) ...\n- (pref) ...\n",
#      "count": 7 }
```

### 重新索引（管理操作）

```bash
curl -fsS -X POST 'http://localhost:8000/doc/reindex?all=true' -H "Authorization: Bearer $TOKEN"
# -> { "added": [...], "updated": [...], "skipped": [...], "deleted": [] }
```

## Pydantic Schema（线上格式）

源头是 [`memex/server/schemas.py`](../memex/server/schemas.py)；以代码为准。

### DocAddRequest

```json
{
  "body":   "...",          // 必填，原始 markdown
  "title":  "...",          // 可选；从 H1 / 文件名推断
  "tags":   ["..."],        // 可选
  "subdir": "inbox"         // 默认 "inbox"
}
```

### DocOut（add/show/list 的返回）

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

### DocSearchHitOut（/doc/search 中的每一条）

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
// 请求
{
  "text":     "...",        // 必填
  "category": "fact",       // profile pref project decision learning fact
  "tags":     ["..."],
  "infer":    false         // 默认 false（逐字）；true = LLM 抽取
}

// add 响应
{ "ids": ["..."] }

// 单条形状（search/list/show）
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
// 请求
{
  "query":            "",
  "budget":           null,    // null -> 用服务端 memex.yaml 的默认值
  "top_k_docs":       null,
  "top_k_mems":       null,
  "include_profile":  true,
  "include_memories": true,
  "include_docs":     true
}

// 响应
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

## 从 Cursor subagent 调用

最简单的情况是 agent shell 出去调 `memex client`：

```bash
export MEMEX_API_URL=https://memex.internal.example
export MEMEX_API_TOKEN=...
memex client ctx "$CURSOR_USER_PROMPT" --write /tmp/ctx.md --budget 2000
```

对 agent 推荐用 `memex client` 而非 raw curl，理由：

1. 只暴露安全、网络友好的操作。
2. 错误输出是 `error 401: ...` 这种一行式，而不是巨大的 Python traceback。
3. 输出格式和本地 CLI 一致，已有的解析 `memex` 输出的 prompt 不用改。

完整"Cursor subagent 在宿主机 + memex 在 Docker"的配方见 [docker.zh.md](docker.zh.md)。
