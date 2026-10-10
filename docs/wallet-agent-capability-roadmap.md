# Wallet Agent 能力扩展路线

这份文档记录钱包 App 对话式 Agent 的扩展方向，以及每一阶段的交付边界。所有交易都遵循非托管模型：服务端只读取链上状态、解析意图和生成未签名交易，签名与广播始终由钱包插件或移动端本地完成。

## 产品目标

- 让用户可以用自然语言完成查询、兑换、转账和交易追踪。
- 在签名前给出可理解的余额、手续费、网络和风险说明。
- 在多链场景下自动识别当前钱包网络，必要时引导切链，不替用户签名。
- 对每个动作保留结构化状态，方便 App 展示、恢复和排错。

## 分阶段路线

### 第一阶段：交易基础体验

状态：业务节点已实现；自然语言验收进行中。

- 自然语言转账参数提取：支持链、币种、金额、收款地址；缺少信息时逐项追问；默认使用当前连接钱包地址作为发送方。
- 统一交易预检查：在转账和兑换准备前检查地址、钱包账户、目标链、余额、预计手续费和 Gas 余额，返回机器可读检查项和用户可读警告。
- 交易状态查询：支持按链和交易哈希查询 pending、confirmed、failed 等状态，并返回适合直接展示给用户的说明。
- 多链转账与兑换准备：EVM、TRON（账户模型）与 Bitcoin（UTXO）都可生成未签名交易。Bitcoin 由服务端本地选 UTXO、计算手续费与找零并输出 PSBT；兑换时以 Provider 充值地址为目标地址，App 用比特币签名器签名后广播。

成功标准：

- 用户可以直接说“在 Base 转 10 USDC 给某地址”，无需先填写结构化表单。
- 余额不足、Gas 不足、钱包账户不一致等情况在签名前明确阻断。
- 用户提供交易哈希后，可以从聊天或 API 得到稳定的状态结果。

当前验收说明：确定性 Graph 测试已覆盖转账参数校验与未签名交易生成；在线模型
eval 仍作为发布门槛单独记录，不能仅凭节点存在将自然语言体验标记为完成。
Bitcoin 目前只覆盖 App 上报的静态地址（多地址 UTXO 已合并挑选）；基于
xpub / descriptor 的地址派生仍待办，见文末《待办工作》。

### 第二阶段：资产与费用助手

状态：已完成。

- 资产组合概览：按链展示原生币、Token、数量和 USD 估值。
- Gas 助手：比较各链当前费用，解释原生币不足，并提供充值或切换网络建议。
- 资产发现：按链搜索 Token，展示合约地址、精度、价格来源和更新时间。

当前已提供：

- 对话 intent：`portfolio_query`、`gas_check`。
- REST：`GET /v1/wallet/{address}/portfolio`、`GET /v1/wallet/{address}/gas`。
- 资产发现 intent：`asset_discovery`；REST：`GET /v1/assets?chain=BASE&search=USDC`。
- 多 Provider 资产结果按链、符号和合约地址去重，并保留 Provider 错误。
- USD 估值不可用时保留原始余额，并明确返回 `price_status: "unavailable"`。

成功标准：

- 用户能回答“我有哪些资产”“哪条链手续费更低”等常见问题。
- 所有价格和余额带有来源、链和观察时间，不把估算值伪装成实时最终值。

### 第三阶段：授权与风险控制

- 授权扫描：列出 ERC-20 对各合约的 allowance、额度和最近使用情况。
- 授权撤销：生成 revoke/approve 为 0 的未签名交易，每笔都要求用户确认。
- 地址风险检查：识别地址格式、零地址、已知风险标签和与当前链不匹配的地址。
- 交易解释：展示目标合约、方法、Token 数量、授权额度、手续费和风险提示。

成功标准：

- 用户在签名之前能知道“谁会被授权、授权多少、调用哪个合约”。
- 风险服务不可用时明确显示“无法判断”，不输出无依据的安全结论。

### 第四阶段：跨链进度与提醒

- 跨链兑换进度中心：展示源链交易、Provider 订单、目标链状态和预计下一步。
- 通知和提醒：确认、失败、超时、需要补充 Gas 或需要用户继续操作时提醒用户。
- 计划交易：支持 DCA、定时兑换等计划，但仍然在每次实际签名前交给钱包确认。

成功标准：

- 用户可以在一个页面追踪跨链动作的完整生命周期。
- Provider 或 RPC 暂时不可用时，状态不会被错误地标记为成功。

### 第五阶段：批量与高级自动化

- 批量转账和批量兑换拆分为可审计的交易清单。
- 每笔交易展示独立金额、收款方、手续费和风险，并支持逐笔确认。
- 允许用户配置提醒、额度和策略，但不允许 Agent 自动获取私钥或绕过钱包签名。

成功标准：

- 批量动作可暂停、重试和恢复，单笔失败不会隐藏其他结果。
- 自动化只负责准备和提醒，不具备无确认的资产转移能力。

## 安全原则

1. 不接收、保存或转发私钥、助记词、Signer、Wallet Client 或任何签名凭据。
2. LLM 只输出经过模型校验的意图和参数，不生成最终 calldata。
3. calldata 由链适配器和 Provider 适配器按固定规则生成，服务端只返回未签名交易。
4. 余额、手续费、交易状态和价格都必须标记链、来源和可用性。
5. 关键动作采用显式确认、可恢复状态和幂等 API。
6. 不把“查询失败”解释成“余额为零”“交易失败”或“地址安全”。

## Agent 智能化改造方案

本节是当前项目后续演进的实施基线。总体原则是：模型负责理解和规划，ToolNode 负责调用能力，业务 Graph 负责执行和校验，钱包负责本地签名与广播。

### 第一阶段：只读工具接入

风险最低，保留现有 Graph 节点，并逐步增加 `ToolNode`。

- 将余额、Gas、资产查询、报价查询封装为只读工具。
- 统一工具输入、输出和错误格式。
- 增加自然语言错误说明和澄清问题。
- 不允许工具接收私钥、助记词或任何签名凭据。

当前状态：当前列出的只读工具已完成。

- 已接入 `ToolNode`：余额查询、交易状态查询、Gas 查询。
- 余额和 Gas 工具已复用现有链适配器。
- 资产查询和兑换报价已通过 JSON-safe ToolNode 调用，并继续复用业务 Graph 做校验。

### 第二阶段：多轮任务与参数收集

- 增加 `conversation_state` 和 slot filling。
- 支持多轮补充兑换、转账和查询信息。
- 支持用户修改参数，例如“改成从 BSC 换”。
- 支持用户说“就选第一个报价”或按 Provider 选择报价。
- 用户说“取消兑换”时清理当前任务。

当前状态：核心任务状态已完成，扩展能力持续补充。

- 已有 canonical `active_task`，并保留 `swap_draft` / `transfer_draft` 兼容投影。
- 转账和兑换使用独立 slot extractor；模型只返回本轮 patch，缺参由服务端计算。
- task revision 会在用户有效修改时递增，并清理旧报价、确认和未签名交易。
- 已有 Token Resolver，可根据链和符号补全唯一 Token。
- 多匹配 Token 会要求用户选择。
- 常见中文兑换方向使用确定性规则校正，例如“换一些 USDT”表示目标资产、
  “用 USDC 换”表示来源资产，避免模型把换入/换出方向写反。
- 缺参响应可返回结构化快捷建议，帮助用户补充网络、来源资产和金额；建议不会
  自动选择报价、构成投资推荐或绕过显式确认。
- Demo 已支持“选第一个报价”和“取消兑换”。
- `conversation_state` 和 SQLite checkpoint 支持跨轮、跨进程恢复。
- 后续仍需把同一套 slot contract 扩展到交易状态、资产搜索等只读查询参数。

### 第三阶段：显式确认与可恢复交易

- 将 Approve、切链、Swap 签名改成显式确认节点。
- 增加用户确认状态、超时和取消。
- 支持恢复被中断的兑换流程。
- 任何签名、Approve、切链或广播动作都必须由用户在钱包侧确认。

当前状态：核心协议已收敛。

- 现有流程已经使用人工确认和 LangGraph interrupt。
- 已有 Approve、切链、Swap 签名、广播和状态跟踪。
- 取消 API 已增加。
- 确认状态绑定 task ID、revision 和 canonical payload hash；旧确认失效时不会调用 Provider。
- Demo 展示任务阶段、revision、已保留参数、缺参和确认状态。

### 第四阶段：Supervisor Agent

- 增加 Supervisor Agent。
- 由 Supervisor 决定调用哪些工具和业务节点。
- 业务 Graph 只负责具体执行、数据校验和安全边界。

目标结构：

```text
Supervisor Agent
  ├─ 信息收集子流程
  ├─ 余额/资产查询子流程
  ├─ 兑换报价子流程
  ├─ 转账子流程
  └─ 交易状态子流程
```

实施约束：

- 不一开始拆分成多个 Agent。
- 先采用一个 Supervisor、多个 ToolNode、现有业务 Graph 和明确的人工确认节点。
- Supervisor 不得直接签名、广播或绕过钱包确认。

当前状态：已采用单一 Supervisor，但其职责被限制为任务分类；transfer/swap 参数由窄化
extractor 提取，业务 Graph 负责合并、校验、路由和确认安全。

### 推荐实施顺序

1. 用离线与在线 eval 持续验收当前 transfer/swap slot contract。
2. 将窄化参数提取扩展到交易状态、资产搜索等带参数的只读任务。
3. 在现有 revision-bound 确认协议上实现授权扫描和撤销能力。
4. 增加跨链进度、提醒和计划交易，但每次签名仍由钱包显式确认。

最终形态：

```text
一个 Supervisor
+ ToolNode
+ 现有业务 Graph
+ 明确的人工确认节点
```

这样可以提升自然语言理解和多轮对话能力，同时保持钱包交易的非托管安全边界。

## 待办工作（Backlog）

### BTC 地址派生（xpub / output descriptor）

记录时间：2026-10-10。来源：多地址 UTXO 聚合落地后的遗留问题。

背景：当前比特币能力只覆盖「App 声明的静态地址」。转账已经会把 `wallet_accounts`
里所有 BTC 地址的 UTXO 合并挑选（见 `docs/app-integration.md` §5.4），但如果 App 只
上报 HD 钱包的一部分地址，超出部分仍会报 `insufficient Bitcoin balance`。要彻底解决
需要服务端按 App 提供的公钥派生地址。

关键前提：**一个 xpub 只能派生一种脚本类型**。当前 demo 钱包恰好覆盖三种
（`bc1qdq…` = p2wpkh/BIP84、`bc1p47…` = p2tr/BIP86、`3FGA…` = p2sh/BIP49），因此需要
按脚本类型各提供一条 descriptor，而不是单个 xpub。

一期：

- [ ] 契约：`WalletAccount`（`src/wallet_agent/api/app.py:87`，当前 `extra="forbid"`）
      增加可选 `descriptor` 与 `gap_limit`。采用 output descriptor 形式，例如
      `wpkh([a1b2c3d4/84'/0'/0']zpub…/0/*)`，静态 `address` 保留作兜底与校验锚点。
- [ ] 新增 `src/wallet_agent/chains/bip32.py`：descriptor 解析、CKDpub、SLIP-132 前缀
      归一化（xpub/ypub/zpub），以及 p2pkh / p2sh-p2wpkh / p2wpkh / p2tr（BIP341
      TapTweak）四种脚本的地址编码。
- [ ] `src/wallet_agent/domain/addresses.py` 补 `bech32_encode` 与
      `base58check_encode`（目前只有解码向的 `base58check_payload` /
      `decode_bech32_segwit`）。
- [ ] secp256k1 依赖选型：仓库当前没有任何曲线库，需在 `coincurve`、`embit` 或纯
      Python 实现之间取舍（先比较引入成本再定）。
- [ ] 地址发现：实现 BIP44 gap limit 扫描（默认 20，receive 与 change 分支各一遍），
      结果按 descriptor 指纹缓存并设 TTL。Esplora 没有批量端点，一次扫描是 20–40 个
      GET，必须缓存。
- [ ] 接线：把「钱包地址从哪来」收敛到单一入口，供
      `_declared_accounts`（`src/wallet_agent/graph/nodes.py:2421`）与
      `portfolio.py` 的余额扇出共用。
- [ ] 处理同步/异步边界：`_declared_accounts` 是同步函数，被同步的 `_chain_sender`
      / `_transfer_draft_request` 调用，而 gap 扫描需要发 HTTP；需要每轮对话开始时
      预解析一次，把结果放进 `wallet_context`，同步函数只读已解析列表。
- [ ] 测试：用 BIP32 test vector 1/2、BIP84、BIP86 官方向量做离线断言。

二期：

- [ ] 找零改用 change 分支（`1/*`）轮换，而不是回主地址。
- [ ] 后台刷新已派生的地址集；如有自建节点，用 `scantxoutset` 替代逐地址扫描。

安全边界：

- 只接受公钥 / descriptor，显式拒绝任何 `*prv*`；只支持主网（拒绝 `tpub`）。
- xpub 不可花费，但会泄露全部地址与余额轨迹：不进日志、不进 checkpoint / graph
  state 的持久化消息、不进 prompt。

替代方案（记录备查）：App 端自行派生并把地址集（或增量）上报，服务端零改动；代价
是每轮传更多地址。xpub 方案的价值在于 App 不必反复上报。
