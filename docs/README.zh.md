# memex 文档

按你的目的挑文档读。每一页都有英文双胞胎（去掉 `.zh` 后缀）。

| 页面 | 内容 |
|---|---|
| [overview.zh.md](overview.zh.md) | memex 是什么、双层记忆模型、整体数据流 |
| [quickstart.zh.md](quickstart.zh.md) | 5 分钟从零到"第一次有用的查询" |
| [cli.zh.md](cli.zh.md) | 每个 CLI 命令、每个参数、可复制粘贴的例子 |
| [api.zh.md](api.zh.md) | HTTP / REST 接口：鉴权、端点、Schema、错误格式 |
| [docker.zh.md](docker.zh.md) | 容器构建（离线模型预装）、compose、持久化、运维 |
| [cursor.zh.md](cursor.zh.md) | Cursor 三种接入方式：hooks、project rule、subagents |
| [config.zh.md](config.zh.md) | `memex.yaml` 字段逐项说明、profile、环境变量 |
| [architecture.zh.md](architecture.zh.md) | 为什么这么设计。权衡与扩展点 |

## 阅读路径

```
+-------------------+        +--------------------+        +-----------------+
| 想先试一下        | -----> | quickstart.zh.md   | -----> | cli.zh.md       |
+-------------------+        +--------------------+        +-----------------+

+-------------------+        +--------------------+        +-----------------+
| 想部署到服务器     | -----> | docker.zh.md       | -----> | config.zh.md    |
+-------------------+        +--------------------+        +-----------------+
                                                                   |
                                                                   v
                                                           +-----------------+
                                                           | api.zh.md       |
                                                           +-----------------+

+-------------------+        +--------------------+        +-----------------+
| 想接入 Cursor     | -----> | cursor.zh.md       | -----> | overview.zh.md  |
+-------------------+        +--------------------+        +-----------------+

+-------------------+        +--------------------+        +-----------------+
| 想扩展或审计       | -----> | architecture.zh.md | -----> | 源码 memex/     |
+-------------------+        +--------------------+        +-----------------+
```

## 约定

- 文档里所有的图都是纯 ASCII（`+ - | -> <- <-> v ^`），不用 Unicode 箭头或制表符，可以放心粘到任何地方。
- 标 `bash` 的代码块是 shell 可直接跑的；`yaml`、`json`、`python` 反映文件实际内容。
- `<占位符>` 表示"换成你自己的值"；`$ENV_VAR` 表示真实环境变量。

## 本目录之外

- [`../README.md`](../README.md) —— 项目顶层介绍（电梯演讲）。
- [`../DOCKER.md`](../DOCKER.md) —— 运维向的 Docker 深度指南（比 [docker.zh.md](docker.zh.md) 更细）。
- `../templates/` —— CLI 真正打包的 hooks/rule/agent 模板。
- `../scripts/docker-build-test.sh` —— 一键构建 + 端到端测试脚本。
