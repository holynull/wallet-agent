# 本地调试 Demo

本文介绍如何在本地启动 Wallet LangGraph Agent，并使用浏览器 Demo 调试对话、报价、Allowance 和未签名交易流程。

Demo 使用真实的服务端配置和真实 API，不使用 fake model 或 fake provider。服务端只负责读取链上信息、获取报价和生成未签名交易；私钥始终保留在钱包 App 内。

## 1. 准备环境

项目要求 Python 3.11 或更高版本。先在项目根目录复制配置文件：

```bash
cp .env.example .env
```

至少配置 DeepSeek：

```dotenv
DEEPSEEK_API_KEY=你的-DeepSeek-API-Key
OPENAI_MODEL=deepseek-v4-pro
OPENAI_BASE_URL=https://api.deepseek.com
```

`OPENAI_MODEL` 必须包含在 `ALLOWED_MODEL_IDS` 中。默认模型是 `deepseek-v4-pro`，同时保留 `deepseek-chat` 和 `deepseek-reasoner` 兼容选项。

### 配置 EVM RPC

如果要查询真实余额、Allowance、交易回执和生成交易，需要配置 RPC。建议每条链配置多个 RPC URL：

```dotenv
RPC_URLS='{"ETH":["https://cloudflare-eth.com"],"BASE":["https://mainnet.base.org"],"BSC":["https://bsc-dataseed.binance.org"]}'
```

生产环境应替换为经过验证的稳定 RPC，并根据实际部署情况设置超时和重试参数：

```dotenv
RPC_TIMEOUT_SECONDS=10
RPC_MAX_ATTEMPTS=2
```

### 配置 Bridgers 和 OmniBridge

只有在需要调试真实兑换时才启用 Provider。Base URL 和 source flag 应使用项目对应 skill 或 Provider 侧提供的真实值：

```dotenv
BRIDGERS_ENABLED=true
BRIDGERS_BASE_URL=...
BRIDGERS_SOURCE_FLAG=...

OMNIBRIDGE_ENABLED=true
OMNIBRIDGE_BASE_URL=...
OMNIBRIDGE_SOURCE_FLAG=...
```

如果 Provider 响应中不包含 ERC-20 spender，还需要配置对应链的 spender：

```dotenv
BRIDGERS_SPENDER_BY_CHAIN='{"BASE":"0x..."}'
OMNIBRIDGE_SPENDER_BY_CHAIN='{"BASE":"0x..."}'
```

不要把私钥、助记词、signer、wallet client 或其他签名材料放入请求、Prompt、日志或页面字段中。

### 可选：配置 OKX 钱包和价格增强

OKX 默认关闭；关闭时 RPC、CoinGecko 和现有兑换流程保持不变。启用时填写：

```dotenv
OKX_ENABLED=true
OKX_BASE_URL=https://web3.okx.com
OKX_API_KEY=...
OKX_SECRET_KEY=...
OKX_PASSPHRASE=...
OKX_PROJECT_ID=...
```

OKX 只用于余额/总价值、价格/市场历史、gas-limit 和 simulation 增强。
浏览器钱包仍是唯一签名和广播方；OmniBridge/Bridgers 仍是兑换 Provider。
随时可设置 `OKX_ENABLED=false` 安全回退。若要运行只读真实契约测试，还需
设置 `OKX_INTEGRATION=1`、`OKX_INTEGRATION_ADDRESS` 和
`OKX_INTEGRATION_TOKEN_ADDRESS`；测试不会签名、提交或广播交易。

## 2. 使用 Python 启动服务

安装项目依赖：

```bash
uv sync
```

启动 FastAPI 服务：

```bash
./scripts/start_local.sh
```

脚本默认使用项目 `.venv` 中的 Uvicorn，监听 `127.0.0.1:8000`，并读取根目录的 `.env`。
开发过程中如果需要热重载，可以使用：

```bash
./scripts/start_local.sh --reload
```

也可以通过环境变量覆盖监听地址、端口和日志级别：

```bash
WALLET_AGENT_HOST=0.0.0.0 WALLET_AGENT_PORT=8000 WALLET_AGENT_LOG_LEVEL=debug \
  ./scripts/start_local.sh
```

服务默认监听：

```text
http://localhost:8000
```

如果系统没有安装 `uv`，可以使用官方安装脚本：

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

## 3. 检查服务状态

打开新的终端窗口执行：

```bash
curl http://localhost:8000/health
```

预期返回：

```json
{"status":"ok"}
```

再检查 LangGraph 是否成功构建：

```bash
curl http://localhost:8000/ready
```

预期返回：

```json
{"status":"ready"}
```

如果 `/health` 正常而 `/ready` 返回 `503`，请查看启动终端的异常信息，并重点检查：

- `.env` 是否存在；
- `DEEPSEEK_API_KEY` 是否正确；
- `OPENAI_BASE_URL` 和 `OPENAI_MODEL` 是否正确；
- `uv sync` 是否完成；
- Provider 的 Base URL、source flag 和 RPC 配置是否满足启用条件。

启动后也可以运行命令行 smoke test：

```bash
python scripts/smoke_test.py --base-url http://localhost:8000
```

## 4. 打开浏览器 Demo

访问：

```text
http://localhost:8000/demo/
```

### 连接浏览器钱包

Demo 支持 CatWallet、MetaMask、Rabby、OKX Wallet 等 EIP-1193 浏览器钱包插件。打开页面后点击“连接钱包”，插件只会向页面提供当前账户地址和 `chainId`。这些公开信息会随着对话请求发送给 Agent，用于查询余额、生成报价和构造未签名交易。

页面会优先选择 CatWallet：包括 EIP-6963 announcement、CatWallet 的
`isCatWallet`/名称标记、`window.catWallet`、`window.catwallet`，以及
`window.ethereum.providers` 中的 CatWallet Provider。没有识别到 CatWallet
时，才回退到 `window.ethereum` 或其他 EIP-1193 Provider。点击“连接钱包”
时会再次请求 EIP-6963 announcement，以覆盖插件延迟注入的情况。

Demo 不会读取或发送：

- private key；
- seed phrase 或 mnemonic；
- signer、wallet client；
- `window.ethereum` 对象本身。

如果服务端启用了 Bearer 认证，可在页面顶部的可选令牌输入框中填写
token。令牌只保存在当前页面内存中，并通过 `Authorization` 请求头发送；
不要把 token 写入兑换消息或 metadata。

账户切换后，页面会清除当前兑换 session，要求重新确认，避免把旧账户的报价或交易交给新账户签名。
网络切换不会清除 session。用户在 Approve 或 Swap 签名前，如果钱包当前链
不是 from token 所在链，Demo 会请求 `wallet_switchEthereumChain` 自动切换；
切换成功后继续签名，切换被拒绝或钱包不支持时则停止签名并提示用户手动切链。

如果浏览器没有安装钱包插件，Demo 仍可显示聊天界面，但不能执行真实签名和广播。

Demo 与移动端使用相同的 REST/SSE API。浏览器开发者工具中的以下位置最有用：

### Network

重点查看这些请求：

```text
POST /v1/agent/turn
GET  /v1/agent/stream/{run_id}
POST /v1/swap/{session_id}/select-quote
POST /v1/swap/{session_id}/approve-broadcast
POST /v1/swap/{session_id}/continue
POST /v1/swap/{session_id}/broadcast
```

### Console

用于检查 SSE 是否断开、JSON 是否解析失败，以及服务端返回的错误 envelope（例如 `422`、`409` 或 `503`）。

### 页面输出区域

调试时重点记录：

- `run_id`：一次 Agent 执行的标识；
- `conversation_id`：LangGraph 的稳定 thread ID，断线重连时应复用；
- `session_id`：一次兑换业务会话的标识；
- `quote_candidates`：完整报价列表；
- `provider_reference`：用户选择的 Provider 报价引用；
- `approval_transaction`：Allowance 不足时返回的 approve 未签名交易；
- `pending_transaction`：授权确认后返回的兑换未签名交易；
- `stage`：当前业务阶段。

页面顶部的“后端调试数据”面板会记录 Demo 看到的原始后端数据：

- `API GET /health`、`API GET /ready`；
- `API POST /v1/agent/turn`；
- `SSE CONNECT {run_id}`；
- 每条 `SSE update`、`SSE complete`、`SSE error` 事件；
- 非 JSON 错误响应的原始文本。

点击“复制调试数据”可把当前面板内容复制到剪贴板，方便提交问题。复制内容
与页面显示使用同一份安全归一化结果：`OK-ACCESS-*`、API key、secret、
passphrase、Authorization、签名/私钥/助记词及签名请求体等字段会先被移除
或遮蔽。不要把浏览器 Network 面板中未经检查的认证请求头直接粘贴到工单。

启用 OKX 增强后，Demo 还会展示价格 `provider`、`observed_at`、市场指标、
历史价格/K 线、预检查的 `gas_sources` 和 simulation 警告。广播状态为
`not_propagated` 时表示钱包返回了交易哈希，但 RPC 在有限重试后仍未看到
源链交易；此时后端会保存哈希，但不会登记或轮询兑换 Provider，页面也不会
误报“Provider 处理中”。

点击面板里的“联调检查”，Demo 会按顺序执行 health、ready、一次
`你好，联调检查` Agent turn 和对应 SSE 读取。这个入口不需要连接钱包，
适合先确认浏览器页面、HTTP API、Agent 图和 SSE 是否打通。若页面上出现
异常，先展开该面板，把最后几条 `API`/`SSE` 数据和启动终端日志对照。

### Agent 功能测试矩阵

点击“测试全部 Agent 功能”可以逐项验证 Agent 的主要 intent 和终止分支：

- `clarification`、`unsupported`；
- `wallet_query`、`portfolio_query`、`gas_check`；
- `asset_discovery`、`price_query`、`transaction_status`；
- `transfer` 的预检查和未签名交易准备；
- `swap_quote`、`swap_select`、`swap_allowance`、`swap_prepare`、`swap_status`。

测试项使用独立的 `conversation_id`，不会覆盖当前聊天或兑换 session。每项
测试都会把 request、API 返回和完整 SSE 事件写入“后端调试数据”面板。
需要钱包的查询在未连接钱包时显示为“阻塞”；RPC、价格服务或兑换 Provider
未配置或不可用时也显示为“阻塞”，避免把环境依赖问题误报为代码失败。

该矩阵只执行到签名前的安全边界。它不会调用 `eth_sendTransaction`，不会
自动签名、广播或提交 Approve hash。真实交易仍需在下方聊天流程中明确点击
钱包操作按钮。

### 自动化 Wallet App 评测

本地一键评测使用 fake chain/provider 后端覆盖钱包能力链路，不读取私钥、
助记词、signer 或生产 secret，不签名，也不广播任何交易。默认命令
适合作为 Phase 1 的确定性回归检查：

```bash
uv sync
.venv/bin/playwright install chromium
.venv/bin/python -m evals.wallet_agent_evals
.venv/bin/pytest -q tests/browser -m browser
```

`.venv/bin/python -m evals.wallet_agent_evals` 应覆盖确定性的 15 个
Wallet Agent 用例，包括 clarification、wallet 查询、资产发现、转账、
兑换报价、报价选择、allowance、prepare、取消和多轮记忆。浏览器命令只
运行带 `browser` marker 的 Demo 自动化测试，验证页面 follow、pause 和
resume 等行为。

需要评估已配置模型时，可以显式追加 `--online`：

```bash
.venv/bin/python -m evals.wallet_agent_evals --online
```

`--online` 只会调用当前配置的语言模型；钱包、链和 Provider 后端仍然使用
模拟实现。它不代表 Phase 2 的完整钱包生命周期模拟，也不代表 Phase 3 的
线上或对抗评测覆盖已经完成。

同样的链路也可以用命令行复现：

```bash
python3 scripts/smoke_test.py --base-url http://127.0.0.1:8000
```

如果需要指定消息或用户：

```bash
python3 scripts/smoke_test.py \
  --base-url http://127.0.0.1:8000 \
  --user-id debug-user \
  --message "你好，联调检查"
```

聊天式流程中，直接输入自然语言即可，例如：

```text
把 1 USDC 从 Base 换成 BSC 上的 USDT
```

如果缺少 Token 合约地址或其他必要信息，Agent 会在聊天中追问；参数完整后才会返回报价卡片。

转账也遵循同样的链规则：钱包必须在转账所在链上签名。如果当前钱包在
其他链，点击转账卡片的签名按钮时，Demo 会先请求自动切链；切链成功后
再调用 `eth_sendTransaction`。转账不会调用兑换 Provider，也不会提交到
`/v1/swap/{session_id}/broadcast`。

## 5. 先调试普通对话

消息填写：

```text
你好，请简要介绍一下你能帮助我做什么。
```

点击“发送”。页面会先收到一个 `run_id`，然后通过 SSE 读取执行事件。正常情况下可以看到 `update` 和 `complete` 事件。

也可以直接使用命令行：

```bash
curl -sS -X POST http://localhost:8000/v1/agent/turn \
  -H 'Content-Type: application/json' \
  -d '{
    "user_id": "debug-user",
    "message": "你好，请回复一句简短的问候。",
    "model_id": "deepseek-chat"
  }'
```

从返回 JSON 中复制 `run_id`，再读取 SSE：

```bash
curl -N http://localhost:8000/v1/agent/stream/替换为_run_id
```

本地未启用认证时，`user_id` 仍然需要传入。认证打开后，应在请求中增加 `Authorization: Bearer <token>`。

## 6. 调试真实兑换流程

在 Demo 中依次执行：

1. 连接浏览器钱包；
2. 在聊天框输入兑换意图，例如“把 1 USDC 从 Base 换成 BSC 上的 USDT”；
3. 如果缺少 Token 合约地址、decimals 等字段，按 Agent 的追问继续发送补充信息；
4. 等待完整的 `quote_candidates` 报价列表；
5. 在聊天消息中的报价卡片选择一个报价。

服务端不会自动选择报价，App 必须原样提交用户选择的 `provider_reference`。

### Allowance 不足时

如果 Allowance 不足，服务端返回 `approval_transaction`，例如：

```json
{
  "approval_transaction": {
    "chain": "BASE",
    "to": "0x...",
    "data": "0x...",
    "value": "0x0"
  }
}
```

此时按以下顺序操作：

1. 点击 Approve 卡片中的钱包签名按钮；
2. 钱包插件在本地签名并广播交易，Demo 只提交返回的交易 hash；
3. 服务端检查 approve receipt 是否成功；
4. 服务端重新读取链上 Allowance；
5. Allowance 足够后，服务端才生成 `pending_transaction`；
6. 点击兑换交易卡片中的钱包签名按钮；
7. 钱包插件在本地广播最终兑换交易，Demo 只提交最终交易 hash。

兑换状态通常按以下顺序变化：

```text
quoted
→ quote_selected
→ approval_required
→ approval_submitted
→ swap_ready
→ broadcasted
```

approve 交易广播成功不等于已经上链确认。只有 `/continue` 完成 receipt 和 Allowance 检查后，才可以使用 `pending_transaction`。

### 指定换出或到账数量

聊天兑换支持两种数量表达：

- `用 8 USDC 换 USDT`：精确指定换出的 USDC 数量。
- `换到 5 USDT`：精确指定期望到账数量，系统会分别向支持的 Provider 反向计算所需 USDC，再展示普通报价。

反向询价只读，不会创建订单、签名或广播；用户仍需选择报价并经过原有确认流程。如果 Provider 不支持反向询价，系统会要求明确填写换出数量。Provider 返回过最低换出限制后，界面会使用该动态最低值，不再建议固定的 `1 USDC`。

## 7. 使用 Docker 启动

Docker Compose 会读取项目根目录的 `.env`，因此必须先完成配置：

```bash
cp .env.example .env
# 编辑 .env，填入真实配置
docker compose up --build
```

查看服务日志：

```bash
docker compose logs -f wallet-agent
```

然后仍然访问：

```text
http://localhost:8000/demo/
```

停止服务：

```bash
docker compose down
```

不要在需要保留 LangGraph checkpoint 时使用 `docker compose down -v`，因为这会删除持久化数据卷。

## 8. 常见问题

| 现象 | 处理方式 |
| --- | --- |
| `/health` 正常、`/ready` 返回 `503` | 检查 DeepSeek key、依赖安装和启动日志 |
| `MODEL_NOT_ALLOWED` | 确认 `OPENAI_MODEL` 包含在 `ALLOWED_MODEL_IDS` 中 |
| 报价列表为空 | 检查 Provider 是否启用、Base URL 和 source flag 是否正确 |
| RPC 超时 | 为同一条链配置多个 RPC URL，并适当提高超时或重试次数 |
| `approval_pending` | approve 交易尚未确认，等待上链后再次点击“继续” |
| `quote not found` | 报价已过期，重新获取报价并让用户重新选择 |
| `422 user_id` | 在请求中补充 `user_id` |
| SSE 断开 | 查看浏览器 Network 和服务端日志，并复用原来的 `conversation_id` |
| Docker 启动失败 | 确认项目根目录存在 `.env`，Compose 会强制加载它 |

## 9. 相关文档

- [EVM 钱包动作接口](evm-wallet-actions.md)
- [移动端集成说明](mobile-integration.md)
- [真实移动端 Demo 说明](real-mobile-demo.md)
- [RPC 端点说明](rpc-endpoints.md)
