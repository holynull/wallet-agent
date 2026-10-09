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

### 钱包 App 的多链余额上下文（包含 BTC）

App 每次调用 `/v1/agent/turn` 时，应提供完整的 `supported_networks` 和 `wallet_accounts`。
`chain` / `address` 仍表示当前签名钱包；余额查询的默认范围来自 App 配置，不受当前签名网络限制。

```json
{
  "user_id": "demo-user",
  "message": "我的全部余额",
  "chain": "ETH",
  "address": "0x1111111111111111111111111111111111111111",
  "supported_networks": ["ETH", "BSC", "BASE", "POLYGON", "ARBITRUM", "OPTIMISM", "BTC", "TRON", "SOLANA"],
  "wallet_accounts": [
    {"chain": "ETH", "address": "0x1111111111111111111111111111111111111111"},
    {"chain": "BASE", "address": "0x1111111111111111111111111111111111111111"},
    {"chain": "BTC", "address": "1BoatSLRHtKNngkdXEeobR76b53LETtpyT"},
    {"chain": "BTC", "address": "3J98t1WpEZ73CNmQviecrnyiWrnqRhWNLy"}
  ]
}
```

这些是格式示例公共地址。EVM 地址即使相同，也应为每个 App 支持的网络显式添加账户记录；每个网络允许多个账户。
BTC 支持主网 Base58Check、SegWit 和 Taproot 地址，OKX chainIndex 为 `0`，精度为 8。
BTC 钱包需由 App 提供全部相关收款和找零地址；单个地址或少量地址的余额不能代表整个 HD 钱包。
当前能力是地址余额查询，不包含 BTC 签名或广播。仅发送公共地址，不能发送助记词、私钥或 xprv。

两个字段分别最多支持 50 个网络和 100 条账户记录，重复的网络/地址不会重复计算。
缺省字段兼容旧客户端：单个 EVM 地址默认查询服务支持的 EVM 网络；不跨地址家族推断账户。
显式 `[]` 会清空相应配置，防止历史账户被继续使用；App 应始终同时发送两个完整字段。
明确询问某条网络只查该网络，仅问某个币种则对所有配置网络按币种过滤，意图由 LLM 根据上下文解释。

工具数据与最终回复的 `response.portfolio` 包含：

- `assets`：完整资产明细，已知美元价值大于零的资产全部保留；价格未知的正余额也保留并标注。
- `total_usd_value`：同一份返回明细中已知美元价值的合计，不混用 Provider 另一接口的总值。
- `complete`：网络和账户是否全部查询成功；`valuation_complete` 表示所有明细是否都有价格。
- `network_status`：逐链的 `success`、`partial`、`failed`、`missing_address`、`invalid_address` 或 `unsupported`，以及逐账户结果。

逐链/逐地址有界并发、超时隔离，部分失败仍返回其他地址余额。未查询、缺失地址与查询失败都不能视为零余额。
Demo 顶部“模拟钱包 App”可配置上述网络和地址，配置保存在浏览器；余额卡片紧跟回复并随历史恢复。
极小非零美元价值显示 `<0.01`，没有截断明细行。

### 读取 SSE 事件

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

更完整的生命周期说明见 [mobile-integration.md](mobile-integration.md)。
