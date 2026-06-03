# Docker 部署

这是简洁版。完整的运维手册（多机部署、故障排查矩阵、Ollama 边车配线、完整端点表）在 [`../DOCKER.md`](../DOCKER.md)。

## 一图看完

```
+-------------------+         +-------------------------------------------+
|  调用方 (LLM、     |  HTTP   |  memex 容器                                |
|  Cursor agent、   | ------> |  uvicorn :7963                            |
|  CI 机器人、curl)  |         |                                           |
+-------------------+         |  /opt/memex/models/   <-- 镜像构建时烤进   |
                              |     chroma/onnx_models/...                |
                              |     hf/hub/...sentence-transformers...    |
                              |                                           |
                              |  /data/             <-- 宿主机卷           |
                              |     docs/                                 |
                              |     memex.yaml                            |
                              |     .cache/chroma/   (你的向量)            |
                              |     .cache/mem0/     (qdrant + history)   |
                              +-------------------------------------------+
                                              |
                                              v
                                        host: ./data/
```

两样东西在动：

- **镜像**（不可变）：代码 + venv + 离线模型。只有 build 内容变化时才重建。
- **卷**（宿主机 `./data`）：通过运行中的容器产生的所有东西。

## 构建

```bash
cd memex/
cp .env.example .env                          # 设 MEMEX_API_TOKEN、OPENAI_API_KEY 等
docker compose build                          # 首次 ~5-10 分钟
```

### 镜像里都烤进了什么

设计上**只有一个变体**——运行时可能加载的所有模型都被打进镜像，容器启动后**永不联网**拉模型文件。镜像大小（~1.5-2 GB）是为此付出的、明确接受的代价。

| Bundle | 用途 |
|---|---|
| CPU-only PyTorch（来自 PyTorch CPU wheel index）                                              | sentence-transformers、mem0 HF embedder |
| sentence-transformers 包                                                                       | mem0 的 HuggingFace embedder |
| ChromaDB ONNX `all-MiniLM-L6-v2`，位于 `/opt/memex/models/chroma/onnx_models/`                 | wiki 向量层（`embedder.provider: chroma-default` 时） |
| HF `sentence-transformers/all-MiniLM-L6-v2`（完整 snapshot），位于 `/opt/memex/models/hf/`     | mem0 的 HuggingFace embedder |
| fastembed `Qdrant/bm25`，位于 `/opt/memex/models/fastembed/`                                    | mem0 / qdrant 的 BM25 关键词检索 |
| spaCy `en_core_web_sm`（`python -m spacy download` 安装到 `/opt/venv`）                         | mem0 词形还原 / 实体抽取（`mem0ai[nlp]`） |
| tiktoken `cl100k_base` BPE 文件，位于 `/opt/memex/models/tiktoken/`                              | memex 分块 / token 计数 |

### 增量构建

Dockerfile 故意拆成三段，**一次普通的源码改动重建只要 ~40 秒，而不是 25 分钟**：

| 阶段 | 做什么                                          | 缓存命中键                | 何时失效            |
|------|------------------------------------------------|---------------------------|---------------------|
| A    | 用 stub 版 `memex/__init__.py` 让 pip 装齐所有依赖（不碰真源码）| `pyproject.toml`、`README.md` | 改依赖时           |
| B    | 预热所有模型（HF MiniLM、spaCy、ChromaDB ONNX、fastembed、tiktoken）| 同 A                     | 改依赖时           |
| C    | `COPY memex/` + `templates/` + `pip install --no-deps .` | `memex/`、`templates/`    | 每次改源码（~5s）  |

BuildKit cache mount 还把 pip 的 `~/.cache/pip` 和 apt 的 `/var/cache/apt` 在
不同构建之间持久化，即便 `pyproject.toml` 改动触发全量重建，下载步骤也能复用缓存。

## 运行

```bash
docker compose up -d
curl -fsS http://localhost:7963/healthz       # 存活探针
open http://localhost:7963/docs               # OpenAPI / Swagger UI
```

默认对外端口 `7963`，改 `.env` 里的 `MEMEX_PORT`：

```bash
MEMEX_PORT=18000 docker compose up -d
```

## 配置

如果卷里没有 `memex.yaml`，容器会用 `openai` profile 启；切到 local profile 直接改 `./data/memex.yaml`：

```yaml
embedder:
  provider: chroma-default
  model: all-MiniLM-L6-v2
llm:
  provider: openai
  model: qwen3:4b
  base_url: http://10.242.29.48:11434/v1
  api_key: no-key
```

或者在容器里重置：

```bash
docker compose exec memex memex init --profile local --force
```

## 持久化

所有需要在重启后保留的东西都放在宿主机 `./data`：

```
./data/
   docs/                    <-- 你的 markdown wiki（.cache/ 要 gitignore）
   memex.yaml               <-- 配置
   .cache/
      chroma/               <-- 向量索引（MB-GB 级）
      mem0/                 <-- qdrant + history.db
      history/              <-- tombstones、审计日志
```

备份：

```bash
docker compose exec memex memex backup -o /data/snap-$(date +%F).tar.gz
# 或在宿主机上：
tar czf memex-backup.tar.gz -C ./data .
```

恢复（到一个新容器）：

```bash
docker compose down
rm -rf ./data && mkdir ./data && tar xzf memex-backup.tar.gz -C ./data
docker compose up -d
```

## 鉴权

```bash
# .env
MEMEX_API_TOKEN=$(openssl rand -hex 32)
```

设了之后除 `/healthz` 外的所有端点都强制要求 `Authorization: Bearer <token>`。容器自己的 healthcheck 不受影响（它打的就是 `/healthz`）。

## 一键构建 + 端到端测试

一个脚本。构建 -> 镜像内省 -> 启动 -> 跑全套 API -> 重启 -> 验证持久化：

```bash
bash scripts/docker-build-test.sh                       # 完整构建 + 测试
FAST=1 bash scripts/docker-build-test.sh               # 镜像存在则跳过构建
FAST=1 bash scripts/docker-build-test.sh                # 镜像存在时跳过 rebuild
```

最后会打印 `PASS / FAIL` 计数。脚本本身就是完整 checklist。

## 日常运维

```bash
# 看日志
docker compose logs -f memex

# 进容器
docker compose exec memex bash

# 在容器里跑本地 CLI（作用于同一份 /data）
docker compose exec memex memex status
docker compose exec memex memex doc ls
docker compose exec memex memex doc reindex --changed

# stop / start / restart
docker compose stop
docker compose start
docker compose restart memex

# 关掉容器（保留 ./data）
docker compose down

# 关掉容器 + 删数据（破坏性）
docker compose down -v && rm -rf ./data
```

## 把 Cursor subagent 接到 Docker 部署

如果你的 KB 在 Docker 里（开发机和笔记本共享，或服务器上加 token），把本地 Cursor 指过去：

```bash
# 在跑 Cursor 的开发机上：
pipx install <memex-repo-路径>            # 或在 repo 里 `pip install -e .`

# 指向部署
export MEMEX_API_URL=http://<host>:7963
export MEMEX_API_TOKEN=...                # 如果设了的话

# 验证
memex client status

# 然后要么把 memex 别名成 memex client：
alias memex='memex client'
# ... 要么在每个 ~/.cursor/agents/memex-*.md 顶部加一行：
#     "本 agent 里所有 `memex ...` 调用都通过 `memex client ...` 走。"
```

## 边车 Ollama（可选）

`docker-compose.yml` 自带一段注释掉的 Ollama 服务。打开后 `memex` 服务就能在 compose 网络里以 `http://ollama:11434/v1` 访问它——不需要在宿主机暴露端口。

```yaml
# 在 docker-compose.yml 里取消注释
ollama:
  image: ollama/ollama:latest
  container_name: ollama
  restart: unless-stopped
  ports:
    - "11434:11434"
  volumes:
    - ./ollama:/root/.ollama
```

然后在 `./data/memex.yaml` 里：

```yaml
llm:
  provider: openai
  model: qwen3:4b
  base_url: http://ollama:11434/v1
  api_key: no-key
```

## 排错

完整表在 [`../DOCKER.md`](../DOCKER.md) 末尾。两个最常见的坑：

- **`/data` permission denied** —— 容器里用户的 uid 是 1000。如果宿主机目录属主不是 1000，跑一次 `chown -R 1000:1000 ./data`，或在 build 时用 `--build-arg` 改 uid。
- **某个 HF 模型报 `OSError: ... offline mode`** —— 你请求的不是镜像里烤进去的那个 HF 模型。要么在 Dockerfile 的预热步骤里也 `snapshot_download` 这个 repo，要么运行时加 `-e HF_HUB_OFFLINE=0 -e TRANSFORMERS_OFFLINE=0` 并给容器网络（首次会从网络拉）。
