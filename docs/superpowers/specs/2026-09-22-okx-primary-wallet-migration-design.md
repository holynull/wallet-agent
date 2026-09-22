# OKX 主数据源钱包接口迁移设计

## 目标

将钱包 Agent 的钱包数据、资产估值、市场数据和交易查询统一迁移到 OKX
Onchain OS API；Bridgers 和 OmniBridge 继续作为唯一的兑换 Provider；浏览器
钱包继续负责用户确认、签名和发送交易。

这不是把所有网络访问一刀切替换成 OKX。Allowance、Nonce、EIP-1559 费用字段、
Approval 回执和浏览器钱包广播后的链上可见性仍属于执行观察能力。当前 OKX
Wallet API 文档没有为这些能力提供已确认且语义等价的通用替代，因此保留一个
最小的执行观察层，避免把安全检查降级成提示。

CoinGecko 不属于目标架构，运行时不再使用它。旧文档中的 CoinGecko fallback
描述在迁移完成后统一清理。

## 范围与边界

### 迁移到 OKX 的能力

| 能力 | OKX 接口 | 迁移策略 |
| --- | --- | --- |
| 总资产 | `GET /api/v6/dex/balance/total-value-by-address` | OKX 唯一来源 |
| 全部 Token 余额 | `GET /api/v6/dex/balance/all-token-balances-by-address` | OKX 唯一来源 |
| 指定 Token/原生币余额 | `POST /api/v6/dex/balance/token-balances-by-address` | OKX 唯一来源 |
| 当前价格和行情 | `/api/v6/dex/market/*` | OKX 唯一来源，保留现有领域模型 |
| 历史价格和 K 线 | `/api/v6/dex/index/historical-price`、`/api/v6/dex/market/candles` | OKX 唯一来源 |
| Gas Limit | `POST /api/v6/dex/pre-transaction/gas-limit` | OKX 预估，记录来源 |
| 交易模拟 | `POST /api/v6/dex/pre-transaction/simulate` | OKX 预估，失败时阻止签名；不可用时返回警告 |
| DEX 历史 | `GET /api/v6/dex/market/portfolio/dex-history` | 只用于 DEX 历史，不冒充全链交易历史 |
| Explorer 交易详情/历史 | OKX Explorer Transaction API | 仅使用官方确认的 endpoint 和链覆盖 |

### 保留的非 OKX 能力

- Bridgers：资产目录、报价、准备、广播登记和订单状态。
- OmniBridge：资产目录、报价、准备、广播登记和订单状态。
- 浏览器钱包：账户连接、用户确认、签名和 `eth_sendTransaction`。
- 最小执行观察层：Allowance、Nonce、EIP-1559 费用、Approval receipt，以及
  浏览器钱包交易的 pending/confirmed/failed/dropped 判断。
- 模型 API、SSE、SQLite/LangGraph checkpoint 和应用认证：这些不是钱包数据
  Provider，不迁移到 OKX。

OKX 的后端 `broadcast-transaction` 接口需要完整的 signed transaction。当前
应用坚持非托管边界，不将原始签名交易交给后端，也不强制用户使用 OKX Wallet
浏览器插件，因此该接口不纳入本次默认广播路径。

## 架构

```text
Agent / REST / Demo
        |
        v
Normalized wallet services
  |                 |                    |
  v                 v                    v
OKX data plane   Execution observer   Swap providers
balances         allowance/receipt    Bridgers
prices           nonce/fee/status     OmniBridge
explorer
pre-transaction

Browser wallet
  -> user confirmation
  -> local signing
  -> eth_sendTransaction
  -> submit tx hash
```

Graph 和 REST API 只依赖领域模型，不暴露 OKX 原始响应。OKX 认证 header、项目
凭据和 signed transaction 不进入 graph state、checkpoint、SSE、Demo 调试数据
或错误消息。

## 组件变更

### OKX wallet/data adapter

扩展现有 `OkxWalletAdapter`，提供以下领域方法：

- `get_total_value`
- `get_token_balances`
- `get_specific_balances`
- `get_transaction_history`
- `get_transaction_detail`
- `get_transaction_status`
- `estimate_gas_limit`
- `simulate_transaction`

Explorer 方法必须按链能力和 OKX 返回的状态字段做规范化。DEX history 只标记
为 `history_kind= dex`；不能把缺少普通转账的结果宣称为完整 transaction history。

### Execution observer

将当前直接使用 `runtime.chains` 的执行检查收敛到窄接口，例如：

- `get_allowance(token, owner, spender)`
- `get_transaction_receipt(tx_hash)`
- `get_transaction(tx_hash)`
- `get_transaction_count(address)`
- `estimate_fee(transaction)`

该接口内部暂时由现有 EVM RPC adapter 实现。它不是钱包数据源，也不负责余额、
价格或资产组合查询。以后若 OKX 提供语义等价且覆盖足够的接口，只替换 observer
实现，不修改 Graph/API 契约。

### API 路由

- `/v1/wallet/{address}/balances`、`/portfolio`、`/total-value` 改为 OKX 数据源。
- `/v1/prices/*` 继续使用 OKX price provider。
- `/v1/wallet/{address}/transactions` 优先返回 OKX Explorer 规范化数据，并在
  响应中标注 `source=okx` 和 `history_kind`。
- `/v1/transactions/{chain}/{tx_hash}` 优先使用 OKX 交易详情/状态数据；对于
  pending 或 OKX 不支持的链，返回结构化 `OKX_CAPABILITY_UNAVAILABLE`，不伪造
  confirmed/failed。
- Swap 的 `/broadcast` 仍接收浏览器钱包返回的 hash，并通过 execution observer
  判断是否可见后再调用 Bridgers/OmniBridge 的登记接口。

现有响应字段保持兼容；新增字段只用于来源、历史类型、观察时间和结构化错误。

## 数据流和缓存

- 余额和总资产：短 TTL 缓存，key 包含 address、chain indexes、asset type 和
  risk-token filter。
- 当前价格：使用现有 OKX TTL 和请求合并机制。
- 历史、K 线、Explorer history：key 包含完整 query、cursor 和 limit；只缓存
  成功响应。
- 交易详情和状态：默认不长时间缓存；pending 可使用极短 TTL，confirmed/failed
  可按 tx hash 缓存一小段时间。
- Allowance、receipt、nonce、fee：不复用资产缓存；每次签名/继续前重新读取。

缓存失败只影响性能，不改变安全检查结果。错误响应不能写入成功缓存。

## 错误和降级

- OKX 认证、限流、链不支持和响应格式错误转换为稳定内部错误码。
- OKX 余额/价格/Explorer 失败不会回退到 CoinGecko；当前接口明确返回
  `source_error` 或 `unavailable`。
- Simulation 不可用是 warning；明确模拟失败是 blocking error。
- Execution observer 不可用时，不允许把交易报告为已确认，也不启动 Provider
  订单登记；状态返回 `unknown` 或 `not_propagated`。
- Bridgers/OmniBridge 的错误保持各自 Provider 错误语义，不包装成 OKX 错误。

## 分阶段实施

1. **OKX 数据源切换**：余额、Portfolio、总资产和所有价格接口去掉旧数据源，
   增加来源字段和缓存测试。
2. **OKX Explorer 查询**：接入官方确认的交易历史、交易详情和状态 endpoint，
   完成链覆盖校验、字段规范化和 API 回归测试。
3. **执行观察隔离**：把 Graph/API 中散落的 RPC 调用收敛到 observer 接口，确保
   Swap allowance、approval、nonce、fee、receipt 和广播可见性行为不变。
4. **Demo 与文档清理**：展示 OKX 来源和 history kind，清除 CoinGecko 旧说明，
   保持 Bridgers/OmniBridge 报价和订单调试数据。

本次不接入 OKX DEX Trade API，不把 OKX 作为 Swap Provider，也不改变浏览器钱包
签名流程。

## 验收标准

1. 钱包余额、Portfolio、总资产和价格请求只产生 OKX 请求。
2. CoinGecko 模块、配置和运行时引用全部移除，旧测试同步删除或改为 OKX 测试。
3. Bridgers 和 OmniBridge 的 quote/prepare/register/status 测试继续通过。
4. 交易历史、详情和状态响应包含 `source=okx`，并正确表示不支持链或 pending。
5. Allowance、Approval receipt、Nonce、费用和广播可见性测试行为不回归。
6. OKX credentials、签名 header 和 signed transaction 不出现在 API、SSE、日志或
   Demo 调试面板。
7. 全量单元测试、API 测试和 Demo 测试通过；网络集成测试默认跳过。

