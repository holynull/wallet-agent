# Demo 接口调用说明

本文面向 Demo、移动端或其他前端客户端，说明如何调用 Wallet Agent 的 REST 和 SSE 接口。
默认服务地址为 `http://localhost:8000`，远程部署时替换为实际 API 地址。

## 安全边界

服务端只接收公开钱包上下文和交易哈希。以下内容不能放入请求、metadata、Prompt、日志或前端调试输出：

- 私钥、助记词、seed phrase；
- signer、wallet client 或 `window.ethereum` 对象；
- API key、secret、password 或其他凭据。

签名和广播始终由浏览器钱包或移动端钱包完成。服务端只返回未签名交易，并接收钱包返回的链上交易哈希。

## 1. 基础检查

```bash
curl http://localhost:8000/health
# {"status":"ok"}

curl http://localhost:8000/ready
# {"status":"ready"}
```

如果服务启用了认证，在所有请求中增加：

```http
Authorization: Bearer <token>
```

## 2. 普通对话和 SSE

### 创建一次对话运行

`POST /v1/agent/turn` 返回 `run_id`。客户端必须保存 `run_id`、`conversation_id`，兑换场景还要保存 `session_id`。

```bash
curl -sS -X POST http://localhost:8000/v1/agent/turn \
  -H 'Content-Type: application/json' \
  -d '{
    "user_id": "demo-user",
    "message": "今天 BTC 的价格是多少",
    "address": "0x1111111111111111111111111111111111111111",
    "chain": "ETH",
    "metadata": {"wallet_chain_id": "0x1"}
  }'
```

典型返回：

```json
{
  "run_id": "<run-id>",
  "conversation_id": "<conversation-id>",
  "status": "running"
}
```

### 读取 SSE

```bash
curl -N http://localhost:8000/v1/agent/stream/<run-id>
```

客户端应处理 `progress`、`update`、`action_required`、`complete` 和 `error` 事件，并保存每个 SSE 帧的 `id`。
不要只根据 `POST /turn` 的 HTTP 200 判断业务成功。

断线后不要重新创建 turn，使用原 `run_id` 和最后一个事件 ID 恢复：

```bash
curl -N \
  'http://localhost:8000/v1/agent/stream/<run-id>?last_event_id=<last-event-id>'
```

也可以使用 `Last-Event-ID` 请求头。恢复只会返回游标之后的事件。

## 3. 只读能力

### 当前 Token 价格

```bash
curl -G http://localhost:8000/v1/prices/token \
  --data-urlencode chain=ETH \
  --data-urlencode symbol=ETH \
  --data-urlencode decimals=18
```

自然语言价格查询也可以直接通过 `/v1/agent/turn`：

```json
{"user_id":"demo-user","message":"ETH 的价格多少"}
```

### 钱包组合和 Gas

```bash
curl -G http://localhost:8000/v1/wallet/<address>/portfolio \
  --data-urlencode chain=ETH

curl -G http://localhost:8000/v1/wallet/<address>/gas \
  --data-urlencode chain=ETH
```

### 资产发现

```bash
curl -G http://localhost:8000/v1/assets \
  --data-urlencode chain=BASE \
  --data-urlencode search=USDC
```

### 交易状态

```bash
curl http://localhost:8000/v1/transactions/ETH/0x<64-hex-tx-hash>
```

## 4. 转账流程

1. 发送自然语言请求，携带公开地址和链：

```json
{
  "user_id": "demo-user",
  "message": "转 1 USDC 给 0x2222222222222222222222222222222222222222",
  "address": "0x1111111111111111111111111111111111111111",
  "chain": "ETH"
}
```

2. 读取 SSE，等待 `response.kind = "transfer_prepare"`。
3. 向用户展示 `pending_transaction` 和 `preflight`，由钱包在本地签名并广播。
4. 只把钱包返回的交易哈希提交给：

```bash
curl -sS -X POST http://localhost:8000/v1/transfer/<session-id>/broadcast \
  -H 'Content-Type: application/json' \
  -d '{
    "user_id": "demo-user",
    "chain": "ETH",
    "tx_hash": "0x<64-hex-tx-hash>"
  }'
```

## 5. 兑换流程

1. 创建兑换对话，例如：

```json
{
  "conversation_id": "stable-conversation-id",
  "user_id": "demo-user",
  "message": "把 1 USDC 从 Base 换成 BSC 的 USDT",
  "address": "0x1111111111111111111111111111111111111111",
  "chain": "BASE",
  "metadata": {"wallet_chain_id": "0x2105"}
}
```

2. 如果返回 clarification，使用同一个 `conversation_id` 和 `session_id` 继续补充参数。
3. 等待报价卡片，展示所有 `quote_candidates`，不要自动选择 Provider。
4. 用户选择报价后提交原样的 `provider_reference`：

```bash
curl -sS -X POST http://localhost:8000/v1/swap/<session-id>/select-quote \
  -H 'Content-Type: application/json' \
  -d '{"user_id":"demo-user","provider_reference":"<provider-reference>"}'
```

5. 如果返回 `approval_transaction`，钱包本地签名并广播，然后提交：

```bash
curl -sS -X POST http://localhost:8000/v1/swap/<session-id>/approve-broadcast \
  -H 'Content-Type: application/json' \
  -d '{
    "user_id":"demo-user",
    "chain":"BASE",
    "approve_tx_hash":"0x<64-hex-tx-hash>"
  }'
```

6. 继续检查 Allowance：

```bash
curl -sS -X POST http://localhost:8000/v1/swap/<session-id>/continue \
  -H 'Content-Type: application/json' \
  -d '{"user_id":"demo-user"}'
```

7. 得到 `pending_transaction` 后，钱包本地签名并广播；只提交最终兑换交易哈希：

```bash
curl -sS -X POST http://localhost:8000/v1/swap/<session-id>/broadcast \
  -H 'Content-Type: application/json' \
  -d '{
    "user_id":"demo-user",
    "chain":"BASE",
    "tx_hash":"0x<64-hex-tx-hash>"
  }'
```

8. 查询 Provider 订单和到账状态：

```bash
curl http://localhost:8000/v1/swap/<session-id>?user_id=demo-user
```

## 6. 前端调用要点

- 每个用户操作对应一个 `turn`，后续补充信息复用 `conversation_id` 和 `session_id`。
- SSE 断线时复用 `run_id`，不要重复发起兑换或广播。
- 用户修改兑换资产、网络或数量后，旧报价和未签名交易必须视为失效。
- 服务端不会替用户签名、选择 Provider 或自动广播。
- `pending_transaction` 的 `chain`/`chain_id` 必须和钱包当前网络一致后才能调用钱包签名方法。

更完整的生命周期说明见 [mobile-integration.md](mobile-integration.md)，浏览器调试步骤见 [local-demo-debugging.md](local-demo-debugging.md)。
