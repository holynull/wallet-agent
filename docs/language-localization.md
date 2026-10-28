# 语言本地化（App 指定 `language`）

## 用途

App 可以在 `/v1/agent/turn` 请求里显式指定回复语言，避免“App 界面是英文、但
后端因为用户消息是中文就回中文”这类不一致。

## 用法

```json
POST /v1/agent/turn
{
  "user_id": "app-user-42",
  "message": "兑换",
  "language": "en"
}
```

- 取值：`zh` / `en`
- 也兼容 `metadata.language`
- 不传时后端按消息/历史自动判断（旧行为不变）
- 语言会持久化到 session，后续状态卡/广播卡继续使用同一语言

## 语言解析优先级

1. `request.language`
2. `metadata.language`
3. 用户消息自动检测（`detect_message_language`）
4. 兜底 `zh`（产品默认）

## 受影响的卡片与文案

| 卡片 | 类型 | 文案示例（zh / en） |
| --- | --- | --- |
| 余额卡 | `wallet_query` | `当前钱包没有查到可用余额。` / `I couldn't find any available balance in this wallet.` |
| 资产总览卡 | `portfolio_query` | `当前资产组合包含 N 项资产，总价值约 $...：` / `Your portfolio contains N assets, worth about $... in total:` |
| 转账快捷卡 | suggestions | `输入地址` / `Enter an address`；`转给联系人` / `Pick a contact`；`先看看余额` / `Check my balance` |
| 金额卡 | suggestions | `用余额的 25%` / `Use 25% of your balance` |
| 兑换意图推荐卡 | `swap_quick_replies` | `把 X 换成 Y` / `Swap X to Y`；`其他币对` / `Other pairs`；`跨链换` / `Cross-chain swap` |
| 兑换/转账澄清卡 | `clarification` | `请补充收款地址...` / `Please provide the recipient address...` |
| 兑换状态卡 | `swap_status` | `兑换已完成...` / `Swap completed...`；`兑换没有成功完成...` / `The swap did not complete...` |
| 广播状态卡 | `broadcast_status` | `源链交易已广播，正在等待链上确认` / `Source-chain transaction broadcast; waiting for on-chain confirmation` |
| 交易状态/失败解释页 | `transaction_status` | 兜底文案：`交易执行失败或已回滚。` / `The transaction failed or was reverted.` |
| 预检查余额不足提示 | `preflight` | `...预检查显示钱包余额不足。` / 对应英文 |
| 不支持/兜底回复 | `unsupported` / 其他 | `抱歉，我目前只能处理...` / `Sorry, I can currently only help with...` |
| 模型对话回复 | ReAct | 系统提示要求模型始终按指定语言回复 |
| 添加好友卡 | `client_action.add_contact` | `reason`、默认 `suggested_name`（好友 / Friend）、无效地址错误文案 |
| 地址风险卡 | `client_action.address_risk` | `title`（地址风险提示 / Address Risk Warning）、`message`、`detail`、`cancel_label`（取消转账 / Cancel transfer）、`continue_label`（继续转账 / Continue transfer）、`reason` |

## 不受本地化影响的字段

- 币种 / 链名 / 金额 / 图标等结构化数据（由 App 展示）
- Provider 返回的 `failure_reason` / `eta` 等原始字段（不做翻译）
- App 自己渲染的静态 UI 文案

## 实现要点

- `TurnRequest.language`：新增顶层可选字段。
- session 持久化：新会话按 `language` 建会话；已有会话在 App 传新语言时更新。
- ReAct 系统提示：追加“请始终使用中文/英文回复用户”。
- 确定性文案：`response_facts` / `naturalize_read_response` / `_status_language`
  等统一优先读 App 语言。
- 结构化建议：`suggestions` 的 label/message、金额/链/币种卡片、状态卡文案按
  语言分支生成。
- 客户端动作：`add_contact` / `address_risk` 的 reason、标题、按钮、默认备注
  等也按语言分支生成。

## 验证

```bash
# 传 en + 中文消息，余额卡文案应为英文
curl -X POST http://127.0.0.1:8000/v1/agent/turn \
  -H 'Content-Type: application/json' \
  -d '{"user_id":"u1","message":"兑换","language":"en"}'
```

测试覆盖：`tests/graph/test_language.py`、`tests/graph/test_client_action.py`、
`tests/graph/test_input_address_risk.py`、`tests/api/test_swap_flow.py`。
