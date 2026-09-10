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
OPENAI_MODEL=deepseek-chat
OPENAI_BASE_URL=https://api.deepseek.com
```

`OPENAI_MODEL` 必须包含在 `ALLOWED_MODEL_IDS` 中。默认配置已经包含 `deepseek-chat` 和 `deepseek-reasoner`。

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

## 5. 先调试普通对话

在 Demo 中关闭“将本次消息作为真实 swap quote 请求”，消息填写：

```text
你好，请简要介绍一下你能帮助我做什么。
```

点击“发送真实请求”。页面会先收到一个 `run_id`，然后通过 SSE 读取执行事件。正常情况下可以看到 `update` 和 `complete` 事件。

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

1. 勾选“将本次消息作为真实 swap quote 请求”；
2. 填写源链、目标链、源/目标 Token 地址、数量、发送地址和接收地址；
3. 点击“发送真实请求”；
4. 等待完整的 `quote_candidates` 报价列表；
5. 在页面中选择一个报价；
6. 点击“选择报价并检查 allowance”。

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

1. 钱包 App 在本地签名并广播 approve 交易；
2. 只将已广播交易的 hash 填入 Demo；
3. 点击“提交 approve hash 并继续”；
4. 服务端检查 approve receipt 是否成功；
5. 服务端重新读取链上 Allowance；
6. Allowance 足够后，服务端才生成 `pending_transaction`；
7. 钱包 App 本地签名并广播最终兑换交易；
8. 将最终交易 hash 提交到 `/v1/swap/{session_id}/broadcast`。

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
