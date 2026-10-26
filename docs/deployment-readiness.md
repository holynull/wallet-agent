# 生产就绪差距清单

对照当前的 `docker-compose.yml` / `Dockerfile` 与测试服务器（`ssh catwallet-test`）的实际部署，
记录"能不能照搬到生产"以及还缺什么。**结论：拓扑可以照搬，Compose 文件本身不是生产配置。**

## 可以直接复用的部分

- 四服务拓扑（API / Demo / Postgres / Redis）+ healthcheck + `restart: unless-stopped` + 数据卷持久化。
- **多实例基础已经设计好**：Postgres 存 LangGraph checkpoint 与会话投影，Redis Streams 存跨实例
  run events，Redis 租约串行化同一 `conversation_id` 的一轮；SSE 每帧带 `id`，支持
  `Last-Event-ID` 续传。换成托管 Postgres/Redis 后 API 可直接水平扩副本
  （见 README「Multi-instance persistence and SSE recovery」）。
- `.env` 与镜像分离；`NEXT_PUBLIC_AGENT_API_URL` / `CORS_ALLOW_ORIGINS` 已按"浏览器可达"配置。

## 差距清单

### P0（上线前必须）

| 项 | 现状 | 需要做到 |
| --- | --- | --- |
| 凭据 | `POSTGRES_PASSWORD=wallet_agent`、Redis 无密码、`.env` 明文放在服务器上并手工维护（测试服务器上还留有 `.env.bak.20261010`） | 接密钥管理（Secrets Manager / SSM Parameter Store），凭据不入库不进镜像；上线前轮换 |
| 鉴权 | `require_auth` 是可配开关，测试环境未强制 | 生产强制开启；确认所有 `/v1/*` 与 `/debug/*` 都受保护 |
| 调试面 | 新增 `/v1/agent/runs/{run_id}` 巡检（owner-only / `ADMIN_TOKEN`），但 `/docs`、`/debug/metrics` 仍对外可访问 | 关闭或统一加访问控制；生产只通过 HTTPS 暴露巡检接口 |
| CORS | `CORS_ALLOW_ORIGINS` 写的是 IP | 收敛到真实域名，禁止通配 |
| 传输 | `:8000` / `:3000` 明文直连 | ALB/Nginx + TLS + HSTS，只暴露入口 |
| Demo | 与 API 一起部署 | **生产不对公网暴露 `demo-next`**（联调控制台会展示调试数据） |

### P1（上线后很快会需要）

| 项 | 现状 | 需要做到 |
| --- | --- | --- |
| 数据 | Postgres/Redis 单容器 + 本地卷，无备份 | 托管实例（RDS/ElastiCache）或至少自动备份 + 恢复演练，并确认 checkpointer 表也在备份范围内 |
| Schema | checkpoint 表由 `setup()` 创建，**没有版本化迁移** | 迁移流程 + 发布前检查，避免滚动发布时 schema 不一致 |
| 运行时 | Dockerfile 单阶段、**以 root 运行**、无 workers、无资源限制 | 非 root 用户；uvicorn/gunicorn 多 worker；compose 里加 CPU/内存限额 |
| 发布 | ssh + `git pull` + 手工 `./scripts/redeploy.sh` | CI/CD：构建镜像 → 推 registry → 滚动发布；**必须能一键回滚**（现在回滚只能 `git reset --hard`，属破坏性操作） |
| 可观测性 | 只有进程内 `/debug/metrics` + 容器日志 | 结构化日志聚合 + 指标导出（Prometheus/OTel）。注意 `/debug/metrics` **是单副本进程内的，多实例下不能当全局指标** |

### P2（按需）

- LangSmith tracing（LangGraph SDK 支持，目前未接），用于排查 ReAct 循环与工具耗时。
- 限流与配额（对外接口目前没有）。
- 显式声明 `durability`（`sync` / `async` / `exit`）：当前是默认的每步 checkpoint，
  建议对"未签名交易、广播回执"这类步骤明确取舍并写进文档。
- 长期记忆（LangGraph `BaseStore`）目前未使用，也没有预留；有跨会话个性化需求时再引入。

## 建议的上线顺序

1. 关掉调试面与鉴权缺口、收敛 CORS、只暴露入口（P0）。
2. 数据换托管 + 备份 + 恢复演练（P1）。
3. 镜像非 root + 资源限额 + 只部署 API，Demo 留在测试环境（P1）。
4. 上 CI/CD 与回滚（P1）。
5. 补可观测性，再考虑限流与 tracing（P1/P2）。

## 与 LangGraph 官方部署形态的关系

当前是**自建 FastAPI 服务**（没有 `langgraph.json`，也没跑 LangGraph Server）。
这带来一部分自研语义：同一 thread 的并发策略、断连语义、`durability`、可恢复流。
我们部分实现了（Redis 租约 ≈ 会话级串行化，`Last-Event-ID` ≈ 可恢复流），
但边界条件由自己负责。若将来切到 LangGraph Server，`/threads/*`、`/runs/stream`
（含 `stream_resumable`、`multitask_strategy`、`on_disconnect`）与 `threads.search`
可以直接替代本仓库的对应自研部分，前提是把会话 title/归属写进 thread metadata。
