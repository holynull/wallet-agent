# Agent 运行与对话巡检

用于在本地、测试环境和生产环境快速查看某一轮 Agent 执行：状态、意图、路由、
工具时间线、模型调用、错误、卡片类型、关联会话和事件历史。它只读、不写业务状态，
也不要求登录数据库或 Redis。

## 安全模型

- `GET /v1/agent/runs/{run_id}` 默认只允许 run 的 owner 读取；跨用户返回 `404`，
  不暴露 run 是否存在。
- 配置 `ADMIN_TOKEN` 后，运维可以用该 token 跨用户读取 run、conversation 和 session。
  `ADMIN_TOKEN` 不是普通 App 用户 token，不要放进 App 构建产物或客户端日志。
- 使用 `ADMIN_TOKEN` 查看 conversation / session 时，同时传目标 `user_id` 作为筛选条件。
- 响应会递归移除 Authorization/Cookie、API key、私钥/助记词、签名、raw/unsigned
  transaction 和 PSBT 等内容，只保留排查所需的元数据与业务摘要。
- run 事件仍受 `RUN_EVENT_TTL_SECONDS` 和 `RUN_EVENT_MAX_ENTRIES` 限制；过期后返回 `404`，
  不能用于长期审计。长期审计应接日志/数据仓库。

生产环境建议：

```dotenv
AUTH_REQUIRED=true
AUTH_TOKENS='{"<app-token>":"<user-id>"}'
ADMIN_TOKEN=<random-long-operator-secret>
```

`ADMIN_TOKEN` 应放在 Secret Manager / SSM 中，仅授予后端值班人员，并定期轮换。
外部访问必须经过 HTTPS；不要把 Demo 调试面板暴露到公网。

## HTTP 接口

```bash
curl -H "Authorization: Bearer $WALLET_AGENT_TOKEN" \
  "$WALLET_AGENT_URL/v1/agent/runs/<run-id>?event_limit=200"
```

主要返回：

| 字段 | 说明 |
| --- | --- |
| `status` / `owner_id` | run 终态与归属 |
| `conversation_id` / `session_id` | 关联的对话和业务 session |
| `summary.intent` / `task_stage` | 路由意图和任务阶段 |
| `summary.route` | `graph_role` / `route_mode` / `route_reason` |
| `summary.tool_timeline` | ReAct / 只读工具的名称、状态、耗时、错误码 |
| `summary.models` | 模型角色、模型 ID、耗时 |
| `summary.cards` | 本轮响应包含的卡片类型 |
| `summary.errors` | SSE error 事件及响应内的 `errors` / `partial_errors` |
| `session` | 不含交易体的 session 摘要 |
| `events` | 按时间顺序的事件；可用 `event_limit` 取尾部 |

`event_limit` 范围是 1 到 5000；不传时返回全部保留事件。

## 命令行

脚本只走 HTTP API，不直连 MySQL/Redis，因此本地和线上使用方式一致：

```bash
# run
python scripts/inspect_agent.py --run-id <run-id>

# 先恢复对话，再自动查看该会话最后一次 run
python scripts/inspect_agent.py --conversation-id <conversation-id> --user-id <user-id>

# 先查看 swap session，再自动查看 last_run_id
python scripts/inspect_agent.py --session-id <session-id> --user-id <user-id>
```

常用参数：

```bash
# 运维 token + 测试服务器
python scripts/inspect_agent.py \
  --base-url https://wallet-agent.example.com \
  --token "$ADMIN_TOKEN" \
  --run-id <run-id> \
  --show-events

# 输出机器可读 JSON，便于归档到工单
python scripts/inspect_agent.py --run-id <run-id> --json

# 只保留最近 50 个事件
python scripts/inspect_agent.py --run-id <run-id> --event-limit 50 --show-events
```

也可以用环境变量：

```bash
export WALLET_AGENT_URL=https://wallet-agent.example.com
export WALLET_AGENT_TOKEN="$ADMIN_TOKEN"
export WALLET_AGENT_USER_ID=<user-id>
```

## 排查顺序

1. 先看 `summary.response_kind`、`summary.route` 和 `summary.task_stage`，判断是路由错、
   槽位缺失还是业务阶段没推进。
2. 再看 `summary.tool_timeline` 的 `status` / `error_code` / `elapsed_ms`，区分模型慢、
   外部数据源失败和链上 RPC 失败。
3. 看 `summary.errors` 和最后一条 `complete` / `error` / `action_required` 事件。
4. 看 `summary.cards`：如果服务端卡片类型正确，问题在 App 投影或渲染；如果为空，
   回到 `response` / 工具结果排查。
5. 需要对齐对话时，用 `conversation_id` 查看 `messages`、`latest_artifacts` 和
   `model_trace`，不要重新发送用户请求来“复现”。

## 代码位置

- 接口：`src/wallet_agent/api/app.py`
- 安全投影：`src/wallet_agent/api/inspection.py`
- CLI：`scripts/inspect_agent.py`
- run 事件与 owner 元数据：`src/wallet_agent/persistence/runtime.py`
