# Figma 设计卡片 → App 接入手册

> 面向 **App 客户端**：每一张设计稿里的卡片，App 该从后端响应的哪个字段取数、
> 怎么渲染、点按后怎么回传，以及对齐状态。
>
> 配套文档：
> - [`app-integration.md`](app-integration.md) —— 接口/时序/会话/签名广播的完整接入契约。
> - [`figma-ai-assistant-alignment.md`](figma-ai-assistant-alignment.md) —— 设计 → 能力对齐与差距清单。
>
> 本文件只讲「卡片怎么接」，两边不互相复制字段契约。

## 0. 范围与来源

- 设计文件：`Cat Wallet v1.6.0`，fileKey `2N2sTwtjY3OznWq8m9zpmR`。
- 画布：`32786:19122`（"v1.6.9 AI 助手"），主分区 `33575:21647`
  （"AI 助手功能入口、文字语音输入、资产查询-交互- v1.6.9"）。
- 另有一页 **v1.7.0 AI 助手**（`34304:20199`）更新，**待产品确认以哪版为准**；
  下文所有节点链接均可直接点开定位到 Figma。
- 数据一律来自服务端响应；服务端只给数据，不生成 UI。

## 1. 卡片总览

| # | 卡片 | 设计节点 | 触发 | 数据来源（`response.kind` / 字段） | 对齐 |
| --- | --- | --- | --- | --- | --- |
| 1 | 空态 + 快捷胶囊行 | [`33715:148396`](https://www.figma.com/design/2N2sTwtjY3OznWq8m9zpmR/Cat-Wallet-v1.6.0?node-id=33715-148396) | 首次进入、无会话 | App 静态文案 + 5 个预设问题 | — |
| 2 | 余额 / 单链资产 | — | 查某链/某币余额 | `wallet_query` | ✅ |
| 3 | 多链资产总览卡 | 资产查询屏（分区内） | 「我有什么资产？」 | `portfolio_query` | ⚠️ 缺单价/空态 |
| 4 | 价格 / 行情卡 | — | 问价格/行情/历史 | `price_query` / `asset_value` / `market_details` / `price_history` | ✅ |
| 5 | 交易记录卡 | [`33715:146539`](https://www.figma.com/design/2N2sTwtjY3OznWq8m9zpmR/Cat-Wallet-v1.6.0?node-id=33715-146539)（屏 [`33715:146533`](https://www.figma.com/design/2N2sTwtjY3OznWq8m9zpmR/Cat-Wallet-v1.6.0?node-id=33715-146533)） | 「我最近有哪些交易？」 | `transaction_history` | ✅ |
| 6 | 交易状态 / 失败解释页 | 分区 [`33617:39513`](https://www.figma.com/design/2N2sTwtjY3OznWq8m9zpmR/Cat-Wallet-v1.6.0?node-id=33617-39513) | 详情页「解释失败原因？」 | `transaction_status` | ⚠️ 缺 failure_reason/ETA |
| 7 | 收款地址卡 / 支持链卡 | [`33715:146203`](https://www.figma.com/design/2N2sTwtjY3OznWq8m9zpmR/Cat-Wallet-v1.6.0?node-id=33715-146203) | 「获取我的收款地址」 | `receive_address` | ✅ |
| 8 | 地址输入卡（转账 step 1） | [`33692:37951`](https://www.figma.com/design/2N2sTwtjY3OznWq8m9zpmR/Cat-Wallet-v1.6.0?node-id=33692-37951) | 缺收款地址 | `suggestions`（输入地址/转给联系人/先看看余额）+ `client_action.address_input` | ✅ |
| 9 | 链选择卡（转账 step 2） | [`33692:48836`](https://www.figma.com/design/2N2sTwtjY3OznWq8m9zpmR/Cat-Wallet-v1.6.0?node-id=33692-48836) | 缺网络 | `suggestions`（`type:"chain"`，当前网络第一） | ✅ |
| 10 | 金额输入卡（转账 step 3） | [`33692:37984`](https://www.figma.com/design/2N2sTwtjY3OznWq8m9zpmR/Cat-Wallet-v1.6.0?node-id=33692-37984) | 缺金额 | `suggestions`（`type:"amount"` 25/50/75/100%）+ `open_amount_keypad` | ✅ |
| 11 | 风险提示卡 | [`33692:48838`](https://www.figma.com/design/2N2sTwtjY3OznWq8m9zpmR/Cat-Wallet-v1.6.0?node-id=33692-48838) | 转账/兑换预检查 | `preflight.risk` | ✅ |
| 12 | 报价列表卡 | 兑换流程 | 「我要兑换」→ 拿到报价 | `swap_quote`（`react_results` 里 `get_swap_quote`） | ✅ |
| 13 | 兑换确认卡 | 兑换流程 | 选中报价后 | `confirmation_required`（`confirmation_state`） | ✅ |
| 14 | 授权卡（Approve） | 兑换流程 | 需要代币授权 | `approval_required`（`approval_transaction`） | ✅ |
| 15 | 未签名交易卡 | 兑换/转账流程 | 确认通过后 | `transfer_prepare` / `swap_prepare`（`pending_transaction` + `preflight`） | ✅ |
| 16 | 兑换状态卡 | 兑换流程 | 广播后查询 | `swap_status`（`broadcast_status`） | ✅ |
| 17 | 发送/兑换详情页入口默认问题 | 分区 [`33617:39513`](https://www.figma.com/design/2N2sTwtjY3OznWq8m9zpmR/Cat-Wallet-v1.6.0?node-id=33617-39513) | 详情页「问问 Cat Wallet AI」 | `metadata` 带交易上下文 | ⚠️ 需 App 传上下文 |
| 18 | 语音输入态 | 分区 [`33575:21658`](https://www.figma.com/design/2N2sTwtjY3OznWq8m9zpmR/Cat-Wallet-v1.6.0?node-id=33575-21658) | 长按说话 | 输入模式，非卡片 | ⚠️ 待产品定 STT 归属 |

## 2. 通用机制

### 2.1 SSE 事件流

`POST /v1/agent/turn` 建 run，`GET /v1/agent/stream/{run_id}` 消费 SSE（支持
`last_event_id` 断线续传）。事件：

- `progress` —— 仅加载文案（`message` / `elapsed_ms`）。
- `update` / `response` —— 中间状态；App 只渲染 `response` 帧和终态。
- `model` —— 模型追踪（调试用），非渲染数据。
- `complete` / `action_required` / `error` —— 终态，卡片数据以**终态**为准。

App 只依赖 `complete`（或 `action_required`）帧里的 `state.response` 渲染卡片；
历史消息（含 suggestions）通过会话接口/`conversation_history` 恢复。

### 2.2 `response` 结构

```jsonc
{
  "kind": "wallet_query | portfolio_query | price_query | asset_value | market_details |\n"
          "price_history | transaction_history | transaction_status | receive_address |\n"
          "transfer_prepare | swap_quote | swap_prepare | swap_status | confirmation_required |\n"
          "approval_required | clarification | react | error",
  "message": "给用户看的文本（Markdown）",
  "suggestions": [ /* 快捷回复，见 2.3 */ ],
  "client_action": { /* App 侧动作，见 2.4 */ },
  // 各 kind 专属数据字段见第 3 节
}
```

### 2.3 快捷回复 `response.suggestions[]`

| 字段 | 说明 |
| --- | --- |
| `label` | 胶囊显示文案 |
| `message` | 点按后作为**用户消息**回传的文本 |
| `type` | `chain` / `symbol` / `amount` / `address` / `token` / `action` |
| `value` | 回填值；`action` 类型时为动作名 |
| `data` | 槽位数据，建议点按后**原样**放进 `metadata.suggestion_data` 回传 |

- `type:"action"` 的胶囊是动作，不要当参数提交；`value`：`input_address` /
  `pick_contact` / `show_balance` / `input_amount`（如返回）。
- `type:"amount"` 的 `value` 可能是 `25%` 比例，App 用**自己持有的余额**换算出数量再提交；
  `data` 带 `chain` / `symbol` / `token_address` / `decimals` 用于定位资产。
- `type:"chain"` 的 `value` 即链名，`data.chain` 相同；第一个胶囊是**钱包当前网络**。

### 2.4 客户端动作 `response.client_action`

```jsonc
{ "type": "open_contacts | scan_qr | open_amount_keypad | address_input",
  "reason": "可选",
  "slots": { "chain": "...", "symbol": "..." } }
```

App 收到后弹出自己的界面；用户完成后的结果作为**下一轮** `turn` 输入回传
（文本，或 `metadata.suggestion_data`）。未知 `type` 服务端不会下发
（白名单校验，非法取值返回 `CLIENT_ACTION_UNSUPPORTED`）。

## 3. 每张卡片的 App 接入

### 3.1 空态 + 快捷胶囊行

- 触发：无历史会话、App 进入对话面板。
- 数据：App 静态文案（"你好，有什么可以帮助你？"）+ 5 个预设问题
  （我有什么资产？／我最近有哪些交易？／转一笔帐给朋友／获取我的收款地址／我要兑换）。
- 点按：把预设问题当用户消息发给 `/v1/agent/turn`。

### 3.2 余额 / 单链资产卡（`wallet_query`）

- 触发：查某链/某币余额，或「所有链总余额」。
- 数据：`response.wallet`，资产行
  `asset{chain,symbol,decimals,address} / amount / amount_raw / usd_value / addresses`，
  附 `network_status` 说明查询范围与缺失。
- 渲染：逐行「币图标 + 符号 + 数量 + USD 估值」；极小非零余额显示 `<0.01`；
  未查询的链不能当零余额展示。

### 3.3 多链资产总览卡（`portfolio_query`）

- 触发：「我有什么资产？」。
- 数据：`response.portfolio`：
  `total_usd_value`（大字总额）、`assets[]`（同 3.2 资产行）、`price_status`、
  `price_snapshots[]`（单价快照）、`source`。
- 渲染：标题「多链资产总览」+ 总额；逐行「名称（链）+ **单价** + 数量 + 估值」。
- ⚠️ **差距**：后端资产行目前**没有 `unit_price`**，单价在 `price_snapshots` 里，
  App 需按 `(chain, chain_id, symbol, address)` join；建议后端把 `unit_price` 补到
  每行，并给空态标记（`total_usd_value == null` 时渲染「暂无资产数据」）。

### 3.4 价格 / 行情卡（`price_query` / `asset_value` / `market_details` / `price_history`）

- 触发：问实时价、历史价、行情详情、单币估值。
- 数据：`response` 即结果（各 kind 各自的字段；Demo 直接 JSON 展示，App 按设计排版）。
- 对齐：✅（字段以接口返回为准，未在 v1.6.9 对话区单列设计稿卡片）。

### 3.5 交易记录卡（`transaction_history`）

- 触发：「我最近有哪些交易？」、筛选只看支出。
- 数据：`items[]`（`direction` send/receive/swap、`counterparty`、`amount_raw` +
  `decimals` + `symbol`、`chain`、`status`、`occurred_at`）、`total`、`summary`、
  `filter`、`partial_errors`。
- 渲染：标题「最近交易记录 共 N 笔」；逐行 方向/对手方/金额/链/状态/时间；
  `raw_only` 为 true 的条目金额是代币最小单位，**原样展示不要换算**。
- 对齐：✅（`data-card-kind="history"` 已实现）。

### 3.6 交易状态 / 失败解释页（`transaction_status`）

- 触发：详情页点「解释失败原因？」/「为什么还在处理中？」。
- 数据：`response` = `{chain, tx_hash, status(pending/confirmed/failed/unknown),
  message, source, transaction(详情)}`；REST 版 `GET /v1/transactions/{chain}/{tx_hash}`。
- App 接入要点：从详情页进入对话时，把 `tx_hash` 和链放进
  **`metadata.transaction_query`**（服务端已支持，模型会先查证再回答）。
- ⚠️ **差距**：链级 `TransactionDetail` **没有 `failure_reason` 和预计到账（ETA）**；
  `failure_reason` 目前只在兑换订单状态（`NormalizedOrderStatus`）上。
  设计稿的「交易完成/失败 + 预计耗时 30.22 分钟」需要补字段。

### 3.7 收款地址卡 / 支持链卡（`receive_address`）

- 触发：「获取我的收款地址」、追问「你还支持哪些链？」。
- 数据：`groups[]`（EVM 全链合并一张卡 + `label` + `address` + `chains[]` +
  多于 1 个地址时 `addresses[]`；BTC/TRON 各自成组）、`supported_chains`、`message`。
- 渲染：120×120 二维码（App 按地址生成）+ 地址 + 复制按钮 + 支持链标签。
- 对齐：✅（`data-card-kind="receive"` 已实现；二维码客户端生成）。

### 3.8 地址输入卡（转账 step 1，`transfer_recipient` 缺失）

- 设计节点：[`33692:37951`](https://www.figma.com/design/2N2sTwtjY3OznWq8m9zpmR/Cat-Wallet-v1.6.0?node-id=33692-37951)。
- 数据：`suggestions` 三连胶囊，`value` 依次 `input_address` / `pick_contact` /
  `show_balance`；「输入地址」的 `data` = `{intent:"transfer", chain, symbol, amount}`。
- App 接入：
  - 点「输入地址」→ 保存 `data`（转账上下文），弹出**自己的地址输入框**；
    用户提交地址后回传 `metadata.suggestion_data={...data, recipient:"<地址>"}`。
  - 点「转给联系人」→ App 通讯录；点「先看看余额」→ 发 `suggestion.message`。
  - 若后端下发 `client_action.address_input`，同样弹出地址输入框（`slots.chain` 即要绑定的网络）。
- 对齐：✅（`data` 绝不预填钱包自身地址，避免默认自转）。

### 3.9 链选择卡（转账 step 2，`transfer_chain` 缺失）

- 设计节点：[`33692:48836`](https://www.figma.com/design/2N2sTwtjY3OznWq8m9zpmR/Cat-Wallet-v1.6.0?node-id=33692-48836)。
- 数据：`suggestions` 全部 `type:"chain"`，`value`/`data.chain` = 链名，
  **第一个是钱包当前网络**，其余按支持列表顺序。
- App 接入：渲染「支持的链」+ 链选项胶囊；点按回传 `data`（含所选 `chain`）。
- 对齐：✅（纯链选项、当前网络置顶）。

### 3.10 金额输入卡（转账 step 3，`transfer_amount` 缺失）

- 设计节点：[`33692:37984`](https://www.figma.com/design/2N2sTwtjY3OznWq8m9zpmR/Cat-Wallet-v1.6.0?node-id=33692-37984)。
- 数据：`suggestions` 为 `type:"amount"` 的 `25%`/`50%`/`75%`/`100%`；
  `data` 带 `chain` / `symbol` / `token_address` / `decimals`（钱包可定位资产时）。
- App 接入：
  - 卡片包含「资产与余额」行 + 金额输入框 + 25/50/75/100% + 余额不足 NoticeBar +
    取消/确认；余额由 **App 持有**，百分比用余额换算。
  - 用户点百分比或输入金额后回传 `metadata.suggestion_data`（含 `amount`）。
  - 若后端下发 `client_action.open_amount_keypad`，弹金额键盘输入。
- 对齐：✅（资产身份已随 `data` 下发）。

### 3.11 风险提示卡（`preflight.risk`）

- 设计节点：[`33692:48838`](https://www.figma.com/design/2N2sTwtjY3OznWq8m9zpmR/Cat-Wallet-v1.6.0?node-id=33692-48838)。
- 数据：`preflight.risk = {level(danger/warning), code, message}`；出现在
  `transfer_prepare` / `swap_prepare` / `error` 的 `preflight` 里。
- App 接入：`danger` 阻断签名前准备；`warning` 展示后确认继续。
- 对齐：✅（`data-card-kind="risk"` 已实现）。

### 3.12 报价列表卡（`swap_quote`）

- 触发：「我要兑换」→ 模型调 `get_swap_quote`。
- 数据：`react_results` 里 `tool=="get_swap_quote"` 且 `result.ok==true` 的
  `result.quotes[]`，每项含 `provider` / `provider_reference` / `input_amount` /
  `expected_output` / `source_asset{symbol}` / `destination_asset{symbol}` 等。
- App 接入：渲染报价行；点「选择此报价」调 `POST /v1/swap/{session_id}/select-quote`
  （body 带 `provider_reference`）；`selected_quote` 回显。
- 对齐：✅。

### 3.13 兑换确认卡（`confirmation_required`）

- 数据：`confirmation_state` = `{action, status:"requested", requested_at, expires_at,
  summary{input_amount, source_asset, expected_output, destination_asset,
  recipient_address, provider}, reason, task_id, task_revision, payload_hash}`。
- App 接入：确认/取消按钮 → `POST /v1/swap/{session_id}/confirm`
  （body `{approved: true|false}`）；`expires_at` 过期则刷新状态。
- 对齐：✅（`data-card-kind="confirmation"`）。

### 3.14 授权卡（`approval_required`）

- 数据：`approval_transaction`（EVM `{chain, from, to, data, value, gas...}`）+ `stage:"approval_required"`。
- App 接入：把 `approval_transaction` 交给钱包签名，回传
  `POST /v1/swap/{session_id}/approve-broadcast`（body `{approve_tx_hash, chain, user_id}`）。
- 对齐：✅（`data-card-kind="approval"`）。

### 3.15 未签名交易卡（`transfer_prepare` / `swap_prepare`）

- 数据：`pending_transaction`（未签名交易：`chain` / `from` / `to` / `value` / `data` /
  `asset` 等，BTC 为 PSBT 材料）+ `preflight`。
- App 接入：展示待签名交易/PSBT → 钱包签名 → 回传
  `POST /v1/transfer/{session_id}/broadcast` 或 `POST /v1/swap/{session_id}/broadcast`
  （body `{tx_hash, chain, user_id}`）。服务端只收 hash + chain，绝不收签名材料。
- 对齐：✅（`data-card-kind="transaction"` / `bitcoin-signing`）。

### 3.16 兑换状态卡（`swap_status`）

- 数据：`broadcast_status`（`broadcast_pending` / `confirmed` / `failed` 等）+
  `tx_hash` + `message`；也见于 `GET /v1/swap/{session_id}`。
- App 接入：广播后轮询展示「交易中…／完成／失败」。
- 对齐：✅（`data-card-kind="status"`）。

### 3.17 发送/兑换详情页入口默认问题（非卡片）

- 设计节点：分区 [`33617:39513`](https://www.figma.com/design/2N2sTwtjY3OznWq8m9zpmR/Cat-Wallet-v1.6.0?node-id=33617-39513)。
- 需求：详情页「问问 Cat Wallet AI」默认问「解释失败原因？」/「为什么还在处理中？」。
- App 接入：进入对话时把交易上下文放进 **`metadata.transaction_query`**
  （`{chain, tx_hash}`；订单上下文扩展 `metadata` 待后端支持）。
- ⚠️ 差距：订单级 ETA / 失败原因字段待补（见 3.6）。

## 4. 未对齐 / 待办汇总

按优先级：

1. **多链资产总览卡**：后端资产行补 `unit_price` + 空态标记（`total_usd_value == null`），
   App 才能渲染「名称（链）+ 单价 + 数量 + 估值」与「暂无资产数据」。
2. **交易状态/失败解释**：链级 `TransactionDetail` 补 `failure_reason`；兑换订单补
   预计到账（ETA，`expires_at` 不等于 ETA）；详情页进入对话需 App 传
   `metadata.transaction_query`。
3. **入口默认问题**：依赖第 2 项字段 + App 传上下文。
4. **语音输入**：先定 STT 归属（客户端转写 vs 服务端识别），再谈接法。
5. **版本确认**：v1.6.9 与 v1.7.0 设计页以哪版为准（涉及卡片字段增减）。

## 5. 参考实现

- `demo-next/app/page.tsx`：`applyState` / `renderConversationCards` —— 每种
  `data-card-kind` 的渲染与交互（chooseSuggestion / selectQuote / confirm / broadcast）。
- `demo-next/lib/agent-utils.ts`：`extractConversationArtifacts` —— 从 `response` 取每张
  卡片数据的权威映射；`buildWalletAppContext` —— 每轮请求应携带的钱包上下文。

## 6. 安全边界提醒

广播只收 `tx_hash` + `chain`；签名材料、私钥、seed、PSBT 永远只留在客户端，
不回传、不落日志、不进对话。`metadata` 里的敏感字段服务端会脱敏。
