# Browser Wallet Chat Demo Design

## Goal

将浏览器 Demo 改造成聊天式兑换 App，支持 EIP-1193 浏览器钱包连接，并把公开的钱包地址和链信息传递给 LangGraph；钱包插件负责 approve 和 swap 的签名广播，服务端只接收交易 hash。

## Scope

- 支持 EVM 浏览器钱包插件（MetaMask、Rabby、OKX Wallet 等 EIP-1193 provider）。
- 对话中收集兑换参数；缺少参数时返回可渲染的 clarification 和 missing fields。
- 复用现有报价、provider_reference、allowance、approve 和 swap API。
- Demo 仍然使用原生 HTML/JavaScript，不新增前端构建工具或第三方 SDK。
- 不接收、保存或记录 private key、seed phrase、signer、wallet client 或 provider 对象。

## Conversation contract

每个对话请求可以携带 `address`、`chain` 和 `session_id`。服务端将它们投影到 checkpoint-safe 的 `wallet_context`；同一兑换流程的后续消息复用 `conversation_id` 和 `session_id`。

LangGraph 的 intent 输出支持可选兑换草稿字段。草稿会与 checkpoint 中的历史草稿合并；字段不完整时返回：

```json
{
  "kind": "clarification",
  "missing_fields": ["source_token_address"],
  "message": "还需要源 Token 合约地址。"
}
```

字段完整后，服务端构建现有 `SwapQuoteRequest` 并进入报价 fan-out。浏览器前端展示全部报价，用户必须显式选择 `provider_reference`。

## Wallet boundary

前端通过 `window.ethereum` 获取 accounts 和 chainId，监听 accountsChanged 和 chainChanged。前端只发送公开地址、链名称、chain ID 和交易 hash。服务端返回 unsigned transaction；前端调用 `eth_sendTransaction`，然后提交 approve 或 swap hash。

## UI

Demo 使用聊天时间线作为主界面，业务节点以内嵌卡片展示：钱包连接状态、兑换参数确认、报价列表、approve 未签名交易、等待确认、swap 未签名交易和广播结果。移动端后续可以复用相同 REST/SSE 契约。

## Errors

- 钱包未安装或用户拒绝连接：显示可恢复的连接提示。
- 当前链与 unsigned transaction 的 chain ID 不匹配：阻止签名并提示切换网络。
- 账户切换：清除本地 wallet context，要求用户重新确认当前兑换。
- 签名拒绝或 RPC 失败：保留对话和 session，允许重试。

## Verification

- API 测试钱包上下文和 session_id 的转发。
- Graph 测试草稿合并、缺失字段 clarification 和完整草稿进入报价。
- 浏览器脚本静态检查和关键字符串检查。
- 运行现有 pytest、Ruff 和 compileall。
