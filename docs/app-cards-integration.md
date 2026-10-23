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
| `type` | `chain` / `symbol` / `amount` / `address` / `token` / `swap_pair` / `action` |
| `value` | 回填值；`action` 类型时为动作名 |
| `data` | 槽位数据，建议点按后**原样**放进 `metadata.suggestion_data` 回传 |

- `type:"action"` 的胶囊是动作，不要当参数提交；`value`：`input_address` /
  `pick_contact` / `show_balance` / `input_amount` / `swap_custom` / `swap_cross_chain`
  （如返回）。
- `type:"amount"` 的 `value` 可能是 `25%` 比例，App 用**自己持有的余额**换算出数量再提交；
  `data` 带 `chain` / `symbol` / `token_address` / `decimals` 用于定位资产。
- `type:"chain"` 的 `value` 即链名，`data.chain` 相同；第一个胶囊是**钱包当前网络**。
- `type:"swap_pair"` 是兑换意图卡（设计 [`33715:134319`](https://www.figma.com/design/2N2sTwtjY3OznWq8m9zpmR/Cat-Wallet-v1.6.0?node-id=33715-134319)）
  的推荐交易对胶囊：`label` 如 `ETH → USDT`，`data.source` / `data.destination` 各带
  `chain` / `symbol`（余额来源时含 `address` / `decimals` / `logo_url`）。点按把
  `message`（如 `把 ETH 换成 USDT`）作为下一轮用户消息回传即可继续兑换。
- 需要画图标的胶囊（链 / 币 / 金额）额外带顶层 `logo_url`（币图标）与
  `chain_logo_url`（链角标），约定见 §2.5；这两个字段不要在点按时回传。

### 2.4 客户端动作 `response.client_action`

```jsonc
{ "type": "open_contacts | scan_qr | open_amount_keypad | address_input | select_btc_address",
  "reason": "可选",
  "slots": { "chain": "...", "symbol": "..." } }
```

App 收到后弹出自己的界面；用户完成后的结果作为**下一轮** `turn` 输入回传
（文本，或 `metadata.suggestion_data`）。未知 `type` 服务端不会下发
（白名单校验，非法取值返回 `CLIENT_ACTION_UNSUPPORTED`）。

兑换金额卡（Figma [`33715:134548`](https://www.figma.com/design/2N2sTwtjY3OznWq8m9zpmR/Cat-Wallet-v1.6.0?node-id=33715-134548)）
的 `open_amount_keypad` 额外带 `slots.intent:"swap"`（转账不带）；点按推荐币对后
再弹键盘时，`slots` 还会带上来源币的 `token_address` / `decimals`（从被点币对的
`source` 复制），供 App 精确定位资产。App 只消费**顶层** `client_action`。

**BTC 兑换出金地址**：`select_btc_address`（`slots.chain:"BTC"`、`slots.symbol:"BTC"`，
后端会尽量带 `slots.addresses:[{address, balance?, balance_raw?}]`）。BTC 作为兑换
来源链且未明确出金地址时，后端会先下发它让用户在多个 BTC 地址中选一个（出金
地址只能选一个，provider 的 fromAddress 是单个地址）；选中后把该地址回传
`metadata.suggestion_data = { intent:"swap", sender_address:"bc1..." }`。`sender_address`
未下发时不会进入模型提示词白名单，避免模型把它当普通槽位猜值。

### 2.5 币 / 链 logo 字段约定

所有带资产的响应都遵循同一套字段，App 不需要自己拼链名、也不需要内置链表：

| 字段 | 含义 |
| --- | --- |
| `asset.logo_url` | **币图标**。provider（OKX / Bridgers / OmniBridge）给的就用它；原生币没有 provider 图，回落成链 logo；两者都没有时为 `null`，App 画占位。 |
| `chain_logo_url` | **链角标**，与所在行/对象同级。未知链为 `null`。 |
| 行级 `logo_url` | 交易流水行的便捷字段，等于该行的币图标（与 `asset.logo_url` 同值），只为少一层嵌套。 |

覆盖位置：

| 出口 | 携带位置 |
| --- | --- |
| `wallet_query` / `portfolio_query` / `GET /v1/wallet/{address}/balances` / `/portfolio` | 资产行 `asset.logo_url` + `chain_logo_url` |
| `transaction_history` / `GET /v1/wallet/{address}/transactions` | 行级 `logo_url` + `chain_logo_url` |
| `swap_quote` 报价、`confirmation_required` 的 `summary` | `source_asset.logo_url` / `destination_asset.logo_url` + `source_chain_logo_url` / `destination_chain_logo_url` |
| `approval_required` | `approval_transaction.token.logo_url` + `chain_logo_url` |
| `transfer_prepare` / `swap_prepare`（含 `POST /v1/transfer/{session_id}/prepare`、`GET /v1/swap/{session_id}`） | `asset`（被转/被换出的资产，带 `logo_url`）+ `chain_logo_url`，在 `pending_transaction` **之外** |
| `swap_status`（含广播后轮询返回的每一帧） | `chain` + `chain_logo_url` + `asset` / `destination_asset` |
| `transaction_status` / `GET /v1/transactions/{chain}/{tx_hash}` | 顶层 `logo_url` + `chain_logo_url` |
| `gas_check` / `GET /v1/wallet/{address}/gas`、`/v1/wallet/{address}/fees` | 原生币 `logo_url` + `chain_logo_url` |
| `price_query` / `market_details` / `asset_value` / `GET /v1/prices/*` | 价格行 `asset.logo_url` + `chain_logo_url`；`/prices/history`、`/candles` 用同级 `asset` + `chain_logo_url` 说明标的是谁 |
| `asset_discovery` / `GET /v1/assets` | 资产行自带 `logo_url` + `chain_logo_url` |
| `receive_address` | `chain_logo_urls`（`{链名: url}`）对应 `supported_chains`；设计稿这里是文字 chip，做图标版时可直接取 |
| `response.suggestions[]`（转账链/币/金额胶囊） | 胶囊顶层 `logo_url` + `chain_logo_url` |
| `response.token_candidates[]`（同名 Token 选择卡） | 每个候选自带 `logo_url` + `chain_logo_url` |

链 logo 来自 Trust Wallet assets 的 CDN（`raw.githubusercontent.com/trustwallet/assets`），
因此**不占用我方带宽、也不需要 App 内置映射**。已知链覆盖 ETH / BSC / Base / Arbitrum /
Optimism / Polygon / TRON / BTC / Solana / Celo / Avalanche（slug `avalanchec`）/ MegaETH。
设计稿收款卡还列了 Robinhood，但该链在 Trust Wallet 没有条目，服务端返回 `null`，
App 需按占位处理（不要用别的链 logo 顶替）。

这些字段都是**展示用**：服务端不校验 App 是否使用，App 也不要把它们回填进
`metadata.suggestion_data` 当作槽位（回传了会被忽略）。签名材料、私钥、seed
仍然只留在客户端。

## 3. 每张卡片的 App 接入

### 3.1 空态 + 快捷胶囊行

- 触发：无历史会话、App 进入对话面板。
- 数据：App 静态文案（"你好，有什么可以帮助你？"）+ 5 个预设问题
  （我有什么资产？／我最近有哪些交易？／转一笔帐给朋友／获取我的收款地址／我要兑换）。
- 点按：把预设问题当用户消息发给 `/v1/agent/turn`。

### 3.2 余额 / 单链资产卡（`wallet_query`）

- 触发：查某链/某币余额，或「所有链总余额」。
- 数据：`response.wallet`，资产行
  `asset{chain,symbol,decimals,address,logo_url} / chain_logo_url / amount / amount_raw /
  usd_value / addresses`，
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
  `decimals` + `symbol`、`chain`、`status`、`occurred_at`、行级 `logo_url` +
  `chain_logo_url`）、`total`、`summary`、`filter`、`partial_errors`。
- 渲染：标题「最近交易记录 共 N 笔」；逐行 方向/对手方/金额/链/状态/时间；
  `raw_only` 为 true 的条目金额是代币最小单位，**原样展示不要换算**。
- 对齐：✅（`data-card-kind="history"` 已实现）。

### 3.6 交易状态 / 失败解释页（`transaction_status`）

- 触发：详情页点「解释失败原因？」/「为什么还在处理中？」。
- 数据：`response` = `{chain, tx_hash, status(pending/confirmed/failed/unknown),
  message, source, failure_reason, eta, transaction(详情)}`；REST 版
  `GET /v1/transactions/{chain}/{tx_hash}` 同样返回顶层 `failure_reason` / `eta`。
- logo：顶层另有 `logo_url`（该笔交易代币的图标，原生币回落链 logo）+
  `chain_logo_url`，REST 版一致，见 §2.5。
- App 接入要点：从详情页进入对话时，把 `tx_hash` 和链放进
  **`metadata.transaction_query`**（服务端已支持，模型会先查证再回答）。
- ✅ 对齐：`failure_reason`（来源给出时）与 `eta`（来源给出时）已透出；
  来源没给原因/耗时时为 `null`，**不要**编造或显示固定值。

### 3.7 收款地址卡 / 支持链卡（`receive_address`）

- 触发：「获取我的收款地址」、追问「你还支持哪些链？」。
- 数据：`groups[]`（EVM 全链合并一张卡 + `label` + `address` + `chains[]` +
  多于 1 个地址时 `addresses[]`；BTC/TRON 各自成组）、`supported_chains`、`message`。
- logo：`chain_logo_urls`（`{链名: url}`）与 `supported_chains` 一一对应；v1.6.9
  这里是文字 chip，做图标版时用它。
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
  `data` 带 `chain` / `symbol` / `token_address` / `decimals`（钱包可定位资产时），
  以及后端同回合查询到的所选链可用余额 `balance` / `balance_raw` / `balance_usd`
  （v1.6.9 起，V169-IOS-018：金额卡百分比一律锚定**所选链**的余额，禁止用同
  symbol 的其他链余额兜底，如 ARBITRUM ETH 不得用主网 ETH）。
- 数据：胶囊顶层另有 `logo_url` / `chain_logo_url`，画「资产与余额」行的图标用；
  选币胶囊（`type:"symbol"`）同理，见 §2.5。
- App 接入：
  - 卡片包含「资产与余额」行 + 金额输入框 + 25/50/75/100% + 余额不足 NoticeBar +
    取消/确认；余额优先用后端下发的 `data.balance`（无则 App 本地持有），百分比用该链余额换算。
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
- 前置：用户只表达兑换意图、还没指定币对时，服务端先回**兑换意图推荐卡**
  （设计 [`33715:134319`](https://www.figma.com/design/2N2sTwtjY3OznWq8m9zpmR/Cat-Wallet-v1.6.0?node-id=33715-134319)）：
  `response.suggestions[]` 里带 `type:"swap_pair"` 的推荐交易对胶囊（按余额推荐）
  与 `type:"action"` 的 `swap_custom`（其他币对）/ `swap_cross_chain`（跨链换）入口；
  用户点按后把 `message` 回传，模型再用 `get_swap_quote` 出报价。
- 前置：用户已明确币对（如「用 USDT 换 ETH」）但**卖出币分布在多条网络**、来源链有歧义时，
  服务端先回**兑换链选择卡**（设计 [`33715:134430`](https://www.figma.com/design/2N2sTwtjY3OznWq8m9zpmR/Cat-Wallet-v1.6.0?node-id=33715-134430)）：
  `response.suggestions[]` 里带 `type:"chain"` 的链胶囊，每个 `data` 为
  `{intent:"swap", chain, symbol, token_address, decimals}`（与转账 §3.9 选链卡同款、
  但带 `intent:"swap"`）。用户点按后把 `message`（如 `用 ETH 网络的 USDT 换 ETH`）
  回传，App 会把该胶囊的 `data` 原样放进 `metadata.suggestion_data`（含已定链），
  服务端不再重复出选链卡，直接进报价。
- 数据：`react_results` 里 `tool=="get_swap_quote"` 且 `result.ok==true` 的
  `result.quotes[]`，每项含 `provider` / `provider_reference` / `input_amount` /
  `expected_output` / `source_asset{symbol}` / `destination_asset{symbol}` 等。
- logo：每个报价带 `source_chain_logo_url` / `destination_chain_logo_url`，
  资产图标在 `source_asset.logo_url` / `destination_asset.logo_url`，见 §2.5。
- App 接入：渲染报价行；点「选择此报价」调 `POST /v1/swap/{session_id}/select-quote`
  （body 带 `provider_reference`）；`selected_quote` 回显。
- 对齐：✅。

### 3.13 兑换确认卡（`confirmation_required`）

- 数据：`confirmation_state` = `{action, status:"requested", requested_at, expires_at,
  summary{input_amount, source_asset, expected_output, destination_asset,
  recipient_address, provider}, reason, task_id, task_revision, payload_hash}`。
- logo：`summary.source_asset.logo_url` / `summary.destination_asset.logo_url` +
  `summary.source_chain_logo_url` / `summary.destination_chain_logo_url`，见 §2.5。
- App 接入：确认/取消按钮 → `POST /v1/swap/{session_id}/confirm`
  （body `{approved: true|false}`）；`expires_at` 过期则刷新状态。
- 对齐：✅（`data-card-kind="confirmation"`）。

### 3.14 授权卡（`approval_required`）

- 数据：`approval_transaction`（EVM `{chain, from, to, data, value, gas...}`）+ `stage:"approval_required"`。
- logo：`approval_transaction.token.logo_url`（被授权代币的图标）+
  `approval_transaction.chain_logo_url`（链角标），见 §2.5。
- App 接入：把 `approval_transaction` 交给钱包签名，回传
  `POST /v1/swap/{session_id}/approve-broadcast`（body `{approve_tx_hash, chain, user_id}`）。
- 对齐：✅（`data-card-kind="approval"`）。

### 3.15 未签名交易卡（`transfer_prepare` / `swap_prepare`）

- 数据：`pending_transaction`（未签名交易：`chain` / `from` / `to` / `value` / `data` /
  `display` 等，BTC 为 PSBT 材料、TRON 为 `raw_data_hex`）+ `preflight`。
- logo：**不在** `pending_transaction` 里。响应顶层另给 `asset`（转账=被转资产、
  兑换=换出的来源资产，带 `logo_url`）与 `chain_logo_url`；`POST
  /v1/transfer/{session_id}/prepare`、`GET /v1/swap/{session_id}` 的会话投影里
  也有同样两个字段，见 §2.5。
- App 接入：展示待签名交易/PSBT → 钱包签名 → 回传
  `POST /v1/transfer/{session_id}/broadcast` 或 `POST /v1/swap/{session_id}/broadcast`
  （body `{tx_hash, chain, user_id}`）。服务端只收 hash + chain，绝不收签名材料。
- 对齐：✅（`data-card-kind="transaction"` / `bitcoin-signing`）。

### 3.16 兑换状态卡（`swap_status`）

- 数据：`broadcast_status`（`broadcast_pending` / `confirmed` / `failed` 等）+
  `tx_hash` + `message` + 顶层 `failure_reason` / `eta`（订单状态里的字段）；
  `GET /v1/swap/{session_id}` 的 `order_status` 里同样可读。
- logo：顶层 `chain` + `chain_logo_url` + `asset` / `destination_asset`（带
  `logo_url`），订单状态卡的币图标从这里取，见 §2.5。
- App 接入：广播后轮询展示「交易中…／完成／失败」。
- 对齐：✅（`data-card-kind="status"`）。

### 3.17 发送/兑换详情页入口默认问题（非卡片）

- 设计节点：分区 [`33617:39513`](https://www.figma.com/design/2N2sTwtjY3OznWq8m9zpmR/Cat-Wallet-v1.6.0?node-id=33617-39513)。
- 需求：详情页「问问 Cat Wallet AI」默认问「解释失败原因？」/「为什么还在处理中？」。
- App 接入：进入对话时把交易上下文放进 **`metadata.transaction_query`**
  （`{chain, tx_hash}`）；兑换订单页也可传 **`metadata.swap_query`**
  （`{provider_order_id | order_id | session_id, tx_hash}`），模型会先
  `query_swap_status` / `transaction_status` 查证再解释。
- ✅ 对齐：tx 与订单两种入口上下文都已支持。

## 4. 未对齐 / 待办汇总

按优先级：

1. ~~**多链资产总览卡**~~ 已完成：资产行 `unit_price` + `has_assets` 空态标记已透出。
2. ~~**交易状态/失败解释**~~ 已完成：`TransactionDetail` / `NormalizedOrderStatus` 补
   `failure_reason` 与 `eta`（来源给才填），`transaction_status` / `swap_status` /
   `/v1/transactions` 顶层透出；详情页入口支持 `metadata.transaction_query` /
   `metadata.swap_query`。
3. **语音输入**：先定 STT 归属（客户端转写 vs 服务端识别），再谈接法。
4. **版本确认**：v1.6.9 与 v1.7.0 设计页以哪版为准（涉及卡片字段增减）。
5. **非原生 Token 的图标来源**：目前只有 OKX / Bridgers / OmniBridge 三家 provider
   会带 `logo_url`；价格 provider 与 App 自己声明的余额没有图标，这些位置
   `asset.logo_url` 为 `null`。要全量图标需接入一个 token logo 数据源
   （例如 Trust Wallet assets 的合约路径），否则由 App 占位。
6. **Robinhood 链**：设计稿收款卡列了它，但 Trust Wallet 无该条目，服务端只能给
   `null`。若产品要求出图标，需要另找来源或让 App 内置。

## 5. 参考实现

- `demo-next/app/page.tsx`：`applyState` / `renderConversationCards` —— 每种
  `data-card-kind` 的渲染与交互（chooseSuggestion / selectQuote / confirm / broadcast）。
- `demo-next/lib/agent-utils.ts`：`extractConversationArtifacts` —— 从 `response` 取每张
  卡片数据的权威映射；`buildWalletAppContext` —— 每轮请求应携带的钱包上下文。

## 6. 安全边界提醒

广播只收 `tx_hash` + `chain`；签名材料、私钥、seed、PSBT 永远只留在客户端，
不回传、不落日志、不进对话。`metadata` 里的敏感字段服务端会脱敏。
