# Wallet App Agent 自动化评测设计

日期：2026-09-20

## 1. 背景

当前 Wallet Agent 已有单元测试、API 测试、图路径测试和少量离线评测，但真实问题仍主要由人工操作 Demo 发现。最近一次对话暴露了两个典型缺口：

- 用户已明确“把 10 USDT 从 ETH 换成 BSC 上的 BNB”，图状态也正确保存了这些参数，但兑换资产解析器把无合约地址的原生 BNB 当成未找到的 Token，最终返回 `ASSET_NOT_FOUND`。
- Demo 新增了自动滚动函数，但 `#messages` 并不是实际滚动容器。现有测试只检查 HTML 中是否存在相关函数名，因此无法发现真实浏览器中滚动没有发生。

这说明“函数存在”“节点分支被走到”或“固定模型输出通过”不足以证明 Wallet App 接入体验正确。需要从用户输入开始，覆盖 Agent、API/SSE、钱包桥接、Provider/RPC 和浏览器 UI 的完整自动化评测。

本设计不承诺数学意义上的“发现所有问题”。目标是系统性覆盖已知风险类别，并把每次真实故障自动沉淀为回归用例，使遗漏范围可见、可度量、持续缩小。

## 2. 目标

1. 修复本次暴露的原生资产解析问题，使 BSC/BNB 等原生资产不依赖 Token 合约地址。
2. 修复 Demo 滚动容器，并用真实浏览器行为测试验证自动跟随和人工上滑暂停。
3. 建立可重复、默认无真实资金风险的 Wallet App 接入评测运行器。
4. 覆盖自然语言、多轮状态、HTTP/SSE 契约、钱包交互、交易生命周期、故障恢复和移动端 UI。
5. 输出机器可读报告，能够按能力、风险和失败阶段定位问题。
6. 将脱敏后的真实失败对话转化为长期回归场景。

## 3. 非目标

- 默认评测不持有或读取用户私钥、助记词或生产钱包签名材料。
- 默认评测不在主网签名或广播交易。
- 第一阶段不替换现有 pytest 测试，而是在其上增加场景和端到端层。
- 不使用 LLM Judge 代替确定性的金额、链、地址、安全边界和状态机断言。
- 不把 Demo 内置的“测试全部 Agent 功能”视为正式评测入口；它仍可作为开发辅助工具。

## 4. 已确认的根因与即时修复

### 4.1 原生资产解析

`_resolve_swap_assets` 当前只把存在 `asset.address` 的 Provider 资产纳入唯一性匹配。原生 BNB、ETH、MATIC 等通常没有 Token 合约地址，因此会被过滤。

修复策略：

- 在请求 Provider Token 列表前，先根据 canonical chain、原生币映射和 chain ID 识别原生资产。
- 原生资产使用 `address=None`、对应 chain ID、原生 decimals（当前支持链默认为 18；非 EVM 链使用适配器定义）。
- Token 仍必须通过可信元数据源确认地址和 decimals。
- Provider 返回的无地址原生资产可以作为交叉验证，但不能和同名 Token 混为一类。
- 来源资产和目标资产都覆盖原生币；exact-in 和 exact-out 使用同一解析逻辑。

回归场景至少包括：

- ETH USDT → BSC BNB。
- BSC BNB → ETH USDT。
- Base ETH → Base USDC。
- 同名 Token 候选不能被误识别为原生币。
- 不支持链上的未知 symbol 仍返回结构化错误。

### 4.2 Demo 自动滚动

当前 `main` 只有 `min-height: 100vh`，内容增长时通常由 document 滚动；代码却只修改 `#messages.scrollTop`。

修复策略：

- 将应用壳限制为 `height: 100dvh` 并隐藏外层溢出。
- 为 `#messages` 设置 `min-height: 0`，使其成为唯一的纵向消息滚动容器。
- 新用户消息强制恢复到底部；Agent 新输出仅在用户处于底部阈值内时跟随。
- 用户主动上滑后暂停跟随，回到底部阈值后恢复。
- 卡片替换、逐字输出、viewport resize 和移动端软键盘引起的尺寸变化都重新校准。

真实浏览器测试必须断言滚动数值和可见元素，不能只断言源码字符串。

## 5. 分层评测架构

### 5.1 第一层：确定性 LangGraph 场景评测

扩展现有 `src/evals`，以多轮场景作为最小评测单位。每个场景包含：

- 用户轮次和钱包上下文变化。
- 固定模型输出或真实模型模式。
- Provider、链适配器和价格源的可编程行为。
- 每轮预期响应、任务状态、槽位、外部调用次数和安全不变量。

确定性断言包括：

- 用户明确给出的链、Token、数量和地址不会被无故覆盖。
- 参数补充、纠正、取消和旁路查询后状态保持正确。
- 未确认前不 prepare，未取得钱包广播结果前不 register broadcast。
- 交易金额、decimals、slippage、allowance、gas 和 chain ID 一致。
- 图在有限步骤内结束、等待用户动作或返回可恢复错误，不能静默卡住。
- checkpoint resume、重复请求和并发请求保持幂等。

### 5.2 第二层：Wallet App 接入契约模拟器

新增一个无 UI 的状态化客户端，通过公开 HTTP/SSE API 驱动完整流程，而不是直接调用图节点。它模拟 Wallet App 的职责：

- 创建 turn，消费 SSE，处理 `action_required`。
- 选择报价、确认摘要、提交 approve hash 和 swap hash。
- 模拟账户切换、网络切换、用户拒签、重复点击和 App 重启恢复。
- 对 EIP-1193 方法进行记录并返回可编程结果。
- 模拟节点“暂时找不到、pending、confirmed、reverted、dropped”。
- 模拟 Provider 超时、空报价、错误字段、过期报价和状态查询失败。

第一阶段使用内存 Provider/Chain 和确定性交易 hash，不产生真实签名。后续可选使用 Anvil 等本地链验证 calldata、allowance 和 receipt，但仍不触达主网资金。

### 5.3 第三层：Playwright 浏览器端到端评测

使用真实浏览器加载 `/demo/`，在页面加载前注入受控的 `window.ethereum`。测试覆盖：

- 移动端 viewport 下连接钱包、发送消息、读取流式响应和操作卡片。
- 自动滚动、用户上滑暂停、回到底部恢复、卡片变高和 resize。
- 报价选择、确认、Approve、Swap、状态刷新和失败提示。
- 网络或账户变化后会话行为。
- 重复点击、按钮禁用、取消、重试和页面刷新。
- 不向后端请求或页面日志泄漏私钥、签名材料和 bearer token。

浏览器测试应保存失败截图、关键 DOM、控制台错误和网络事件，便于直接复现。

### 5.4 第四层：真实模型与故障/对抗评测

在显式启用的 online/nightly 模式中，使用配置的真实模型和安全的模拟后端测试：

- 中英文混合、口语、错别字、币种歧义和紧凑金额。
- 多轮省略、反悔、纠正链或数量、插入无关查询再继续。
- prompt injection、诱导泄漏、要求代签或绕过确认。
- Provider/RPC 延迟、超时、畸形 JSON、数值边界和顺序变化。

金额、安全和状态断言始终由确定性规则评分。LLM Judge 仅用于评估提示是否清晰、错误是否可操作、是否暴露内部实现等软性指标，并保留原始证据。

## 6. 场景格式与覆盖矩阵

场景采用版本化的数据结构，建议字段如下：

```yaml
id: swap_eth_usdt_to_bsc_bnb
persona: connected_evm_wallet
turns:
  - user: 把 10usdt 换成bnb
    expect:
      kind: clarification
      missing_fields: [source_chain, destination_chain]
  - user: 用以太链上的usdt换bnb
    expect:
      missing_fields: [destination_chain]
  - user: 目标链在bsc
    expect:
      kind: swap_quote
invariants:
  no_server_signing: true
  no_broadcast_before_wallet_hash: true
```

覆盖矩阵至少按以下维度组合，而不是只维护若干 happy path：

- 能力：余额、资产、价格、gas、转账、exact-in/exact-out 兑换、状态查询。
- 资产：原生币、ERC-20、同名 Token、不同 decimals、无 logo/name 的最小元数据。
- 对话：单轮、多轮、纠正、取消、插入查询、恢复、重复消息。
- 钱包：未连接、错误网络、账户变化、拒签、重复签名、App 重启。
- 链上状态：不可见、pending、confirmed、reverted、dropped、RPC 分歧。
- Provider：Bridgers、Omni、单 Provider 失败、多 Provider 部分成功、反向询价。
- UI：桌面、移动端、短消息、长消息、长卡片、软键盘/resize。

## 7. 评分与报告

每次运行输出 JSON，并生成适合人工阅读的摘要。顶层维度：

- `conversation_understanding`
- `state_and_resume`
- `asset_and_chain_resolution`
- `quotes_slippage_and_allowance`
- `gas_and_transaction_building`
- `broadcast_and_confirmation`
- `wallet_api_contract`
- `safety`
- `browser_ux`

每个失败必须包含：场景 ID、失败轮次、期望与实际、LangGraph stage、相关 API/SSE 事件、模拟器调用记录，以及浏览器层的截图路径。依赖不可用与产品逻辑失败分开统计，不能把“blocked”算作通过。

正式门禁以关键安全不变量 100% 通过为前提。体验评分下降可以报警，但不能掩盖确定性错误。

## 8. 真实故障回归闭环

为避免继续依赖人工重复发现：

1. 为 turn、session、provider/RPC 调用记录关联 ID 和结构化阶段信息。
2. 导出失败对话时移除钱包地址、Token、认证信息和 Provider 敏感 payload。
3. 将失败转换为最小可复现场景，先在修复前失败，再在修复后通过。
4. 场景进入永久回归集，并标记来源问题和首次修复版本。

生产或真实联调日志不得直接成为评测 fixture；必须先脱敏和最小化。

## 9. CI 与运行模式

- PR 必跑：确定性 LangGraph 场景、API/Wallet 模拟器、核心 Playwright 流程。
- Nightly：完整浏览器矩阵、故障注入、真实模型评测。
- 手动显式启用：真实 Provider/RPC smoke；默认只读，不签名、不广播。
- 主网广播测试始终排除在自动 CI 外。

为了减少不稳定性，测试必须使用固定时钟、固定随机种子、受控超时和独立 conversation/session ID。失败重跑只能用于诊断，不能把第二次通过当作首次成功。

## 10. 实施阶段

### 阶段一：当前缺陷与评测基础

- 修复原生币兑换资产解析。
- 修复 Demo 滚动容器。
- 添加本次三轮兑换回归场景。
- 引入 Playwright 最小运行框架和真实滚动测试。
- 建立统一场景结果与 JSON 报告。

### 阶段二：Wallet App 生命周期

- 建立状态化 API/SSE 客户端和 EIP-1193 模拟器。
- 覆盖报价、确认、Approve、广播、状态恢复和幂等。
- 加入 Provider/RPC 故障注入矩阵。

### 阶段三：真实模型和持续回归

- 扩大自然语言语料和对抗输入。
- 增加 online/nightly 模式与软性质量评分。
- 建立真实失败脱敏、最小化和回归导入流程。

## 11. 验收标准

阶段一完成需同时满足：

1. 本次三轮对话不再返回 BNB `ASSET_NOT_FOUND`，并能进入报价结果或明确的 Provider 不支持错误。
2. 浏览器测试证实新输出在底部时自动跟随；用户上滑时不抢滚动；回到底部后恢复。
3. 静态字符串测试不再是自动滚动的唯一证据。
4. 场景运行器可用单一命令执行并输出 JSON 报告，失败时退出码非零。
5. 当前 pytest 与 lint 全部通过，未跟踪的用户文件不被纳入提交。

完整体系完成需进一步满足：

1. Wallet App 模拟器能无真实私钥完成报价到 confirmed/reverted 的全生命周期。
2. 所有关键安全不变量 100% 通过。
3. Bridgers、Omni、原生币、Token、exact-in/exact-out 和恢复路径都有确定性场景。
4. CI 保存失败证据并能从场景 ID 单独复现。
