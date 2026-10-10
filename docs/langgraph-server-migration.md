# LangGraph Server 迁移评估（spike 结论）

> **决策（2026-10-10）：暂不迁移。**
> 技术上已验证可行（见下），但先用现有自建服务。重启迁移的前置条件见第七节「待官方确认」。
> 本文档是将来重启迁移的起点，不是待办。

结论先行：**技术上可行，已验证到"平台能跑通一轮并透出我们的卡片事件"**；但迁移等于把 App 契约、
Demo、文档、部署全部重做一遍，**主要收益是删掉自研管道，不是获得新能力**。建议在出现明确驱动
（要上 LangSmith Deployment / 需要时间旅行 / 不想再维护这套服务）之前不动。

## 一、已验证的事实

用 `langgraph dev`（端口 2030）实测：

```
POST /threads/{id}/runs/stream  {"stream_mode": ["custom"]}
→ HTTP 200
事件块: 1 × event: custom
载荷:  {"kind":"wallet_agent_card","response":{"kind":"react","message":"你好！我是你的多链数字货币钱包助手 👋 ..."}}
```

即：**平台完整跑完一轮我们的图，并把卡片 payload 作为 custom 事件交给了客户端**，这份 payload
现在由图产出，而不是 HTTP 层拼的。

过程中解决了三个真实拦路石：

| 拦路石 | 现象 | 处理 |
| --- | --- | --- |
| checkpointer 遮蔽 | `build_graph` 总要编译自己的 checkpointer，会盖掉平台每次 run 注入的那个 | 加 `checkpointer=False` 口子 + `USE_PLATFORM_CHECKPOINTER` 开关 |
| 同步阻塞调用 | 平台把阻塞调用当错误：`BlockingError: Blocking call to os.getcwd` | 用平台同款检测器 `blockbuster` 定位到 `Settings()` 构造（pydantic-settings 解析 env 文件走 `os.getcwd`）；工厂改 async + `asyncio.to_thread` |
| 图对象类型 | `424: Graph 'wallet_agent' is not valid` | 拆分图返回的是自研 `GraphRouter`，平台校验 `CompiledGraph`；加 `split_graphs` 覆盖，改注册单个编译图 |

顺带把装配抽成 `build_graph_dependencies(settings)` / `build_runtime_graph(settings, *, checkpointer, split_graphs)`，
服务端入口与 HTTP 入口共用同一份参数。

## 二、还没解决的（真迁移的前置）

1. **每轮钱包上下文没有落脚点**：`wallet_accounts` / `supported_networks` / `address` / `chain` 现在挂在
   我们的 turn payload 上；服务端 run 的输入是图 state，这些要挪到 run `config` / `context` 或 thread metadata。
   **这一条会改 App 的调用方式。**
2. **会话 title / 归属要进 thread metadata**，否则 `threads.search` 无法替代我们自建的会话索引。
3. **SSE 事件形状**：我们自定义的 `update` / `model` / `action_required` / `complete` 要映射到平台的
   stream mode（`values` / `updates` / `messages` / `custom`）；卡片投影已搬进图（见 `graph/emit.py`），
   但 App 与 Demo 的消费方式要跟着改。
4. **鉴权**：现在的 `require_auth` / `user_id` → 平台的 auth handler + 命名空间隔离。
5. **文档**：`app-integration.md` §4 / §7 / §11 的协议全部要重写。

## 三、自托管部署选项（命令与前置）

```bash
langgraph dockerfile ./Dockerfile.lg      # 生成生产镜像的 Dockerfile
langgraph build -t wallet-agent-lg .      # 构建镜像（默认 base: langchain/langgraph-api:3.11）
langgraph up --postgres-uri <dsn> --engine-runtime-mode combined_queue_worker
langgraph up --engine-runtime-mode distributed    # executor 与 orchestrator 分容器，可分别扩缩
langgraph deploy                                  # 或直接交给托管的 LangSmith Deployment
```

前置：

- **Postgres**（`--postgres-uri`）：平台自己管 checkpoint / 线程 / 状态；
- `distributed` 模式还需要 Redis 与队列组件；
- `langgraph dev`（inmem）**不能用于生产**，其启动日志明确写了这一点。

## 四、授权问题（待官方确认，非技术结论）

- 生成的 Dockerfile 里有这一行，说明**生产 base image 内置了授权组件**：
  `mkdir -p /api/langgraph_api /api/langgraph_runtime /api/langgraph_license ...`
- 但 **dev 用的 inmem 包没有硬门槛**：`langgraph_api/metadata.py` 里没有
  `LANGGRAPH_CLOUD_LICENSE_KEY` / `LANGSMITH_CONTROL_PLANE_API_KEY` 时只是 `return`，
  跳过向 LangChain 上报用量的后台循环（非 `lcl_` 开头的 key 则记 "air-gapped mode"）。
- CLI 的 `build` / `up` / `deploy` 都**没有** license 选项，配置走环境变量。

**所以：代码里没有强制拦截 ≠ 生产部署不需要许可。** 自托管生产是否需要购买 license 取决于
LangChain 的条款，**必须向官方确认**；上生产前还应用 `langchain/langgraph-api` 那个真实镜像
实测一遍（有没有额外校验，本仓库没有验证过）。

**对我们最实际的一点**：现在这套自建 FastAPI **完全没有这个依赖**——没有授权条款、没有 air-gapped 上报。

## 五、附：MySQL（已实现并验证）

官方 checkpointer 仍然只有 `InMemorySaver` / `SqliteSaver` / `AsyncSqliteSaver` /
`PostgresSaver` / `AsyncPostgresSaver`，**没有 MySQL**；但社区有实现，我们已接入并实测通过：

- 包：`langgraph-checkpoint-mysql`（第三方，作者 tjni，仓库 `github.com/tjni/langgraph-checkpoint-mysql`），
  异步类 `AIOMySQLSaver`，其说明写明"尽量模仿 postgres 版以跟官方保持同步"；
- 接入方式：`create_checkpointer()` 把 `mysql://` / `mysql+aiomysql://` / `mysql+pymysql://`
  统一归一为 `mysql+aiomysql://`，复用与 Postgres 相同的惰性打开逻辑（`LazyAsyncMySQLSaver`）；
- 会话索引：`SqlSessionStore` 只加了一条方言映射（`mysql://` → `mysql+aiomysql://`），
  表结构由 SQLAlchemy `create_all` 建，无需改 DDL；
- **必须安装 `cryptography`**：MySQL 8 默认 `caching_sha2_password`，aiomysql 缺了它会认证失败，
  而报错是" misleading 的 `Lost connection to MySQL server during query`"。

实测（本地 MySQL 8 容器）：

```
checkpointer class: AIOMySQLSaver
checkpoint round-trip: OK            # setup + aput + aget_tuple
session rows: [('转给朋友', True), ('查一下余额', False)]
title search: ['转给朋友']            # 标题搜索 + pinned 都在 MySQL 上工作
```

**取舍**：这条依赖是第三方的（非 LangChain 官方），需要 pin 版本并关注上游；
另外**平台自托管只吃 Postgres**，所以选 MySQL 等于放弃 LangGraph Server 这条路
（本文档其余部分的迁移评估因此只在"改用 Postgres"的前提下成立）。

## 六、建议

先不迁。若要迁，顺序是：① 钱包上下文与 thread metadata 的契约设计（需 App 配合）；
② 鉴权与事件形状映射；③ Demo / 文档重写；④ 授权确认后再定自托管还是 LangSmith Deployment。

**spike 产物对现网零影响**：`langgraph.json`、`src/wallet_agent/graph/server_app.py`、
`langgraph-cli[inmem]`（dev 依赖）都是旁路；对主路径的改动只有三处，都可单独回退——
`build_graph` 的 `checkpointer=False` 语义、`USE_PLATFORM_CHECKPOINTER` 开关（默认关）、
`build_runtime_graph` 的 `split_graphs` 覆盖（默认跟随设置）。


## 七、待官方确认（重启迁移的前置）

**决策记录**：技术上已证明可行，但收益集中在"时间旅行 + 平台级并发/恢复语义 + 少维护自研管道"，
而代价是 App 契约 / Demo / 文档 / 部署全部重做，外加授权这条非技术变量。在授权未确认前不动。

发给 LangChain 商务或技术支持时，建议逐条确认：

1. **自托管生产是否需要购买许可？** 生产镜像 `langchain/langgraph-api` 里内置了
   `/api/langgraph_license` 组件（见第四节证据），但 dev 包没有硬门槛——需要官方给明确口径。
2. **价格与计费口径**：按什么计费（节点执行数？运行数？席位？）、有没有免费额度、
   测试环境与生产环境是否分开计费。
3. **生产镜像是否有运行时强制校验**：拿不到合法 key 时是仅跳过用量上报，还是拒绝启动。
   本仓库**没有**用真实镜像验证过这一点。
4. **air-gapped 模式的功能边界**：非 `lcl_` key 会进 air-gapped 模式，功能上是否有任何缺失。
5. **自托管的支持范围**：`distributed` 模式（executor / orchestrator 分容器）是否在支持范围内、
   能否按副本独立扩缩、有无最低版本要求。
6. **后端限制**：平台只支持 Postgres（无 MySQL）；若我们环境以 MySQL 为准，是否有替代方案。
7. **迁移支持**：从自建 FastAPI 迁到平台，有没有官方的迁移路径或兼容层建议。

**若上述第 1、3 条结论为"需要许可且强制校验"**，则本项目的性价比不成立，建议继续自建，
只按需补单项能力（例如时间旅行可以在自建服务上单独实现，见第二节）。

**若授权可行**，则按第六节的顺序推进，并优先走 B 路线（我们的 FastAPI 作为适配层，
平台在内部做 graph-host），先验证再谈 App 契约变更。
