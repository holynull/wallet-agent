# 生产部署手册（Wallet Agent）

> 适用分支：`main`　当前基线版本：`34ad3f9`（2026-11-02）
>
> 目标：把与测试服（`catwallet-test`）相同的拓扑与配置部署到生产，**只替换密钥**。
> 生产加固项（强制鉴权、调试面收敛等）见 §3.3，未做之前不要直接暴露公网。

---

## 1. 部署拓扑

```
浏览器 / App
    │ HTTPS
    ▼
Caddy（docker-compose.https.yml，HTTPS 终结）
    ├── /health、/ready、/v1/*  ──► wallet-agent:8000（FastAPI）
    └── 其他路径                 ──► demo-next:3000（Next.js 调试台）

wallet-agent ──► MySQL（外部托管，PERSISTENCE_URL / DEPLOY_PERSISTENCE_URL）
              ──► Redis（compose 内置容器，RUN 事件 + 会话锁）
```

| 组件 | 容器/来源 | 端口 | 说明 |
| --- | --- | --- | --- |
| `wallet-agent` | 本仓库 Dockerfile | 8000 | FastAPI + LangGraph，无签名/无私钥 |
| `redis` | `redis:7-alpine`（compose 内置） | 6379 | RUN 事件（Streams）+ 会话级串行锁；AOF 持久化 |
| `demo-next` | `demo-next/` | 3000 | 联调控制台；**生产不建议对公网暴露** |
| `caddy` | `docker-compose.https.yml` | 8443（可配） | HTTPS 终止、SSE 不缓冲、统一入口 |
| MySQL | 外部托管 | 3306 | 会话投影 `swap_sessions` + LangGraph checkpoint 表 |

### Compose 关键行为（务必知道）

- `docker-compose.yml` 把 **`PERSISTENCE_URL` 强制取 `DEPLOY_PERSISTENCE_URL`**
  （`PERSISTENCE_URL: ${DEPLOY_PERSISTENCE_URL:-...}`）。**生产必须设置 `DEPLOY_PERSISTENCE_URL`**，
  否则会连到内置默认 MySQL。
- `REDIS_URL` 在 compose 里固定为 `redis://redis:6379/0`，指向内置 Redis 容器。
- 不设置 `DEPLOY_PERSISTENCE_URL` 时，`scripts/redeploy.sh` 会自动叠加
  `docker-compose.bundled-db.yml`（内置 MySQL）。**用外部 MySQL 时不要叠加该文件**。
- `demo-next` 的 API 地址是**构建期**注入（`NEXT_PUBLIC_AGENT_API_URL`），改完必须 `--build`。

---

## 2. 前置条件

- Linux 服务器，已安装 Docker 24+ 与 Docker Compose v2（`docker compose version`）
- 可访问 GitHub（`git pull`）、模型 API、OKX、CoinGecko、各链 RPC
- 可访问生产 MySQL；数据库已建库（默认库名 `wallet_agent`）
- 端口：HTTPS 入口（默认 8443；对外建议 443）

---

## 3. 配置（`.env`）

`.env` 放在部署目录（如 `/home/ubuntu/wallet-agent/.env`），**不入库、权限 `600`**。

```bash
chmod 600 .env
```

### 3.1 必须替换的密钥（生产）

| 变量 | 用途 | 生产做法 |
| --- | --- | --- |
| `DEEPSEEK_API_KEY` | 模型 API Key | 生产账号的 key |
| `OKX_API_KEY` / `OKX_SECRET_KEY` / `OKX_PASSPHRASE` / `OKX_PROJECT_ID` | OKX 读链/行情 | 生产账号的一组凭据（四个必须齐） |
| `COINGECKO_API_KEY` | 行情兜底 | 生产 key（无 key 时 CoinGecko 有严格限流） |
| `AUTH_TOKENS` | App 鉴权 token（JSON `{"token":"user_id"}`） | 生产 token，`openssl rand -hex 32` 生成 |
| `ADMIN_TOKEN` | 运维巡检 token（可跨用户只读 run） | 随机长串，仅值班人员持有 |
| `PERSISTENCE_URL` / `DEPLOY_PERSISTENCE_URL` | 生产 MySQL 账号密码 | 生产库地址 + 只读写该库的账号 |
| `RPC_URLS` | 各链 RPC | 若使用带 key 的 RPC（Alchemy/Infura 等），换成生产 key |

生成示例：

```bash
openssl rand -hex 32   # 用于 AUTH_TOKENS 里的 token / ADMIN_TOKEN
```

`AUTH_TOKENS` 格式（token → user_id）：

```dotenv
AUTH_TOKENS='{"prod-app-token":"w1_xxx","prod-app-token-2":"w1_yyy"}'
```

> 注意：`ADMIN_TOKEN` 不是普通用户 token，不要下发到 App、不要进日志。

### 3.2 其余配置（与测试服保持一致）

以下非密钥配置照搬测试服 / `.env.example` 即可：

| 分组 | 变量 |
| --- | --- |
| 模型 | `OPENAI_MODEL`、`OPENAI_BASE_URL`、`ALLOWED_MODEL_IDS`、`READ_AGENT_MODEL` |
| Provider | `BRIDGERS_ENABLED`、`BRIDGERS_BASE_URL`、`BRIDGERS_SOURCE_FLAG`、`OMNIBRIDGE_ENABLED`、`OMNIBRIDGE_BASE_URL`、`OMNIBRIDGE_SOURCE_FLAG` |
| 超时 | `PROVIDER_TIMEOUT_SECONDS`、`HTTP_TIMEOUT_SECONDS`、`RPC_TIMEOUT_SECONDS`、`RPC_MAX_ATTEMPTS` |
| 链路 | `ALLOWED_CHAINS`、`RPC_URLS`、`BTC_MIN_CONFIRMATIONS`、`BTC_FEE_TARGET_BLOCKS`、`BTC_RBF_ENABLED` |
| 轮询 | `POLL_INTERVAL_SECONDS`、`POLL_MAX_ATTEMPTS`、`CONFIRMATION_TTL_SECONDS` |
| 持久化 | `RUN_EVENT_TTL_SECONDS`、`RUN_EVENT_MAX_ENTRIES`、`CONVERSATION_LOCK_LEASE_SECONDS`、`CONVERSATION_LOCK_WAIT_SECONDS`、`REDIS_KEY_PREFIX` |
| 行情缓存 | `PRICE_CACHE_TTL_SECONDS`、`ASSET_CACHE_TTL_SECONDS` |
| 部署地址 | `NEXT_PUBLIC_AGENT_API_URL`、`CORS_ALLOW_ORIGINS`（改成生产域名） |

### 3.3 生产安全建议（与"配置不变"并存）

当前测试服是 `AUTH_REQUIRED=false`、CORS 写 IP、Demo 对外暴露、`/docs` 可访问。
**生产建议**（详见 `docs/deployment-readiness.md`）：

```dotenv
AUTH_REQUIRED=true
AUTH_TOKENS='{"<prod-token>":"<user-id>"}'
ADMIN_TOKEN=<random-long-operator-secret>
CORS_ALLOW_ORIGINS=https://app.example.com
NEXT_PUBLIC_AGENT_API_URL=https://api.example.com
```

并在网关层：

- 只暴露 HTTPS 入口；`/docs`、`/openapi.json`、`/redoc`、`/debug/metrics` 直接 404；
- 不把 `demo-next`（3000）映射到公网；
- MySQL / Redis 只走内网或托管实例。

> 如果本次按"只换 key、其他不变"上线，请明确接受上面的公网暴露风险，并在内网/白名单内测。

---

## 4. 首次部署

```bash
# 1) 代码
cd /home/ubuntu/wallet-agent        # 部署目录（按实际调整）
git fetch origin main
git checkout main
git pull --ff-only origin main
git rev-parse --short HEAD           # 记录部署版本

# 2) 配置
cp .env.example .env                 # 首次；已有 .env 则跳过
vim .env                             # 只替换 §3.1 的 key
chmod 600 .env

# 3) 启动（外部 MySQL：不要叠加 bundled-db）
docker compose -f docker-compose.yml -f docker-compose.https.yml up -d --build

# 4) 健康检查
curl -fsS http://127.0.0.1:8000/health
curl -fsS http://127.0.0.1:8000/ready
curl -fsS http://127.0.0.1:3000/ >/dev/null && echo demo-ok

# 5) 冒烟（可选；需换 base-url）
python scripts/smoke_test.py --base-url http://127.0.0.1:8000

# 6) 查看容器
docker compose -f docker-compose.yml -f docker-compose.https.yml ps
```

也可以直接用 `./scripts/redeploy.sh`：它会自动等待 `/health`、`/ready`、Demo 健康检查；
未设置 `DEPLOY_PERSISTENCE_URL` 时会自动叠加内置 MySQL。

如无外部 MySQL、要使用内置库：

```bash
docker compose -f docker-compose.yml -f docker-compose.bundled-db.yml \
  -f docker-compose.https.yml up -d --build
```

---

## 5. 日常升级（只重建 API）

```bash
cd /home/ubuntu/wallet-agent
git pull --ff-only origin main
docker compose -f docker-compose.yml -f docker-compose.https.yml \
  up -d --build wallet-agent
curl -fsS http://127.0.0.1:8000/ready
docker compose -f docker-compose.yml -f docker-compose.https.yml ps
```

Redis / MySQL / Caddy 不需要重建；`--build wallet-agent` 只重启动 API。

---

## 6. 回滚

**回滚前先备份生产 MySQL**（`swap_sessions` + LangGraph checkpoint 表）。

```bash
cd /home/ubuntu/wallet-agent
git fetch origin main
git checkout <上一个稳定 commit>
docker compose -f docker-compose.yml -f docker-compose.https.yml up -d --build wallet-agent
curl -fsS http://127.0.0.1:8000/ready
```

确认无误后再把分支切回 `main`。不要在生产上用 `git reset --hard` 丢历史。

---

## 7. 调试接口（重点）

### 7.1 存活/就绪（无需鉴权）

| 接口 | 返回 | 用途 |
| --- | --- | --- |
| `GET /health` | `{"status":"ok"}` | 进程存活，给负载均衡/容器 healthcheck |
| `GET /ready` | `{"status":"ready"}`；失败 503 `{"detail":{"code":"PERSISTENCE_UNAVAILABLE",...}}` | 依赖（MySQL/Redis/graph）就绪 |

```bash
curl -fsS "$BASE/health"
curl -fsS "$BASE/ready"
```

### 7.2 进程指标

| 接口 | 返回 | 说明 |
| --- | --- | --- |
| `GET /debug/metrics` | `metrics`（模型/工具调用数与耗时）+ `metadata_cache` | **单副本进程内**，多实例不聚合 |

鉴权：走 `authenticated_user`。`AUTH_REQUIRED=true` 时需要任一有效 `AUTH_TOKENS`；
`AUTH_REQUIRED=false` 时无需 token。生产建议网关屏蔽或仅内网放行。

> **HTTPS overlay 的路由注意**：`deploy/https/Caddyfile` 只把 `/health`、`/ready`、`/v1/*`
> 转发给 API，`/debug/metrics` 等路径会落到 `demo-next`（通常 404）。调试时请直连
> API 的 8000 端口（内网或 `ssh -L 8000:127.0.0.1:8000 <prod-host>` 隧道）。

### 7.3 运行巡检（核心排查接口）

```http
GET /v1/agent/runs/{run_id}?event_limit=1..5000
Authorization: Bearer <ADMIN_TOKEN 或该 run owner 的 token>
```

- 权限：默认 **owner-only**；`ADMIN_TOKEN` 可跨用户读取；跨用户/不存在统一返回 `404`。
  `AUTH_REQUIRED=false` 且配置了 `ADMIN_TOKEN` 时，该接口要求 operator token。
- `event_limit` 取尾部事件；不传返回全部保留事件（上限 5000）。
- 返回字段：

| 字段 | 内容 |
| --- | --- |
| `status` / `owner_id` | run 终态与归属 |
| `session_id` / `conversation_id` | 关联会话 |
| `summary.intent` / `task_stage` / `response_kind` | 意图、阶段、响应类型 |
| `summary.route` | `graph_role` / `route_mode` / `route_reason` |
| `summary.tool_timeline` | 工具名、状态、耗时、错误码 |
| `summary.models` | 模型角色/模型 id/耗时/token |
| `summary.cards` | 本轮卡片类型 |
| `summary.errors` | 响应 `errors` / `partial_errors` |
| `session` | 会话摘要（不含交易体） |
| `events` | 事件流（`update`/`model`/`progress`/`complete`/`action_required`） |

安全：响应会递归移除 Authorization/Cookie/API key、私钥/助记词、签名、
raw/unsigned transaction、PSBT 等（见 `src/wallet_agent/api/inspection.py`）。

### 7.4 会话与业务状态

| 接口 | 用途 | 鉴权 |
| --- | --- | --- |
| `GET /v1/agent/conversations?user_id=&limit=&cursor=&q=` | 会话列表（按发布时间倒序） | `AUTH_REQUIRED=true` 时需 token；`ADMIN_TOKEN` 可跨用户 |
| `GET /v1/agent/conversations/{conversation_id}` | 对话 `messages`、`latest_artifacts`、`model_trace` | 同上 |
| `GET /v1/swap/{session_id}` | 兑换会话完整记录（`status`/`stage`/`broadcast_status`/`order_status`/`pending_transaction`/`last_error`），读取时会主动刷新 Provider 订单状态 | owner 或 `ADMIN_TOKEN` |
| `GET /v1/transactions/{chain}/{tx_hash}` | 链上交易状态 | `AUTH_REQUIRED=true` 时需 token |

`GET /v1/swap/{session_id}` 是排查“兑换一直 processing / 卡片不出”的首选：
它会向 Provider 查一次订单状态并持久化，再返回最新记录。

### 7.5 SSE 事件流

```http
GET /v1/agent/stream/{run_id}
Last-Event-ID: <上次事件 id>
```

- 事件类型：`update` / `model` / `progress` / `complete` / `action_required` / `error`。
- 支持 `Last-Event-ID` 或 `?last_event_id=` 断线续传；终态后自动结束。
- Caddy 已配置 `flush_interval -1`，不会缓冲 SSE。

### 7.6 FastAPI 自动文档（生产必须处理）

当前 `create_app` 未关闭 FastAPI 默认文档，以下路径**默认对外可访问**：

- `GET /docs`
- `GET /openapi.json`
- `GET /redoc`

> 通过 Caddy HTTPS 入口时这些路径会落到 `demo-next`，但**直连 8000 端口仍可访问**；
> 生产仍需按下面处理。

生产二选一：

1. **网关屏蔽**（推荐，不改代码）。Caddy 示例：

   ```caddyfile
   @docs path /docs* /openapi.json /redoc*
   respond @docs 404
   ```

2. 代码里 `FastAPI(..., docs_url=None, redoc_url=None, openapi_url=None)` 后重建镜像。

### 7.7 调试接口权限矩阵（生产）

| 接口 | 生产建议 | `AUTH_REQUIRED=true` 时 | `AUTH_REQUIRED=false` 时 |
| --- | --- | --- | --- |
| `/health`、`/ready` | 保留（内网/负载均衡） | 无鉴权 | 无鉴权 |
| `/debug/metrics` | 网关屏蔽或仅内网 | 任意有效 App token | 无鉴权 |
| `/v1/agent/runs/{run_id}` | 保留给运维 | owner 或 `ADMIN_TOKEN` | 配置 `ADMIN_TOKEN` 时需 operator；否则开放 |
| `/docs`、`/openapi.json`、`/redoc` | **屏蔽 404** | 无鉴权（默认） | 无鉴权（默认） |
| `/v1/agent/conversations*` | 保留给 App | Bearer | 无鉴权（可带 `user_id`） |
| `/v1/swap/{id}` | 保留给 App | owner 或 `ADMIN_TOKEN` | 按 `user_id` 校验 |

### 7.8 命令行巡检

`scripts/inspect_agent.py` 只走 HTTP，不直连数据库/Redis：

```bash
# 直接看 run
python scripts/inspect_agent.py --base-url "$BASE" --token "$ADMIN_TOKEN" \
  --run-id <run-id> --show-events

# 由 conversation_id 恢复对话并查看最后一次 run
python scripts/inspect_agent.py --base-url "$BASE" --token "$ADMIN_TOKEN" \
  --conversation-id <conversation-id> --user-id <user-id>

# 由 swap session 查看 last_run_id
python scripts/inspect_agent.py --base-url "$BASE" --token "$ADMIN_TOKEN" \
  --session-id <session-id> --user-id <user-id>
```

### 7.9 常见错误码

| 码 | 含义 | 排查 |
| --- | --- | --- |
| `PAIR_NOT_SUPPORTED` | Provider 明确不支持该币对（omnibridge `isSupport=false`） | 换币对（当前 ETH/BSC USDT 不可用，USDC 正常） |
| `PROVIDER_QUOTE_FAILED` | 报价失败；看 `details.provider_code`（如 bridgers `412`） | Provider 路由/网络；`412 quote fail` 多为 Provider 侧不可用 |
| `PROVIDER_PREPARE_FAILED` | 建单失败；看 `details.provider_code`（如 omnibridge `218`） | 历史路径；已改为询价阶段 `PAIR_NOT_SUPPORTED` |
| `NO_VALID_QUOTES` | 所有 Provider 都没出价 | 看 `react_results.provider_errors` |
| `CONFIRMATION_NOT_PENDING`（409） | 确认态已变化（App 重复 `/confirm`） | App 收到 409 应停止重试并刷新 `GET /v1/swap` |
| `TRANSACTION_PREFLIGHT_FAILED` | 预检查失败（余额不足/模拟失败） | 看 `preflight.simulation.failure_reason` |
| `PERSISTENCE_UNAVAILABLE`（`/ready` 503） | MySQL/Redis 不可用 | 检查 `DEPLOY_PERSISTENCE_URL`、Redis 容器 |
| `RUN_NOT_FOUND`（SSE error） | run 已过期/不存在 | 受 `RUN_EVENT_TTL_SECONDS` 限制 |

---

## 8. 日志

```bash
docker compose -f docker-compose.yml -f docker-compose.https.yml \
  logs -f --tail=200 wallet-agent
```

- `/debug/metrics` 是**单实例进程内**指标，多副本不能当全局指标。
- 生产建议接日志聚合（Loki/CloudWatch 等），并保留 `run_id` / `conversation_id` 便于巡检。

---

## 9. 备份与恢复

- **MySQL**：备份 `wallet_agent` 库（至少 `swap_sessions` 与 LangGraph checkpoint 表）。
  示例：`mysqldump -h <host> -u <user> -p wallet_agent > wallet_agent_$(date +%F).sql`
- **Redis**：compose 内置 Redis 使用 AOF 卷（`wallet-agent-redis`）；RUN 事件有 TTL，
  长期审计请另存日志/数仓。
- 恢复演练：在预发环境用备份恢复一次，确认会话与订单状态一致后再上生产。

---

## 10. 变更记录

| 日期 | 版本 | 内容 |
| --- | --- | --- |
| 2026-11-02 | `34ad3f9` | 首版生产部署手册；记录调试接口与鉴权矩阵；补充 412/218/`PAIR_NOT_SUPPORTED` 排查 |

---

## 11. 上线前检查清单

- [ ] 已记录部署 commit（`git rev-parse --short HEAD`）与上一个稳定 commit（回滚用）
- [ ] `.env` 已替换 §3.1 全部密钥，且 `chmod 600 .env`
- [ ] `DEPLOY_PERSISTENCE_URL` 指向生产 MySQL（未误用内置库）
- [ ] 生产 MySQL 已备份 / 可恢复；Redis 卷有持久化
- [ ] 若走生产加固：`AUTH_REQUIRED=true`、`AUTH_TOKENS`、`ADMIN_TOKEN`、CORS 域名已配置
- [ ] `/docs`、`/openapi.json`、`/redoc`、`/debug/metrics` 已在网关屏蔽或仅内网放行
- [ ] `demo-next` 未映射到公网（生产）
- [ ] `/health`、`/ready` 返回正常；`scripts/smoke_test.py` 通过
- [ ] 用 `python scripts/inspect_agent.py --run-id <run-id>` 验证巡检接口可用
- [ ] 部署后观察 `wallet-agent` 日志 5–10 分钟，无 5xx / provider 异常放大
