# Issue V169-IOS-025：兑换状态判定字段说明（给 App 端）

## 1. 问题背景

- 现象：兑换已到账，App 仍显示「交易中 / pending」，没有更新为「完成」。
- 根因方向：App 判定依赖的字段有冗余，可能把「源链已确认但目标链未到账」
  （`broadcast_status = source_confirmed/confirmed`）当成兑换完成，或读取了会话
  生命周期态（`status` / `stage`）而不是 Provider 订单终态。
- 后端已经把这些状态归一化并下发，App 只需要按下面的优先级读一个权威字段即可。

## 2. 判定规则（按优先级，只认第一个命中）

| 优先级 | 字段 | 说明 |
| --- | --- | --- |
| 1 | `order_status.status`（REST）<br>`status.status` 或 `provider_status`（SSE 生命周期）<br>`status`（ReAct `query_swap_status` 工具结果） | **唯一权威终态来源**。后端已把 Provider 原始状态归一化 |
| 2 | `broadcast_status` | 源链广播状态。只在没有 `order_status`（订单还没被 Provider 接收）时用于“处理中/失败” |
| 3 | `status` / `stage` | 会话生命周期态，**不要用来判终态** |
| 4 | Provider 原始字段（`provider_payload.detailState` 等） | App 不要读，后端已归一化 |

## 3. 归一化取值

### 3.1 `order_status.status`（订单终态，最权威）

| 取值 | App 展示 | 是否停止轮询 |
| --- | --- | --- |
| `completed` | 完成 / 成功 | ✅ 停 |
| `refunded` | 已退款（终态） | ✅ 停 |
| `failed` | 失败 | ✅ 停 |
| `timed_out` | 超时/未返回最终结果（终态） | ✅ 停 |
| `kyc_required` | 需要补充 KYC（终态） | ✅ 停 |
| `cancelled` | 已取消（终态） | ✅ 停 |
| `pending` / `processing` / `broadcasted` / `broadcast_seen` / `broadcast_pending` / `not_propagated` / `confirmed` / `unknown` | 处理中 / 继续轮询 | ❌ 继续 |

### 3.2 `broadcast_status`（源链状态）

| 取值 | 说明 |
| --- | --- |
| `not_propagated` | 源链还没查到这笔交易，哈希已保存 |
| `broadcast_seen` / `broadcast_pending` | 源链已广播，等待确认 |
| `source_confirmed` | 源链已确认，但**目标链未必到账**，不等于兑换完成 |
| `confirmed` | 源链已确认；只有配合 `order_status.status == completed` 才是兑换完成 |
| `failed` | 源链失败 |
| `dropped_or_replaced` | 交易可能被替换/丢弃 |
| `unknown` | 暂时无法确认 |

> **关键**：`source_confirmed` / `confirmed` 单独出现 ≠ 兑换完成。跨链兑换的
> “完成”只由 `order_status.status == completed` 决定。

### 3.3 `status` / `stage`（不要用）

- 广播后会短暂等于 `broadcast_status`。
- Provider 轮询到终态后后端会把 `status` / `stage` 同步为 `order_status.status`，
  但它们同时也会被会话生命周期覆盖（如 `swap_ready`、`cancelled` 等）。
- **App 不要用这两个字段判“兑换完成/失败”**，会造成冗余判断不一致。

## 4. 后端同步机制

- App 轮询 `GET /v1/swap/{session_id}` 时，后端每次都会：
  1. 如果 `order_status.status` 已经是终态 → 直接返回，不再向 Provider 查询。
  2. 否则向 Provider 查一次，归一化后写入 `order_status` / `status` / `stage`。
  3. 只有 `provider_status == "completed"` 时，才把 `broadcast_status` 提升为
     `confirmed`。
- SSE 生命周期 `swap_status` 同样下发归一化后的 `provider_status` 与
  `broadcast_status`；ReAct 聊天 `query_swap_status` 工具结果里的 `status`
  就是归一化后的订单终态字符串。

## 5. App 建议实现

### 5.1 展示判断

```text
if order_status.status 存在:
    switch order_status.status:
        completed            -> 完成
        refunded             -> 已退款
        failed / timed_out /
        kyc_required /
        cancelled            -> 失败/停止
        pending / processing /
        broadcasted / ...    -> 处理中（继续轮询）
else:
    # 订单还没被 Provider 接收，只有源链状态
    switch broadcast_status:
        failed / dropped_or_replaced -> 失败
        not_propagated / broadcast_seen /
        broadcast_pending / source_confirmed /
        confirmed / unknown           -> 处理中（继续轮询）
```

### 5.2 停止轮询

当出现以下任一值，App 停止轮询并固定展示终态卡片：

- `order_status.status ∈ {completed, failed, refunded, timed_out, kyc_required, cancelled}`
- `broadcast_status ∈ {failed, dropped_or_replaced}`（且没有 order_status 时）

### 5.3 不要做的判断

- ❌ 不要用 `broadcast_status == "confirmed"` 判成功。
- ❌ 不要用 `broadcast_status == "source_confirmed"` 判成功。
- ❌ 不要用顶层 `status` / `stage` 判成功。
- ❌ 不要解析 Provider 原始状态字符串。

## 6. 字段来源对照

| 数据入口 | 权威字段 |
| --- | --- |
| `GET /v1/swap/{session_id}` | `order_status.status`（嵌套对象里） |
| SSE 生命周期 `swap_status` | `status.status` 或 `provider_status` |
| ReAct 聊天 `query_swap_status` 工具结果 | `status` |
| 广播接口响应 | 先看 `broadcast_status`；订单被 Provider 接收后看 `order_status` |

## 7. 示例

```json
// 处理中：源链已确认，但目标链未到账
{
  "order_status": {"provider": "omnibridge", "status": "processing"},
  "broadcast_status": "source_confirmed",
  "status": "processing",
  "stage": "processing"
}

// 已到账：唯一可判「完成」
{
  "order_status": {"provider": "omnibridge", "status": "completed"},
  "broadcast_status": "confirmed",
  "status": "completed",
  "stage": "completed"
}

// 失败：broadcast_status 可能仍是 confirmed/source_confirmed，以 order_status 为准
{
  "order_status": {"provider": "omnibridge", "status": "failed", "failure_reason": "..."},
  "broadcast_status": "confirmed",
  "status": "failed",
  "stage": "failed"
}

// 订单还没被 Provider 接收
{
  "order_status": null,
  "broadcast_status": "not_propagated",
  "status": "not_propagated",
  "stage": "not_propagated"
}
```

## 8. 结论

- 后端已经解决“已到账仍 pending”所需的归一化状态与被动轮询。
- App 端只需把判定收敛到**一个**字段：`order_status.status`（没有订单时用
  `broadcast_status`），并忽略 `status` / `stage` / Provider 原始字段。
- 这样两侧判定一致，App 上的冗余判断可以删掉。
