# App 端接入 Wallet Agent

本文面向移动端 / App 客户端，说明如何按 Demo（`demo-next`）的既有协议接入 Wallet Agent。
接口清单和字段级说明见 [demo-api-guide.md](demo-api-guide.md)，交易生命周期见
[mobile-integration.md](mobile-integration.md)；本文聚焦「App 怎么实现」，给出可直接照抄的调用顺序、状态处理与自测方式。

Demo 中的对照实现：

- 请求封装与错误解析：`demo-next/app/page.tsx:166`、`demo-next/lib/agent-utils.ts:24`
- 发起一轮对话：`demo-next/app/page.tsx:469`
- 消费 SSE 与断线重连：`demo-next/app/page.tsx:481`
- 事件 → UI 卡片投影：`demo-next/app/page.tsx:610`、`demo-next/lib/agent-utils.ts:268`
- 钱包上下文构造：`demo-next/lib/agent-utils.ts:248`
- 交易动作（选择报价 / 确认 / Approve / 广播 / 查询）：`demo-next/app/page.tsx:562`

## 1. 安全边界

App 是唯一的签名方。以下内容绝不放进请求体、`metadata`、日志或调试面板：

- 私钥、助记词、seed phrase、xprv；
- signer / wallet client / `window.ethereum` 等对象；
- API key、secret、password 等凭据。

服务端会校验请求体：一旦在 `metadata` 等位置出现上述字段，`POST /v1/agent/turn` 直接返回
`422 {"code":"VALIDATION_ERROR"}`，请求不会下发到模型。服务端只返回**未签名交易**，并接收钱包返回的**链上交易哈希**。

## 2. 接入准备

1. 配置 API 地址（Demo 用 `NEXT_PUBLIC_AGENT_API_URL`，App 侧写入构建配置或下发配置）。
2. 启动时探测可用性：

```http
GET /health   -> {"status":"ok"}
GET /ready    -> {"status":"ready"}
```

3. 若服务端开启 `AUTH_REQUIRED`，所有请求带 `Authorization: Bearer <token>`。
   App 侧 token 由自有账号体系换取，不要硬编码在包内。
4. 为每个本地用户生成稳定的 `user_id`，并在**所有**请求中保持一致。会话归属按 `user_id` 校验，
   App 不能用别人的 `conversation_id` / `session_id`。

## 3. 每轮请求都要携带的钱包上下文

`POST /v1/agent/turn` 的字段（服务端 `extra="forbid"`，多余字段会报错）：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `user_id` | string | 用户标识，会话归属用 |
| `message` | string | 用户自然语言输入，必填 |
| `conversation_id` | string? | 会话 ID；首轮不传，之后回传服务端返回的值 |
| `session_id` | string? | 兑换/转账 session；由服务端创建并返回，续轮原样回传 |
| `address` | string? | 当前签名钱包的公开地址 |
| `chain` | string? | 当前签名网络，如 `BASE`、`ETH`、`TRON`、`SOLANA` |
| `metadata.wallet_chain_id` | string? | 当前网络 ID，如 `0x2105`；用于网络一致性校验 |
| `supported_networks` | string[]? | App 支持查询的全部网络，最多 50 个 |
| `wallet_accounts` | {chain,address}[]? | 各网络公开地址，最多 100 条 |
| `metadata` | object | 结构化意图（可选，见下） |
| `model_id` | string? | 指定模型；必须命中服务端 `ALLOWED_MODEL_IDS`，否则 `MODEL_NOT_ALLOWED` |

`address` / `chain` 表示**当前签名钱包**；`supported_networks` / `wallet_accounts` 表示**余额查询范围**，
两者分离：用户可以在签名网络是 `ETH` 时查询全部链的资产。

多链余额上下文按下面方式构造（对应 `demo-next/lib/agent-utils.ts:248`）：

```json
{
  "user_id": "app-user-42",
  "message": "看下我的资产组合",
  "conversation_id": "conv-1",
  "address": "0x1111111111111111111111111111111111111111",
  "chain": "BASE",
  "supported_networks": ["ETH", "BSC", "BASE", "POLYGON", "ARBITRUM", "OPTIMISM", "BTC", "TRON", "SOLANA"],
  "wallet_accounts": [
    {"chain": "ETH", "address": "0x1111111111111111111111111111111111111111"},
    {"chain": "BASE", "address": "0x1111111111111111111111111111111111111111"},
    {"chain": "BTC", "address": "bc1qexampleaddress"},
    {"chain": "TRON", "address": "TExampleAddress"},
    {"chain": "SOLANA", "address": "ExampleSolanaAddress"}
  ],
  "metadata": {"wallet_chain_id": "0x2105"}
}
```

要点：

- 相同 EVM 地址也要按网络逐条列出，不要只发一条 `ETH`。
- BTC 必须提供全部收款与找零地址；`BTC` 当前只做地址余额查询，不支持 BTC 签名广播。
- 两个字段每次都发送**完整快照**；显式传 `[]` 表示清空范围，避免历史账户被继续使用。
- 缺省（不传）时按旧客户端兼容处理：单个 EVM 地址默认查询服务端支持的 EVM 网络。

`metadata` 里也可以直接下发结构化意图，跳过自然语言解析，适合 App 内已有明确入口的场景
（如余额页、Gas 页）：

```json
{"portfolio_query": {"address": "0x...", "chain": "BASE"}}
```

支持 `swap_request` / `transfer_request` / `price_request` / `transaction_query` / `portfolio_query` /
`gas_request` / `asset_query`。

## 4. 一轮对话的完整时序

```
App                                    Wallet Agent
 │  POST /v1/agent/turn                    │
 │ ──────────────────────────────────────► │  返回 {run_id, conversation_id, status:"running"}
 │                                         │  （兑换/转账会额外返回 session_id）
 │  GET /v1/agent/stream/{run_id}          │
 │ ──────────────────────────────────────► │  SSE：progress / update / response / action_required / complete
 │  ◄──── 卡片数据（报价、未签名交易等）─── │
 │                                         │
 │  本地签名 + 广播（钱包栈）               │
 │  POST /v1/swap|transfer/{session}/broadcast
 │ ──────────────────────────────────────► │  只提交 tx_hash
```

### 4.1 创建 run

```http
POST /v1/agent/turn
Content-Type: application/json

{"user_id":"app-user-42","conversation_id":"conv-1","message":"把 1 USDC 从 Base 换成 BSC 的 USDT",
 "address":"0x1111...","chain":"BASE","metadata":{"wallet_chain_id":"0x2105"}}
```

响应（HTTP 200 只代表 run 已受理，**不代表业务成功**）：

```json
{"run_id":"<run-id>","conversation_id":"conv-1","status":"running","session_id":"<swap-session>"}
```

App 必须持久化 `run_id`、`conversation_id`、`session_id`。

### 4.2 消费 SSE

```http
GET /v1/agent/stream/{run_id}
Accept: text/event-stream
```

帧格式：

```
id: 1736298045123-0
event: response
data: {"event":"response","state":{...}}
```

事件类型：

| event | 含义 | App 处理 |
| --- | --- | --- |
| `progress` | 阶段进度（含 `elapsed_ms`） | 更新 loading 文案，不视为终态 |
| `update` | 中间状态快照 | 可忽略或用于调试面板 |
| `response` | 本轮业务响应（含 `response.kind`） | 渲染卡片与助手文案 |
| `action_required` | 需要用户操作（如确认/授权） | 渲染交互卡片，**终态** |
| `complete` | 本轮结束 | 结束 loading，**终态** |
| `error` | 执行失败 | 展示 `code` / `message`，**终态** |

实现要点（对应 `demo-next/app/page.tsx:481`）：

- 保存每个帧的 `id` 作为游标；只有整帧解析成功后才更新游标。
- 断线**不要重新发 turn**，用游标续传：

```http
GET /v1/agent/stream/{run_id}?last_event_id=1736298045123-0
```

也可用 `Last-Event-ID` 请求头。服务端只回放游标之后的事件，因此换实例重连也不会重复处理。
- 未收到终态就断开视为异常，需要重连（Demo 最多重试 3 次，退避 250ms × 次数）。
- 收到 `RUN_NOT_FOUND` 说明 run 事件已过期（`RUN_EVENT_TTL_SECONDS`），此时应以本地保存的会话快照为准。
- 用户取消时 `AbortController.abort()` 掉流即可；服务端侧另见 4.4。

### 4.3 从事件里取卡片

`data.state` 是当前图状态快照，业务响应在 `state.response`。Demo 的归一化写法可复用：

```js
const current = payload?.state || payload?.data?.state || payload?.data || payload;
const response = current?.response?.response || current?.response || current;
```

`response.kind` 决定渲染什么卡片：

| `kind` | 关键字段 | 说明 |
| --- | --- | --- |
| `clarification` | `message`、`suggestions`、`token_candidates` | 缺参数，用同一 `conversation_id` / `session_id` 补发一轮 |
| `wallet_query` | `wallet` | 余额 |
| `portfolio_query` | `portfolio` | 资产组合（`assets`、`total_usd_value`、`complete`、`network_status`） |
| `price_query` | `symbol` 等 | 价格（附来源时间，非报价承诺） |
| `gas_check` | `gas` | Gas 估算、`sufficient`、`shortfall_raw` |
| `asset_discovery` | `assets` | 资产发现（含合约地址、精度） |
| `swap_quote` | `quotes[]` | 报价列表，必须全部展示，**不要**自动选 |
| `confirmation_required` | `confirmation` | 二次确认卡片 |
| `approval_required` | `approval_transaction` | 需要先 Approve |
| `swap_prepare` / `transfer_prepare` | `pending_transaction`、`preflight` | 待签名交易 |
| `swap_status` / `transaction_status` | `status` | 链上状态 |
| `unsupported` / `error` | `message`、`errors[]` | 兜底展示 |
| `react` | `react_results[]` | ReAct 工具调用结果，按 tool 名自行投影 |

恢复历史时需要同样的投影逻辑：`GET /v1/agent/conversations/{id}` 返回 `latest_artifacts`，
字段与上表一一对应（`quotes` / `confirmation` / `approval` / `pending_transaction` / `pending_kind` /
`portfolio` / `gas` / `assets` / `status` / `broadcast_hash` / `read_only`）。

## 5. 业务流程

### 5.1 只读查询

两条路都可用，App 按场景选择：

- 自然语言：`POST /v1/agent/turn`，等待 `response`。
- 结构化 REST（不经过模型，适合钱包页直出）：

| 能力 | 接口 |
| --- | --- |
| 组合 | `GET /v1/wallet/{address}/portfolio?chain=BASE` |
| Gas | `GET /v1/wallet/{address}/gas?chain=BASE` |
| 余额 | `GET /v1/wallet/{address}/balances?chain=BASE` |
| 交易记录/手续费 | `GET /v1/wallet/{address}/transactions?chain=BASE`、`/fees?chain=BASE` |
| 资产发现 | `GET /v1/assets?chain=BASE&search=USDC` |
| 价格 | `GET /v1/prices/token?chain=ETH&symbol=ETH&decimals=18` |
| 交易状态 | `GET /v1/transactions/{chain}/{tx_hash}` |

约定：价格未知或 RPC 失败返回 `unavailable`，**不能显示成 0**；`<0.01` 的极小美元价值按 `<0.01` 展示。

### 5.2 转账

1. 发起自然语言请求，带 `address` + `chain`。
2. 等到 `response.kind === "transfer_prepare"`，展示 `preflight`（失败项、警告）和
   `pending_transaction`。
3. 校验 `pending_transaction.chain` / `chain_id` 与钱包当前网络一致；不一致先
   `wallet_switchEthereumChain`，用户拒绝则不签名。
4. 本地签名并广播，只把哈希提交回来：

```http
POST /v1/transfer/{session_id}/broadcast
{"user_id":"app-user-42","chain":"ETH","tx_hash":"0x<64 hex>"}
```

5. 重复提交同一哈希是幂等的；同一 session 提交不同哈希会被拒绝。

### 5.3 兑换

1. 发起请求（含 `provider_reference` 之前不要预设 Provider）。
2. 若为 `clarification`，用同一 `conversation_id` / `session_id` 补齐源/目标 token 合约与精度。
3. 展示全部 `quotes[]`；用户点选后提交**原样**的 `provider_reference`：

```http
POST /v1/swap/{session_id}/select-quote
{"user_id":"app-user-42","provider_reference":"bridgers-quote-123"}
```

4. 若返回 `approval_required`，展示 `approval_transaction`，本地签名广播后：

```http
POST /v1/swap/{session_id}/approve-broadcast
{"user_id":"app-user-42","chain":"BASE","approve_tx_hash":"0x<64 hex>"}
```

5. 轮询检查 Allowance（Demo 每 2s 一次，最多 60 次）：

```http
POST /v1/swap/{session_id}/continue
{"user_id":"app-user-42"}
```

`stage` 可能为 `approval_pending` / `approval_failed` / `approval_required`，只有 `swap_ready` 才能继续签名。

6. 展示 `pending_transaction`，本地签名广播后提交最终哈希：

```http
POST /v1/swap/{session_id}/broadcast
{"user_id":"app-user-42","chain":"BASE","tx_hash":"0x<64 hex>"}
```

7. 查询订单状态：`GET /v1/swap/{session_id}?user_id=app-user-42`。

### 5.4 取消与失效

- 取消当前兑换：`POST /v1/swap/{session_id}/cancel`（`{"user_id":"..."}`）。
- `confirmation` 有 `requested_at` / `expires_at`；超时后服务端返回 `CONFIRMATION_EXPIRED`，需重新发起。
- 确认卡片带 `task_id` / `task_revision` / `payload_hash`。用户改了币种、网络或数量后，
  **旧报价、旧确认、旧未签名交易一律作废**，必须重新走一轮。
- 广播失败后不要伪造成功状态；只提交真实哈希。

## 6. 签名与广播（App 职责）

- 未签名交易包含 `chain`、`chain_id`、`to`、`data`、`value`。
- 签名前必须向用户展示资产、数量、收款方 / spender，并做一次明确确认。
- `pending_transaction` 的 `chain` / `chain_id` 必须与钱包当前网络一致，否则先切网。
- 服务端**不会**替 App 签名、选 Provider 或自动广播。

## 7. 会话与历史

| 操作 | 接口 |
| --- | --- |
| 列表 | `GET /v1/agent/conversations?user_id=...&limit=50` |
| 详情 | `GET /v1/agent/conversations/{id}?user_id=...` |
| 删除 | `DELETE /v1/agent/conversations/{id}?user_id=...` |

详情返回 `messages`（含 `artifacts` 卡片快照，服务端在 SSE 中已剥离该字段以去重）、
`latest_artifacts`、`model_trace`、`session_id`。App 冷启动时应据此恢复消息列表和当前卡片，
而不是依赖 SSE 重放。

## 8. 错误处理

REST 错误统一信封：

```json
{"code":"VALIDATION_ERROR","message":"..."}
```

| code | 场景 | App 处理 |
| --- | --- | --- |
| `VALIDATION_ERROR` | 字段缺失 / 非法 / 携带签名材料 | 修正请求，不要重试 |
| `MODEL_NOT_ALLOWED` | `model_id` 不在白名单 | 回落到默认模型 |
| `INVALID_AGENT_TEST_INTENT` | 测试意图非法 | 仅联调用 |
| `CONFIRMATION_EXPIRED` / `CONFIRMATION_NOT_PENDING` | 确认已过期或不存在 | 重新发起业务 |
| `CONFIRMATION_RESUME_NOOP` | 重复恢复确认 | 忽略 |
| `PROVIDER_REGISTRATION_FAILED` | 广播哈希登记失败 | 可重试同一哈希 |
| `INVALID_TRANSACTION_HASH` | 哈希格式错误 | 修正后重试 |
| `OKX_CAPABILITY_UNAVAILABLE` / `PRICE_PROVIDER_UNAVAILABLE` | 上游不可用 | 降级展示，稍后重试 |
| `RUN_NOT_FOUND` | run 事件过期 | 用本地会话快照恢复 |

SSE 的 `error` 事件正文同样是 `code` + `message`。

## 9. 实现清单

- 一个用户操作 = 一个 `turn`；补参数复用 `conversation_id` / `session_id`，不要新建会话。
- 每次 `turn` 独立维护进度时间线，不要把上一轮的进度混进来。
- SSE 必须处理终态与重连，`HTTP 200` 不能当作成功判据。
- 卡片渲染与历史恢复共用同一套投影函数，避免两处逻辑漂移。
- 未签名交易只在 App 侧签名，提交的永远只是哈希。
- 余额、价格、Gas 的未知值一律显示为「未知 / 不可用」，不能当成 0。

## 10. 自测

冒烟脚本（仓库自带）：

```bash
python scripts/smoke_test.py --base-url http://<server-ip>:8000 --message "你好"
```

手工核对一轮对话：

```bash
# 1) 建 run
curl -sS -X POST http://<server-ip>:8000/v1/agent/turn \
  -H 'Content-Type: application/json' \
  -d '{"user_id":"smoke-user","message":"ETH 的价格多少"}'

# 2) 读 SSE（把 <run-id> 换成上一步返回值）
curl -N http://<server-ip>:8000/v1/agent/stream/<run-id>

# 3) 断线重连（把 <event-id> 换成最后一帧的 id）
curl -N 'http://<server-ip>:8000/v1/agent/stream/<run-id>?last_event_id=<event-id>'
```

真机联调清单：

- 冷启动 → `/health`、`/ready` 通过后 UI 才允许发送。
- 首轮不传 `conversation_id`，后续轮次正确回传。
- 余额查询覆盖多链；未提供地址的网络显示「未提供地址」而不是 0。
- 兑换走完 报价 → 选择 → （Approve）→ 广播 → 状态 全流程。
- 杀进程/断网后重连：不重复发 turn、不重复广播，状态能恢复到正确阶段。
- 未签名交易的网络与钱包当前网络不一致时，签名入口被正确拦截。
