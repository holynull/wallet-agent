# Figma AI 助手设计对齐（v1.6.9）

对照来源：Figma `Cat Wallet v1.6.0`，fileKey `2N2sTwtjY3OznWq8m9zpmR`，
画布节点 [`32786:19122`](https://www.figma.com/design/2N2sTwtjY3OznWq8m9zpmR/Cat-Wallet-v1.6.0?node-id=32786-19122)（"v1.6.9 AI 助手"），主分区 [`33575:21647`](https://www.figma.com/design/2N2sTwtjY3OznWq8m9zpmR/Cat-Wallet-v1.6.0?node-id=33575-21647)
（"AI 助手功能入口、文字语音输入、资产查询-交互- v1.6.9"）。

阅读范围：该分区的结构与文案节点。分区内部分节点在读取时被截断，下文标注为「待核」
的条目标题有待补充确认。

本文只跟踪**设计 → 能力**的对齐与差距；卡片字段级的数据契约以
[app-integration.md](app-integration.md#56-对话卡片字段契约) §5.6 为准；
每张卡片怎么接（取数/渲染/回传）见 [app-cards-integration.md](app-cards-integration.md)，
两边不要互相复制。

## 一、设计稿要求的 Agent 能力

分区标题即设计者的意图：**功能入口 + 文字/语音输入 + 资产查询**。

### 1. 入口

- AI 对话入口出现在钱包首页（[`33575:21654`](https://www.figma.com/design/2N2sTwtjY3OznWq8m9zpmR/Cat-Wallet-v1.6.0?node-id=33575-21654) "ai对话入口、记录"）。
- 发送详情页与兑换详情页新增 AI 对话入口（[`33617:39513`](https://www.figma.com/design/2N2sTwtjY3OznWq8m9zpmR/Cat-Wallet-v1.6.0?node-id=33617-39513)
  "发送和兑换详情-新增ai对话入口"）。
- 历史记录入口与对话共用一块区域（[`33715:145845`](https://www.figma.com/design/2N2sTwtjY3OznWq8m9zpmR/Cat-Wallet-v1.6.0?node-id=33715-145845)），底部有「再来一笔」
  （`wallet_swap_bridge_details_button_make_another_transaction`）。
- 分区内两处标注 `钱包 --> PayAll`，疑为批量付款入口（待核）。

### 2. 对话面板

- 空态：头像 + "你好，有什么可以帮助你？"，下方 5 个快捷 chip
  （[`33715:148396`](https://www.figma.com/design/2N2sTwtjY3OznWq8m9zpmR/Cat-Wallet-v1.6.0?node-id=33715-148396)）：**我有什么资产？／我最近有哪些交易？／转一笔帐给朋友／
  获取我的收款地址／我要兑换**。
- 单一输入框 "发消息或按住说话..."（[`33715:148455`](https://www.figma.com/design/2N2sTwtjY3OznWq8m9zpmR/Cat-Wallet-v1.6.0?node-id=33715-148455)），文字与长按语音共用。
- 语音态：[`33715:148136`](https://www.figma.com/design/2N2sTwtjY3OznWq8m9zpmR/Cat-Wallet-v1.6.0?node-id=33715-148136) "AI助手-语音说话中"，并单列「语音输入流程」分区
  （[`33575:21658`](https://www.figma.com/design/2N2sTwtjY3OznWq8m9zpmR/Cat-Wallet-v1.6.0?node-id=33575-21658)）。
- 文字输入流程单列分区（[`33616:22196`](https://www.figma.com/design/2N2sTwtjY3OznWq8m9zpmR/Cat-Wallet-v1.6.0?node-id=33616-22196)）。

### 3. 历史记录

- 列表项 = **用户原话** + 相对时间（"7 分钟前" / "1 小时前" / 绝对日期）+ 右侧图标。
- 设计注释："此处搜索记录与其他地方的搜索记录互相独立" → 对话历史需要**独立搜索**。

### 4. 能力清单

| 能力 | 设计依据 |
| --- | --- |
| 资产查询 | 分区标题「资产查询」；快捷 chip "我有什么资产？" |
| 交易记录查询 | [`33715:146533`](https://www.figma.com/design/2N2sTwtjY3OznWq8m9zpmR/Cat-Wallet-v1.6.0?node-id=33715-146533) 卡片「最近交易记录 共 5 笔」，列出 发送/接收/兑换、时间、金额、状态；回复明确承诺"想看某笔详情或者**筛选只看支出**，跟我说一声" |
| 转账（对话式收集） | [`33692:37944`](https://www.figma.com/design/2N2sTwtjY3OznWq8m9zpmR/Cat-Wallet-v1.6.0?node-id=33692-37944)：先问地址 → 再问链 → 再问金额；快捷「输入地址／**转给联系人**／先看看余额」；金额 25%／50%／75%／100%；余额不足 NoticeBar；底部 取消／确认 |
| 收款地址 | 卡片 = 二维码 + 地址 + Copy + 支持链标签；回复"你的收款地址：0x… 👆 支持 EVM 链：… 直接转就行～"；追问"你还支持哪些链？"→ BTC／TRON |
| 兑换 | 快捷 chip "我要兑换"；回复"…你想换点什么，我帮你看" |
| 地址风险提示 | 转账流程内「地址风险提示」卡片 + 警告文案 |
| 链覆盖 | 收款卡片列出 Ethereum／Base／Arbitrum／Optimism／BNB Chain／Polygon 之外还有 **Avalanche、Robinhood、MegaETH、CELO**，文案称"支持所有 EVM 链" |

### 5. 交互注释（客户端侧）

- "复制地址成功后顶部有提示，复用接收里面的样式"。
- "选中后，按钮高亮。未选中的按钮置灰"。

## 二、与当前实现的对照

当前对话意图（`src/wallet_agent/graph/nodes.py` `_VALID_INTENTS`）：`portfolio_query`、
`gas_check`、`asset_discovery`、`price_query`、`market_details`、`price_history`、
`transaction_status`、`transfer`、`swap_quote/prepare/select/status/allowance`、
`wallet_query`、`clarification`、`unsupported`。
ReAct 工具：`wallet_balance`、`portfolio_snapshot`、`price_lookup`、`market_details`、
`price_history`、`asset_discovery`、`gas_estimate`、`transaction_status`、
`get_swap_quote`、`prepare_transfer`、`query_swap_status`。

| 设计要求 | 现状 | 结论 |
| --- | --- | --- |
| 文字输入 | `/v1/agent/turn` + SSE | 已有 |
| 资产查询 | `portfolio_query`；`GET /v1/wallet/{address}/portfolio` | 已有 |
| 兑换 | `swap_quote/prepare/select/status` 全链路 + 确认 | 已有 |
| 转账对话式收集 | `transfer` intent + slot filling + 逐项追问 | 已有 |
| 余额不足提示 | `INSUFFICIENT_BALANCE` 预检查 | 已有 |
| 历史记录列表 | 已补：每轮对话都会建 session，只读问答也会出现在历史里（原先只有兑换/转账类消息才建 session，只读问答在历史中不可见） | 已有 |
| 交易记录查询 | 已实现 `transaction_history` 工具：跨 App 声明的地址/网络取记录、按方向筛选、笔数汇总；`TransactionRecord` 补齐卡片字段 | 已有 |
| 地址风险提示 | 已实现确定性 `address_risk` 预检查：零地址（阻断）、转给自己、转给本钱包其他地址；`preflight.risk` 带 level/code/message | 已有 |
| 收款地址 | 已实现 `receive_address` 工具：从 `wallet_accounts` 汇总，EVM 全链合并为一张卡，返回 `groups`/`supported_chains`/`message` | 已有 |
| 转给联系人 | 无联系人概念 | **缺失** |
| 语音输入 | 全仓库无 audio/speech/STT 代码 | **缺失** |
| 历史记录独立搜索 | 只有资产搜索（`/v1/assets?search=`） | **缺失** |
| 链覆盖 | `APP_NETWORKS` = ETH/BSC/BASE/POLYGON/ARBITRUM/OPTIMISM + BTC/TRON/SOLANA，缺 Avalanche/Robinhood/MegaETH/CELO | **缺失** |
| PayAll / 批量付款 | 仅路线图第五阶段有描述 | **缺失**（待核设计细节） |

## 三、差距与建议顺序

**P0（对话主路径上的空白）**

1. ~~**收款地址能力**~~ 已完成：`receive_address` 工具（`nodes.py` `react_receive_address`）返回
   `groups`（EVM 合并为一张卡、BTC/TRON 各自成组）、`supported_chains` 与设计一致的
   `message` 文案；二维码仍由客户端按地址生成。Demo 新增 `data-card-kind="receive"` 卡片。
2. ~~**交易记录对话查询**~~ 已完成：`transaction_history` 工具（`nodes.py`
   `react_transaction_history`）按 App 声明的地址并发取数、超时保留部分结果，支持
   `chain` 与 `direction`（send/receive/swap）筛选，返回 `items`/`total`/`summary`
   与设计一致的 `message`。`TransactionRecord` 增加 `direction/symbol/decimals/
   counterparty/occurred_at`，OKX 与 BTC(Esplora) 适配器都会填充。
   仍未做：单笔详情跳转、跨页分页（`next_cursor` 未透出到卡片）。

**P1（体验完整性）**

3. 历史记录：列表项返回**用户原话**（当前 `summary` 对非兑换会话是固定串），
   并补对话历史搜索接口。
4. ~~地址风险提示卡片~~ 已完成（见 4.3）。路线图第三阶段的"已知风险标签/黑名单"仍未做，
   目前只覆盖零地址、自转和本钱包地址三种可确定的风险。
5. 联系人（转给联系人）：需要 App 侧提供联系人或由用户手动录入。

**P2（范围扩张）**

6. 语音输入：需确认是否由服务端做 STT，还是客户端本地转文字后复用
   `/v1/agent/turn`（后者改动最小）。
7. EVM 链覆盖扩展（Avalanche 等）：属于链适配器与 provider 覆盖问题，非对话层。
8. PayAll 批量付款：与路线图第五阶段合并考虑。

## 四、对话卡片对齐

### 4.1 设计中的卡片清单

| 卡片 | 节点 | 关键内容 |
| --- | --- | --- |
| 交易记录卡 | [`33715:146539`](https://www.figma.com/design/2N2sTwtjY3OznWq8m9zpmR/Cat-Wallet-v1.6.0?node-id=33715-146539) | 标题「最近交易记录」+「共 5 笔」；分隔线；逐行 24px 图标 + 描述 + 时间 + 右侧金额 + 状态徽章 |
| 收款地址卡 | [`33715:146203`](https://www.figma.com/design/2N2sTwtjY3OznWq8m9zpmR/Cat-Wallet-v1.6.0?node-id=33715-146203) | 120×120 二维码；地址（14 Semibold，居中，超长省略）；「支持所有 EVM 链」；链标签；整宽「复制地址」按钮 |
| 多链资产总览卡 | 资产查询屏（[`33575:21647`](https://www.figma.com/design/2N2sTwtjY3OznWq8m9zpmR/Cat-Wallet-v1.6.0?node-id=33575-21647) 第五段）；空态见「文字输入流程」末屏 | 标题「多链资产总览」+ 大字总额（`$ 186.46`）；逐行 币图标 + `ETH (ETH)` + 右侧「数量 ／ USD 估值」，名称下方灰字单价；空态为 `$ 0.00` +「暂无资产数据」 |
| 支持链卡 | 收款地址屏内 | 标题「支持的链」+ 链标签（BTC／TRON） |
| 地址输入卡 | [`33692:37951`](https://www.figma.com/design/2N2sTwtjY3OznWq8m9zpmR/Cat-Wallet-v1.6.0?node-id=33692-37951) | 「请问转给哪个地址？（也可以直接发地址给我）」+ 三个选项：输入地址／转给联系人／先看看余额 |
| 地址确认卡 | [`33692:48777`](https://www.figma.com/design/2N2sTwtjY3OznWq8m9zpmR/Cat-Wallet-v1.6.0?node-id=33692-48777) | 「地址」标签 + 地址 + 复制图标 |
| 链选择卡 | [`33692:48836`](https://www.figma.com/design/2N2sTwtjY3OznWq8m9zpmR/Cat-Wallet-v1.6.0?node-id=33692-48836) | 「支持的链」+ 链选项（ETH／BASE） |
| 风险提示卡 | [`33692:48838`](https://www.figma.com/design/2N2sTwtjY3OznWq8m9zpmR/Cat-Wallet-v1.6.0?node-id=33692-48838) | 「地址风险提示」+ 警告文案 + 两个按钮 |
| 金额输入卡 | [`33692:37984`](https://www.figma.com/design/2N2sTwtjY3OznWq8m9zpmR/Cat-Wallet-v1.6.0?node-id=33692-37984) | 资产与余额；金额输入 + 法币估值；25%／50%／75%／100%；余额不足 NoticeBar；取消／确认 |
| 用户消息气泡 | [`33715:146536`](https://www.figma.com/design/2N2sTwtjY3OznWq8m9zpmR/Cat-Wallet-v1.6.0?node-id=33715-146536) | 右对齐浅黄气泡，右下角小圆角 |
| 交易状态页（App 页，非对话卡） | 分区 [`33617:39513`](https://www.figma.com/design/2N2sTwtjY3OznWq8m9zpmR/Cat-Wallet-v1.6.0?node-id=33617-39513)「发送和兑换详情」 | `交易中…`（支付数量／到账金额／预计耗时 30.22 分钟）与 `交易完成／失败`（金额 + ❌失败）；页内带默认问题「问问 Cat Wallet AI：解释失败原因？／为什么还在处理中？」 |

### 4.2 视觉规范（从节点提取）

- 卡片容器：背景 `#f5f7fa`，圆角 12，内边距 16，纵向间距 12。
- 分隔线：1px `#ebedf0`。
- 字阶：14 Semibold（卡片标题）、12 Regular（正文）、12 Medium（金额）、10 Regular（时间／状态／链标签）。
- 文字色：`#000` / `rgba(0,0,0,.5)`（次要）/ `rgba(0,0,0,.7)`（链标签）。
- 用户气泡：`#fff8d6`，圆角 18/18/2/18（右下 2）。
- 状态徽章：成功 = `#093` 文字 + `rgba(0,153,51,.1)` 底 + 勾选图标；处理中 = `#ff9e2f` 文字 + `rgba(255,158,47,.1)` 底 + 环形进度；10px，圆角 2。
- 链标签：高 16，圆角 4，底 `#ebedf0`，10px。
- 整宽按钮（复制地址）：高 40，圆角 24，底 `#ebedf0`，12 Medium。
- 输入区：圆角 37，底 `#ebedf0`，麦克风图标 + 占位文案 + 32px 圆形发送钮。
- 顶部栏：居中「Cat Wallet AI」，左右各一个 40px 玻璃圆钮。

### 4.3 服务端需要补齐的字段

服务端不必拼 UI，但要把这些卡片所需的**结构化数据**稳定返回：

1. **交易记录卡**：已完成。`TransactionRecord` 增加 `direction`（send／receive／swap）、
   `symbol`、`decimals`、`counterparty`、`occurred_at`；工具返回 `items`（含带符号金额渲染所需的
   `amount_raw` + `decimals` + `symbol`）、`total` 与 `summary`，并支持 `direction` 与 `chain` 过滤。
2. **收款地址卡**：新增能力，返回用户自己的收款地址（按链或「EVM 全链共用」）、支持链清单；二维码由客户端按地址生成。
3. ~~**参数收集卡**~~ 已完成：快捷建议现在带 `type` 与 `value`（`chain`／`symbol`／
   `amount`／`address`／`token`／`action`）。转账缺收款人时按设计给出三个胶囊：
   输入地址／转给联系人／先看看余额（后两个是 `action`，由 App 处理）。Demo 里的胶囊
   按钮会带上类型，且刷新后仍保留（随 artifacts 一起投影）。
   第一位固定为「输入地址」：不再把「使用当前钱包地址」放在首位（会把自转当成默认收款人），
   当前钱包地址交给 App 的地址输入卡自行决定是否提供。
   模型需要用户粘贴收款地址时还会下发
   `response.client_action {type: "address_input", slots: {chain}}`，App 据此弹出地址输入框
   （与 `open_contacts` / `scan_qr` / `open_amount_keypad` 同一通道）。
   缺链时给出**链选择卡**（`type: "chain"`，钱包当前网络排第一、纯链选项）；
   缺金额时给出**金额输入卡**（`type: "amount"` 的 25%/50%/75%/100%，`data` 带
   `token_address` / `decimals` 供 App 定位资产；余额与百分比换算留在客户端）。
   模型逐步收集（先地址 → 再网络 → 再金额），网络未明确时不再默认使用钱包当前网络。
4. ~~**风险提示卡**~~ 已完成：`_address_risk`（`nodes.py`）产出等级与文案，进入预检查的
   `address_risk` 检查项并作为 `preflight.risk` 返回；零地址为 `danger` 直接阻断，
   自转与"转给本钱包其他地址"为 `warning`。Demo 新增 `data-card-kind="risk"`。

### 4.4 现状对照

`demo-next` 现有卡片（`data-card-kind`）：`portfolio`、`quotes`、`confirmation`、`preflight`、
`approval`、`transaction`、`bitcoin-signing`、`tokens`、`wallet`、`price`、`asset-value`、
`gas`、`assets`、`status`、`react-results`、`react-observability`、`tool-timeline`。

| 设计卡片 | 现状 |
| --- | --- |
| 交易记录卡 | 已有 `data-card-kind="history"` 卡片：标题 + 共 N 笔 + 逐行「方向/对手方/金额/链/状态/时间」+ 汇总 |
| 收款地址卡 | 已有 `data-card-kind="receive"` 卡片：分组地址 + 所属网络 + 链清单 + 复制；二维码仍由客户端生成 |
| 地址／链／金额输入卡 | 快捷建议已带 `type`/`value` 并可按设计排成胶囊行；卡片化的视觉呈现仍由 App 负责 |
| 风险提示卡 | 已有 `data-card-kind="risk"` 卡片，数据来自 `preflight.risk` |
| 多链资产总览卡 | 数据已有（`portfolio`），但资产行没有**单价**，也没有「暂无资产数据」空态语义 |
| 金额卡（25/50/75/100%） | 无 25/50/75/100% 预设；余额数据已有，换算属 UI 层 |
| 交易失败原因／预计耗时 | `TransactionDetail` 只有 gas／fee／token_transfers，`NormalizedOrderStatus` 只有 message，没有稳定的失败原因与 ETA 字段 |

### 4.5 仍未对齐的卡片（按建议顺序）

1. **多链资产总览卡**：`portfolio` 内部已经算出单价（`price_map`），但没有透出到资产行——
   资产行现在是 `asset／amount／amount_raw／usd_value／addresses`。补上单价与空态标记，
   App 才能渲染设计里的「名称（链）+ 单价 + 数量 + 估值」四要素。
2. **交易状态 / 失败解释**：设计详情页的默认问题是「解释失败原因？」和「为什么还在处理中？」。
   现在 `transaction_status` 只返回状态枚举与固定文案，`TransactionDetail` 没有 `failure_reason`，
   订单状态没有预计到账（`expires_at` 不等于 ETA）。需要补字段，并让 App 进入对话时带上
   交易上下文（tx hash 已支持，订单上下文需要扩展 `metadata`）。
3. ~~**金额比例快捷**~~ 已完成：金额输入卡给出 25%／50%／75%／100%（`type: "amount"`），
   `data` 带 `token_address`/`decimals` 供 App 定位资产；余额换算留在客户端
   （见 [app-cards-integration.md](app-cards-integration.md) §3.10）。
4. **入口与默认问题**：发送/兑换详情页的「问问 Cat Wallet AI」需要 App 把当前交易上下文
   带进这一轮，否则 agent 只能反问。
5. **语音输入**：属于输入模式而非卡片；服务端没有 STT，需要先定产品方案
   （客户端转写 vs 服务端识别）。

**结论**：`demo-next` 是联调控制台（大量卡片直接 `JSON.stringify` 调试数据），设计稿面向的是 **App**。
因此"对齐卡片"主要落在**服务端响应契约**上：为每类卡片定义稳定的 payload（`kind` + 字段），
再决定是否在 Demo 里补一个贴合设计的卡片视图用于走查。

## 五、待确认

- 本文件同仓库还有一页 **v1.7.0 AI 助手**（page [`34304:20199`](https://www.figma.com/design/2N2sTwtjY3OznWq8m9zpmR/Cat-Wallet-v1.6.0?node-id=34304-20199)），比本次对照的 v1.6.9 更新，
  需要确认以哪一版为准。本文件页签列表还有 v1.6.3 BTC SWAP、v1.6.1 Robinhood 等相关页。
- 资产查询结果卡、兑换流程卡、语音流程屏的节点在读取时被截断，未纳入本文件第四节，需要补读后定稿。
- "钱包 --> PayAll" 两条标注的确切含义。
- 语音输入的链路归属（服务端 STT vs 客户端转写）。
- 交易记录的数据源：现有 REST 依赖各链 explorer，条数、分页与「兑换」识别的覆盖度需先评估。
- 交易记录卡里的「兑换 BNB → USDT」需要 provider 侧数据来判定兑换类型，链上 explorer 未必能直接给出。
